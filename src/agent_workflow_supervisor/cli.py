"""Command-line interface for running durable supervisor ticks."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer
from tomlkit import dumps as toml_dumps
from tomlkit import parse as toml_parse

from agent_workflow_supervisor.accounts import add_claude_account, list_claude_accounts
from agent_workflow_supervisor.adapters.ao import AoRunner
from agent_workflow_supervisor.config import AppConfig, load_config
from agent_workflow_supervisor.identifiers import canonical_github_issue_id
from agent_workflow_supervisor.model_health import diagnose_model_profile
from agent_workflow_supervisor.project_accounts import (
    bind_ao_project_account,
    install_ao_orchestrator_rules,
    resolve_or_create_project_config,
    schedule_ao_project_account_switch,
    set_project_accounts,
)
from agent_workflow_supervisor.registry import (
    REGISTRY_PATH,
    get_model_profile,
    get_project,
    list_model_profiles,
    list_projects,
    register_account,
    register_model_profile,
    register_project,
    set_project_model_profiles,
)
from agent_workflow_supervisor.runtime import graph_runtime, workflow_thread_id
from agent_workflow_supervisor.service import (
    JobStore,
    job_as_dict,
    read_pid,
    runtime_paths,
    service_running,
    start_service,
    stop_service,
)

app = typer.Typer(no_args_is_help=True, help="Durable, provider-neutral agent supervisor.")
account_app = typer.Typer(no_args_is_help=True, help="Manage isolated agent logins.")
project_app = typer.Typer(no_args_is_help=True, help="Manage project-level policy.")
model_app = typer.Typer(no_args_is_help=True, help="Manage reusable runner/model profiles.")
app.add_typer(account_app, name="account")
app.add_typer(project_app, name="project")
app.add_typer(model_app, name="model")
DEFAULT_CONFIG_PATH = Path(
    os.environ.get(
        "OA_CONFIG",
        str(REGISTRY_PATH.parent / "config.toml"),
    )
).expanduser()
ConfigPath = Annotated[Path | None, typer.Option("--config")]
ProjectId = Annotated[str | None, typer.Option("--project")]
WorkItemId = Annotated[str, typer.Option("--work-item")]


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "value") and hasattr(value, "id"):
        return {"id": value.id, "value": _jsonable(value.value)}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def _print(value: Any) -> None:
    typer.echo(json.dumps(_jsonable(value), ensure_ascii=False, indent=2))


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        delete=False,
    ) as temporary:
        temporary.write(text)
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, path)


def _load_runtime_config(path: Path) -> AppConfig:
    return load_config(path, registry_path=REGISTRY_PATH)


def _resolve_config_path(path: Path | None, project_id: str | None = None) -> Path:
    if path is not None:
        candidate = path.expanduser().resolve()
    elif project_id is not None:
        try:
            candidate = get_project(project_id).config_path.expanduser().resolve()
        except KeyError:
            candidate = DEFAULT_CONFIG_PATH.resolve()
            if candidate.is_file() and _load_runtime_config(candidate).project.id != project_id:
                raise typer.BadParameter(
                    f"project {project_id!r} is not registered; use `oa project accounts`"
                ) from None
    else:
        candidate = DEFAULT_CONFIG_PATH.resolve()
    if not candidate.is_file():
        raise typer.BadParameter(
            f"configuration not found at {candidate}; run `oa setup --source FILE`"
        )
    return candidate


def _thread_id(config: AppConfig, work_item_id: str) -> str:
    return workflow_thread_id(config, work_item_id)


def _canonical_work_item_id(config: AppConfig, work_item_id: str) -> str:
    try:
        return canonical_github_issue_id(
            work_item_id,
            config.tracker.repository,
            strict=True,
        )
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error


def _dispatch_origin_session(config: AppConfig) -> str | None:
    if config.runner.type != "ao":
        return None
    session_id = os.environ.get("AO_SESSION_ID")
    if not session_id:
        return None
    session = AoRunner(
        config.runner.command,
        repository=config.tracker.repository,
    ).get_session(session_id)
    if session is None:
        raise typer.BadParameter(f"AO origin session {session_id!r} was not found")
    if (
        not session.active
        or session.role != "orchestrator"
        or session.project_id not in {None, config.project.id}
    ):
        raise typer.BadParameter(
            "dispatch notifications require the active orchestrator for this AO project"
        )
    return session.id


@app.command()
def setup(
    source: Annotated[Path, typer.Option("--source", exists=True, dir_okay=False)],
    apply: Annotated[
        bool,
        typer.Option("--apply", help="Enable real external mutations in the installed config."),
    ] = False,
) -> None:
    """Install one project configuration as the default used by `oa`."""
    source_path = source.expanduser().resolve()
    document = toml_parse(source_path.read_text(encoding="utf-8"))
    source_config = load_config(source_path)
    if source_config.runner.type == "process":
        runner = document.setdefault("runner", {})
        runner["repository_path"] = str(source_config.runner.repository_path)
        runner["worktree_root"] = str(source_config.runner.worktree_root)
    if apply:
        document.setdefault("supervisor", {})["shadow_mode"] = False
    DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    rendered = toml_dumps(document)
    _atomic_write_text(DEFAULT_CONFIG_PATH, rendered)
    config = load_config(DEFAULT_CONFIG_PATH)
    register_account("default", config_dir=None)
    register_project(config.project.id, config_path=DEFAULT_CONFIG_PATH)
    ao_rules = (
        install_ao_orchestrator_rules(
            config.project.id,
            ao_command=config.runner.command,
        )
        if config.runner.type == "ao"
        else None
    )
    _print(
        {
            "installed": str(DEFAULT_CONFIG_PATH),
            "project_id": config.project.id,
            "shadow_mode": config.supervisor.shadow_mode,
            "merge_mode": config.policy.merge_mode,
            "runner": config.runner.type,
            "ao_rules": ao_rules,
        }
    )


@app.command()
def start(config_path: ConfigPath = None, project_id: ProjectId = None) -> None:
    """Start the configured runner and background supervisor."""
    resolved = _resolve_config_path(config_path, project_id)
    config = _load_runtime_config(resolved)
    pid = start_service(resolved)
    _, log_path = runtime_paths(config)
    _print(
        {
            "running": True,
            "pid": pid,
            "project_id": config.project.id,
            "shadow_mode": config.supervisor.shadow_mode,
            "merge_mode": config.policy.merge_mode,
            "runner": config.runner.type,
            "log": str(log_path),
        }
    )


@app.command()
def stop(config_path: ConfigPath = None, project_id: ProjectId = None) -> None:
    """Stop the background supervisor without stopping active workers."""
    resolved = _resolve_config_path(config_path, project_id)
    config = _load_runtime_config(resolved)
    stopped = stop_service(config)
    _print({"stopped": stopped, "project_id": config.project.id})


@app.command()
def dispatch(
    work_item_id: Annotated[str, typer.Argument(help="Issue or work-item id")],
    config_path: ConfigPath = None,
    project_id: ProjectId = None,
) -> None:
    """Explicitly enqueue one work item and ensure the supervisor is running."""
    resolved = _resolve_config_path(config_path, project_id)
    config = _load_runtime_config(resolved)
    work_item_id = _canonical_work_item_id(config, work_item_id)
    origin_session_id = _dispatch_origin_session(config)
    pid = start_service(resolved)
    job = JobStore(config.supervisor.runtime_dir).dispatch(
        work_item_id,
        origin_session_id=origin_session_id,
    )
    _print({"dispatched": work_item_id, "service_pid": pid, "job": job_as_dict(job)})


@account_app.command("add")
def account_add(
    name: Annotated[str, typer.Argument(help="Short name, such as secondary")],
    config_dir: Annotated[
        Path | None,
        typer.Option(
            "--config-dir",
            help="Adopt an existing authenticated CLAUDE_CONFIG_DIR without logging in again",
        ),
    ] = None,
) -> None:
    """Interactively add a global Claude login without assigning a project."""
    result = add_claude_account(name, config_dir=config_dir)
    _print(result)


@account_app.command("list")
def account_list() -> None:
    """Show configured Claude login profiles without exposing credentials."""
    _print({"accounts": list_claude_accounts()})


@model_app.command("add")
def model_add(
    name: Annotated[str, typer.Argument(help="Reusable profile name, such as local-qwen")],
    harness: Annotated[str, typer.Option("--harness", help="AO worker harness")],
    model: Annotated[str, typer.Option("--model", help="Model id passed to the harness")],
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Optional backend name used for readiness checks"),
    ] = None,
    capacity: Annotated[
        int,
        typer.Option("--capacity", min=1, help="Safe active-worker limit for this harness"),
    ] = 1,
) -> None:
    """Create or replace a global non-secret model profile."""

    affected_projects = [
        project
        for project in list_projects()
        if name in project.model_profiles and project.config_path.is_file()
    ]
    try:
        previous_profile = get_model_profile(name)
    except KeyError:
        previous_profile = None
    stopped_projects = []
    try:
        for project in affected_projects:
            config = _load_runtime_config(project.config_path)
            if service_running(config):
                stop_service(config)
                stopped_projects.append(project)

        profile = register_model_profile(
            name,
            harness=harness,
            model=model,
            provider=provider,
            capacity=capacity,
        )
        try:
            for project in affected_projects:
                if project.accounts:
                    set_project_accounts(
                        project.project_id,
                        list(project.accounts),
                        config_path=project.config_path,
                        registry_path=REGISTRY_PATH,
                    )
                _load_runtime_config(project.config_path)
        except Exception:
            if previous_profile is not None:
                register_model_profile(
                    previous_profile.name,
                    harness=previous_profile.harness,
                    model=previous_profile.model,
                    provider=previous_profile.provider,
                    capacity=previous_profile.capacity,
                )
                for project in affected_projects:
                    if project.accounts:
                        set_project_accounts(
                            project.project_id,
                            list(project.accounts),
                            config_path=project.config_path,
                            registry_path=REGISTRY_PATH,
                        )
            raise
    finally:
        for project in stopped_projects:
            start_service(project.config_path)
    restarted_projects = [project.project_id for project in stopped_projects]
    _print({**profile.__dict__, "restarted_projects": restarted_projects})


@model_app.command("list")
def model_list() -> None:
    """List global model profiles and the projects that use them."""

    projects = list_projects()
    _print(
        {
            "models": [
                {
                    **profile.__dict__,
                    "assigned_projects": [
                        project.project_id
                        for project in projects
                        if profile.name in project.model_profiles
                    ],
                }
                for profile in list_model_profiles()
            ]
        }
    )


@model_app.command("doctor")
def model_doctor(
    name: Annotated[str, typer.Argument(help="Global model profile name")],
    config_path: ConfigPath = None,
    project_id: ProjectId = None,
) -> None:
    """Check AO, harness, and local-provider readiness without changing anything."""

    try:
        profile = get_model_profile(name)
    except KeyError as error:
        raise typer.BadParameter(f"model profile {name!r} is not registered") from error
    if config_path is not None or project_id is not None or DEFAULT_CONFIG_PATH.is_file():
        resolved = _resolve_config_path(config_path, project_id)
        runtime_config = _load_runtime_config(resolved)
        ao_command = runtime_config.runner.command
        runner_type = runtime_config.runner.type
        process_commands = runtime_config.runner.commands
    else:
        ao_command = "ao"
        runner_type = "ao"
        process_commands = None
    _print(
        diagnose_model_profile(
            profile,
            ao_command=ao_command,
            runner_type=runner_type,
            process_commands=process_commands,
        )
    )


@project_app.command("accounts")
def project_accounts(
    project_id: Annotated[str, typer.Argument(help="Supervisor project id")],
    selected: Annotated[
        str | None,
        typer.Option("--set", help="Comma-separated global account names"),
    ] = None,
) -> None:
    """Show or replace the Claude accounts allowed for one project."""
    if selected is None:
        try:
            project = get_project(project_id)
        except KeyError as error:
            raise typer.BadParameter(f"project {project_id!r} is not registered") from error
        _print(
            {
                "project_id": project.project_id,
                "accounts": list(project.accounts),
                "config": str(project.config_path),
            }
        )
        return

    account_names = [name.strip() for name in selected.split(",") if name.strip()]
    config_path = resolve_or_create_project_config(
        project_id,
        default_config_path=DEFAULT_CONFIG_PATH,
        registry_path=REGISTRY_PATH,
    )
    config = _load_runtime_config(config_path)
    was_running = service_running(config)
    if was_running:
        stop_service(config)
    try:
        result = set_project_accounts(
            project_id,
            account_names,
            config_path=config_path,
            registry_path=REGISTRY_PATH,
        )
    finally:
        if was_running:
            start_service(config_path)
    _print(result)


@project_app.command("bind-account")
def project_bind_account(
    project_id: Annotated[str, typer.Argument(help="Existing AO project id")],
    account_name: Annotated[
        str,
        typer.Option("--use", help="Global Claude account name"),
    ],
    restart: Annotated[
        bool,
        typer.Option(
            "--restart",
            help="Replace active orchestrator after the new binding is saved",
        ),
    ] = False,
    session_id: Annotated[
        str | None,
        typer.Option(
            "--session",
            help="Specific active orchestrator to replace when more than one exists",
        ),
    ] = None,
) -> None:
    """Bind one AO project to one Claude account for future manual sessions."""

    if session_id is not None and not restart:
        raise typer.BadParameter("--session requires --restart")

    ao_command = (
        _load_runtime_config(DEFAULT_CONFIG_PATH).runner.command
        if DEFAULT_CONFIG_PATH.is_file()
        else "ao"
    )
    _print(
        bind_ao_project_account(
            project_id,
            account_name,
            ao_command=ao_command,
            restart_orchestrator=restart,
            orchestrator_session_id=session_id,
        )
    )


@project_app.command("switch-account")
def project_switch_account(
    project_id: Annotated[str, typer.Argument(help="Current AO project id")],
    account_name: Annotated[
        str,
        typer.Option("--use", help="Registered Claude account name"),
    ],
) -> None:
    """Safely replace this AO conversation with one using another Claude account."""

    ao_command = (
        _load_runtime_config(DEFAULT_CONFIG_PATH).runner.command
        if DEFAULT_CONFIG_PATH.is_file()
        else "ao"
    )
    _print(
        schedule_ao_project_account_switch(
            project_id,
            account_name,
            ao_command=ao_command,
        )
    )


@project_app.command("register")
def project_register(
    project_id: Annotated[str, typer.Argument(help="Existing AO project id")],
) -> None:
    """Register an AO project and create its supervisor config when needed."""

    config_path = resolve_or_create_project_config(
        project_id,
        default_config_path=DEFAULT_CONFIG_PATH,
        registry_path=REGISTRY_PATH,
    )
    config = _load_runtime_config(config_path)
    project = get_project(project_id)
    ao_rules = install_ao_orchestrator_rules(
        project_id,
        ao_command=config.runner.command,
    )
    _print(
        {
            "project_id": project.project_id,
            "config": str(config_path),
            "repository": config.tracker.repository,
            "shadow_mode": config.supervisor.shadow_mode,
            "merge_mode": config.policy.merge_mode,
            "ao_rules": ao_rules,
        }
    )


@project_app.command("list")
def project_list() -> None:
    """List projects registered with the supervisor."""

    _print(
        {
            "projects": [
                {
                    "project_id": project.project_id,
                    "config": str(project.config_path),
                    "accounts": list(project.accounts),
                    "models": list(project.model_profiles),
                    "default_model": project.default_model_profile,
                    "merge_mode": (
                        _load_runtime_config(project.config_path).policy.merge_mode
                        if project.config_path.is_file()
                        else None
                    ),
                }
                for project in list_projects()
            ]
        }
    )


@project_app.command("merge-mode")
def project_merge_mode(
    project_id: Annotated[str, typer.Argument(help="AO project id")],
    selected: Annotated[
        str | None,
        typer.Option("--set", help="automatic or manual"),
    ] = None,
) -> None:
    """Show or change whether approved changes merge without user confirmation."""

    config_path = resolve_or_create_project_config(
        project_id,
        default_config_path=DEFAULT_CONFIG_PATH,
        registry_path=REGISTRY_PATH,
    )
    config = _load_runtime_config(config_path)
    if selected is None:
        _print(
            {
                "project_id": project_id,
                "merge_mode": config.policy.merge_mode,
                "config": str(config_path),
            }
        )
        return
    if selected not in {"automatic", "manual"}:
        raise typer.BadParameter("--set must be automatic or manual")

    was_running = service_running(config)
    if was_running:
        stop_service(config)
    original = config_path.read_text(encoding="utf-8")
    try:
        document = toml_parse(original)
        document.setdefault("policy", {})["merge_mode"] = selected
        _atomic_write_text(config_path, toml_dumps(document))
        updated = _load_runtime_config(config_path)
    except Exception:
        _atomic_write_text(config_path, original)
        raise
    finally:
        if was_running:
            start_service(config_path)

    _print(
        {
            "project_id": project_id,
            "merge_mode": updated.policy.merge_mode,
            "config": str(config_path),
            "restarted": was_running,
        }
    )


@project_app.command("models")
def project_models(
    project_id: Annotated[str, typer.Argument(help="AO project id")],
    selected: Annotated[
        str | None,
        typer.Option("--set", help="Comma-separated global model profile names"),
    ] = None,
    default: Annotated[
        str | None,
        typer.Option("--default", help="Default profile for unmatched work items"),
    ] = None,
) -> None:
    """Show or replace the model profiles allowed for one project."""

    if selected is None:
        try:
            project = get_project(project_id)
        except KeyError as error:
            raise typer.BadParameter(f"project {project_id!r} is not registered") from error
        _print(
            {
                "project_id": project.project_id,
                "models": list(project.model_profiles),
                "default": project.default_model_profile,
                "config": str(project.config_path),
            }
        )
        return

    profile_names = [name.strip() for name in selected.split(",") if name.strip()]
    if not profile_names:
        raise typer.BadParameter("--set must contain at least one model profile")
    selected_default = default or profile_names[0]
    config_path = resolve_or_create_project_config(
        project_id,
        default_config_path=DEFAULT_CONFIG_PATH,
        registry_path=REGISTRY_PATH,
    )
    previous = get_project(project_id)
    service_config = _load_runtime_config(config_path)
    was_running = service_running(service_config)
    if was_running:
        stop_service(service_config)
    try:
        try:
            project = set_project_model_profiles(
                project_id,
                profile_names,
                default_profile=selected_default,
            )
            _load_runtime_config(config_path)
            if project.accounts:
                set_project_accounts(
                    project_id,
                    list(project.accounts),
                    config_path=config_path,
                    registry_path=REGISTRY_PATH,
                )
        except Exception:
            register_project(
                previous.project_id,
                config_path=previous.config_path,
                accounts=previous.accounts,
                model_profiles=previous.model_profiles,
                default_model_profile=previous.default_model_profile,
            )
            if previous.accounts:
                set_project_accounts(
                    project_id,
                    list(previous.accounts),
                    config_path=config_path,
                    registry_path=REGISTRY_PATH,
                )
            raise
    finally:
        if was_running:
            start_service(config_path)
    _print(
        {
            "project_id": project.project_id,
            "models": list(project.model_profiles),
            "default": project.default_model_profile,
        }
    )


@app.command()
def validate(config_path: ConfigPath = None, project_id: ProjectId = None) -> None:
    """Validate configuration without calling external providers."""
    config = _load_runtime_config(_resolve_config_path(config_path, project_id))
    _print(config.model_dump(mode="json"))


@app.command()
def tick(
    work_item_id: WorkItemId,
    config_path: ConfigPath = None,
    project_id: ProjectId = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Allow external mutations for this tick even when config uses shadow mode.",
        ),
    ] = False,
) -> None:
    """Reconcile one work item until it reaches a wait, approval, or terminal state."""
    config = _load_runtime_config(_resolve_config_path(config_path, project_id))
    work_item_id = _canonical_work_item_id(config, work_item_id)
    if apply:
        config.supervisor.shadow_mode = False
    thread_id = _thread_id(config, work_item_id)
    run_config = {"configurable": {"thread_id": thread_id}}
    with graph_runtime(config) as graph:
        result = graph.invoke(
            {"project_id": config.project.id, "work_item_id": work_item_id, "events": []},
            config=run_config,
        )
    _print({"thread_id": thread_id, "state": result})


@app.command()
def approve(
    work_item_id: Annotated[str, typer.Argument(help="Issue or work-item id")],
    decision: Annotated[str, typer.Option("--decision", help="approve or reject")] = "approve",
    config_path: ConfigPath = None,
    project_id: ProjectId = None,
) -> None:
    """Queue a decision for a workflow waiting at a human approval gate."""
    if decision not in {"approve", "reject"}:
        raise typer.BadParameter("decision must be approve or reject")
    resolved = _resolve_config_path(config_path, project_id)
    config = _load_runtime_config(resolved)
    work_item_id = _canonical_work_item_id(config, work_item_id)
    store = JobStore(config.supervisor.runtime_dir)
    thread_id = _thread_id(config, work_item_id)
    run_config = {"configurable": {"thread_id": thread_id}}
    with graph_runtime(config) as graph:
        snapshot = graph.get_state(run_config)
    if "approval_gate" not in snapshot.next:
        raise typer.BadParameter(
            f"work item {work_item_id} is not currently waiting at an approval gate"
        )
    change_id = str(snapshot.values.get("change_id") or "")
    target_sha = str(snapshot.values.get("change_head_sha") or "")
    if not change_id or not target_sha:
        raise typer.BadParameter("approval gate does not identify a change id and target SHA")
    try:
        job = store.approve(
            work_item_id,
            decision,
            change_id=change_id,
            target_sha=target_sha,
        )
    except KeyError as error:
        raise typer.BadParameter(f"work item {work_item_id} has not been dispatched") from error
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    pid = start_service(resolved)
    _print(
        {
            "queued_decision": decision,
            "work_item_id": work_item_id,
            "change_id": change_id,
            "target_sha": target_sha,
            "service_pid": pid,
            "job": job_as_dict(job),
        }
    )


@app.command()
def status(
    work_item_id: Annotated[str | None, typer.Option("--work-item")] = None,
    config_path: ConfigPath = None,
    project_id: ProjectId = None,
) -> None:
    """Show service and queue status, optionally with one graph checkpoint."""
    config = _load_runtime_config(_resolve_config_path(config_path, project_id))
    pid = read_pid(config)
    store = JobStore(config.supervisor.runtime_dir)
    service = {"running": service_running(config), "pid": pid}
    if work_item_id is None:
        _print(
            {
                "service": service,
                "project_id": config.project.id,
                "shadow_mode": config.supervisor.shadow_mode,
                "merge_mode": config.policy.merge_mode,
                "jobs": [job_as_dict(job) for job in store.list()],
            }
        )
        return

    work_item_id = _canonical_work_item_id(config, work_item_id)
    thread_id = _thread_id(config, work_item_id)
    run_config = {"configurable": {"thread_id": thread_id}}
    with graph_runtime(config) as graph:
        snapshot = graph.get_state(run_config)
    _print(
        {
            "service": service,
            "project_id": config.project.id,
            "shadow_mode": config.supervisor.shadow_mode,
            "merge_mode": config.policy.merge_mode,
            "job": job_as_dict(job) if (job := store.get(work_item_id)) else None,
            "thread_id": thread_id,
            "state": snapshot.values,
            "next": snapshot.next,
            "created_at": snapshot.created_at,
        }
    )


if __name__ == "__main__":
    app()
