"""Explicit project-to-account assignment and AO execution checkout setup."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from tomlkit import document, dumps, parse, table

from agent_workflow_supervisor.accounts import DATA_ROOT, _auth_status, _write_document
from agent_workflow_supervisor.adapters.command import AdapterCommandError, CommandAdapter
from agent_workflow_supervisor.config import load_config
from agent_workflow_supervisor.identifiers import canonical_github_repository
from agent_workflow_supervisor.locking import (
    account_switch_id,
    attach_account_switch_helper,
    clear_account_switch_pending,
    global_capacity_lock,
    list_acquisition_records,
    mark_account_switch_pending,
    project_account_switch_lock,
    record_execution_credential_identity,
)
from agent_workflow_supervisor.registry import (
    CONFIG_ROOT,
    REGISTRY_PATH,
    AccountRecord,
    get_account,
    get_project,
    register_project,
)


def _github_repository(remote: str) -> str:
    return canonical_github_repository(remote)


def _verify_execution_remote(clone_dir: Path, expected_remote: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(clone_dir), "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(f"execution checkout has no readable origin remote: {clone_dir}")
    expected = _github_repository(expected_remote).casefold()
    actual_remote = completed.stdout.strip()
    actual = _github_repository(actual_remote).casefold()
    if actual != expected:
        raise RuntimeError(
            f"execution checkout origin mismatch at {clone_dir}: "
            f"expected {expected}, found {actual}"
        )


def _claude_worker_model(config: Any) -> str | None:
    profile_name = config.policy.default_model_profile
    if profile_name:
        profile = config.policy.model_profiles.get(profile_name)
        if profile is not None and profile.harness == "claude-code":
            return profile.model
    return config.policy.models.get("claude-code")


def _new_project_config(
    project_id: str,
    *,
    repository: str,
    ao_command: str,
) -> Any:
    value = document()
    supervisor = table()
    supervisor.add("database_path", f".state/{project_id}/checkpoints.sqlite")
    supervisor.add("runtime_dir", f".state/{project_id}/runtime")
    supervisor.add("poll_interval_seconds", 5)
    supervisor.add("shadow_mode", True)
    value["supervisor"] = supervisor

    project = table()
    project.add("id", project_id)
    value["project"] = project

    runner = table()
    runner.add("type", "ao")
    runner.add("command", ao_command)
    value["runner"] = runner

    tracker = table()
    tracker.add("type", "github")
    tracker.add("command", "gh")
    tracker.add("repository", repository)
    value["tracker"] = tracker

    policy = table()
    policy.add("merge_mode", "manual")
    policy.add("default_harness", "claude-code")
    policy.add("skip_labels", ["agent:skip"])
    policy.add("approval_labels", [])
    value["policy"] = policy
    capacity = table()
    capacity.add("claude-code", 1)
    policy["capacity"] = capacity
    return value


def resolve_or_create_project_config(
    project_id: str,
    *,
    default_config_path: Path,
    registry_path: Path = REGISTRY_PATH,
) -> Path:
    try:
        registered = get_project(project_id, registry_path)
        if registered.config_path.is_file():
            return registered.config_path.resolve()
    except KeyError:
        pass

    if default_config_path.is_file():
        default_config = load_config(default_config_path, registry_path=registry_path)
        if default_config.project.id == project_id:
            register_project(project_id, config_path=default_config_path, path=registry_path)
            return default_config_path.resolve()
        ao_command = default_config.runner.command
    else:
        ao_command = "ao"

    ao = CommandAdapter(ao_command)
    response = ao.run_json("project", "get", project_id, "--json")
    raw = response.get("project")
    if not isinstance(raw, dict):
        raise ValueError(f"AO project {project_id!r} does not exist")
    repository = _github_repository(str(raw.get("repo") or ""))
    config_path = CONFIG_ROOT / "projects" / f"{project_id}.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        dumps(_new_project_config(project_id, repository=repository, ao_command=ao_command)),
        encoding="utf-8",
    )
    register_project(project_id, config_path=config_path, path=registry_path)
    load_config(config_path)
    return config_path


def _ensure_execution_project(
    *,
    ao: CommandAdapter,
    base_project: dict[str, Any],
    account: AccountRecord,
    model: str | None,
) -> str:
    project_id = str(base_project["id"])
    execution_project_id = f"{project_id}-claude-{account.name}"
    if account.config_dir is None:
        raise RuntimeError(f"non-default account {account.name!r} has no config directory")

    remote = str(base_project.get("repo") or "")
    if not remote:
        raise RuntimeError(f"AO project {project_id!r} has no Git remote")
    clone_dir = (DATA_ROOT / "execution-projects" / project_id / account.name).resolve()
    existing: dict[str, Any] | None = None
    try:
        response = ao.run_json("project", "get", execution_project_id, "--json")
        raw_existing = response.get("project")
        if isinstance(raw_existing, dict):
            existing = raw_existing
    except AdapterCommandError:
        pass

    if not clone_dir.exists():
        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        clone = subprocess.run(
            ["git", "clone", "--filter=blob:none", remote, str(clone_dir)],
            check=False,
            text=True,
        )
        if clone.returncode != 0:
            raise RuntimeError(f"failed to clone {remote} into {clone_dir}")
    elif not (clone_dir / ".git").exists():
        raise RuntimeError(f"execution path is not a Git repository: {clone_dir}")
    _verify_execution_remote(clone_dir, remote)

    if existing is not None:
        registered_path = str(existing.get("path") or "")
        if not registered_path or Path(registered_path).expanduser().resolve() != clone_dir:
            raise RuntimeError(
                f"AO project {execution_project_id!r} is registered to an unexpected path: "
                f"{registered_path or '<missing>'}"
            )
    else:
        ao.run(
            "project",
            "add",
            "--path",
            str(clone_dir),
            "--id",
            execution_project_id,
            "--name",
            f"{project_id} — Claude {account.name}",
            "--worker-agent",
            "claude-code",
        )

    # Execution projects are managed mirrors of the base project's policy. Use
    # the complete typed AO config so a retry repairs partial creation and an
    # account/profile change cannot leave stale environment, model, harness, or
    # permission settings behind.
    base_config = json.loads(json.dumps(base_project.get("config") or {}))
    base_config["defaultBranch"] = str(
        base_config.get("defaultBranch") or base_project.get("defaultBranch") or "auto"
    )
    environment = dict(base_config.get("env") or {})
    environment["CLAUDE_CONFIG_DIR"] = str(account.config_dir.expanduser().resolve())
    base_config["env"] = environment
    worker = dict(base_config.get("worker") or {})
    worker["agent"] = "claude-code"
    worker_agent_config = dict(worker.get("agentConfig") or {})
    if model:
        worker_agent_config["model"] = model
    if worker_agent_config:
        worker["agentConfig"] = worker_agent_config
    else:
        worker.pop("agentConfig", None)
    base_config["worker"] = worker
    ao.run_json(
        "project",
        "set-config",
        execution_project_id,
        "--config-json",
        json.dumps(base_config, ensure_ascii=False),
        "--json",
    )
    record_execution_credential_identity(execution_project_id, account.config_dir)
    return execution_project_id


def _reconcile_default_account_project(
    ao: CommandAdapter, base_project: dict[str, Any]
) -> dict[str, Any]:
    """Remove a stale direct Claude override before using the default login."""

    raw_config = json.loads(json.dumps(base_project.get("config") or {}))
    environment = dict(raw_config.get("env") or {})
    if "CLAUDE_CONFIG_DIR" not in environment:
        record_execution_credential_identity(str(base_project["id"]), None)
        return base_project
    environment.pop("CLAUDE_CONFIG_DIR", None)
    if environment:
        raw_config["env"] = environment
    else:
        raw_config.pop("env", None)
    project_id = str(base_project["id"])
    ao.run_json(
        "project",
        "set-config",
        project_id,
        "--config-json",
        json.dumps(raw_config, ensure_ascii=False),
        "--json",
    )
    record_execution_credential_identity(project_id, None)
    updated = dict(base_project)
    updated["config"] = raw_config
    return updated


def _install_orchestrator_rules(
    ao: CommandAdapter, project_id: str, raw_config: dict[str, Any]
) -> None:
    marker = "[LANGGRAPH_SUPERVISOR]"
    end_marker = "[/LANGGRAPH_SUPERVISOR]"
    current = str(raw_config.get("orchestratorRules") or "")
    if marker in current:
        before, managed = current.split(marker, 1)
        if end_marker in managed:
            _, after = managed.split(end_marker, 1)
            unmanaged = "\n\n".join(part.strip() for part in (before, after) if part.strip())
        else:
            # Migrate the original single-marker format. Content before the
            # marker is operator-owned; the remainder is the old managed block.
            unmanaged = before.rstrip()
    else:
        unmanaged = current.rstrip()
    oa_command = shlex.quote(shutil.which("oa") or "oa")
    addition = (
        f"{marker}\n"
        "Treat this AO conversation as the primary user interface for the durable "
        "supervisor. Run the mapped oa commands yourself; do not tell the user to open a "
        "terminal for normal operation. Only when the user explicitly says to handle, "
        f"implement, fix, or process issue #N, run {oa_command} dispatch N --project "
        f"{shlex.quote(project_id)}. For explicit approval or rejection, run {oa_command} "
        f"approve N --project {shlex.quote(project_id)} with --decision approve or "
        f"--decision reject. For a status request, run {oa_command} status --project "
        f"{shlex.quote(project_id)} --work-item N. To list configured Claude accounts, run "
        f"{oa_command} account list. When the user explicitly asks to switch this AO "
        f"project to a registered Claude account ACCOUNT, run {oa_command} project "
        f"switch-account {shlex.quote(project_id)} --use ACCOUNT. That command schedules a "
        "safe replacement after this turn becomes idle; report that the current conversation "
        "will be replaced. Never run bind-account --restart from inside the session. To list "
        f"model profiles, run {oa_command} model list; to inspect this project's assigned "
        f"models, run {oa_command} project models {shlex.quote(project_id)}. "
        f"To inspect whether this project merges automatically or waits for approval, run "
        f"{oa_command} project merge-mode {shlex.quote(project_id)}. Only when the user "
        f"explicitly asks to change that policy, run the same command with --set automatic "
        f"or --set manual. In manual mode, an explicit request to merge issue #N maps to "
        f"{oa_command} approve N --project {shlex.quote(project_id)} --decision approve, "
        "and only after the workflow is waiting at that exact merge gate. "
        "Interactive account login with oa account add and recovery when AO is unavailable "
        "still require an external terminal. Discussion, inspection, planning, or mentioning "
        "an issue number is not authorization to dispatch or mutate anything. The supervisor owns "
        "worker acquisition, review reconciliation, approval gates, merge, and cleanup.\n"
        f"{end_marker}"
    )
    raw_config["orchestratorRules"] = f"{unmanaged}\n\n{addition}".strip()
    ao.run(
        "project",
        "set-config",
        project_id,
        "--config-json",
        json.dumps(raw_config, ensure_ascii=False),
    )


def install_ao_orchestrator_rules(project_id: str, *, ao_command: str) -> dict[str, Any]:
    """Install the managed AO-first command rules while preserving operator rules."""

    ao = CommandAdapter(ao_command)
    response = ao.run_json("project", "get", project_id, "--json")
    project = response.get("project")
    if not isinstance(project, dict):
        raise RuntimeError(f"AO project {project_id!r} was not found")
    raw_config = dict(project.get("config") or {})
    _install_orchestrator_rules(ao, project_id, raw_config)
    return {"project_id": project_id, "installed": True}


def set_project_accounts(
    project_id: str,
    account_names: list[str],
    *,
    config_path: Path,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    """Reconcile a project account pool under the AO account-operation lock."""

    with global_capacity_lock():
        with project_account_switch_lock(project_id):
            return _set_project_accounts_unlocked(
                project_id,
                account_names,
                config_path=config_path,
                registry_path=registry_path,
            )


def _set_project_accounts_unlocked(
    project_id: str,
    account_names: list[str],
    *,
    config_path: Path,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    if not account_names:
        raise ValueError("at least one account is required")
    if len(account_names) != len(set(account_names)):
        raise ValueError("account list contains duplicates")

    config = load_config(config_path, registry_path=registry_path)
    if config.project.id != project_id:
        raise ValueError(f"configuration belongs to {config.project.id!r}, not {project_id!r}")
    accounts = [get_account(name, registry_path) for name in account_names]
    for account in accounts:
        if not _auth_status(account.config_dir)["logged_in"]:
            raise RuntimeError(f"Claude account {account.name!r} is not logged in")

    total_capacity = config.policy.capacity.get("claude-code", 2)
    if len(accounts) > total_capacity:
        raise ValueError(f"{len(accounts)} accounts exceed Claude capacity {total_capacity}")

    ao: CommandAdapter | None = None
    base_project: dict[str, Any] | None = None
    if config.runner.type == "process":
        # Process profiles are logical capacity namespaces. They all share the
        # configured repository, while each Claude subprocess receives only
        # its selected CLAUDE_CONFIG_DIR.
        execution_ids = {
            account.name: f"{project_id}-process-claude-{account.name}" for account in accounts
        }
    else:
        ao = CommandAdapter(config.runner.command)
        response = ao.run_json("project", "get", project_id, "--json")
        raw_project = response.get("project")
        if not isinstance(raw_project, dict):
            raise RuntimeError(f"AO project {project_id!r} was not found")
        base_project = raw_project
        model = _claude_worker_model(config)
        if any(account.name == "default" for account in accounts):
            base_project = _reconcile_default_account_project(ao, base_project)
        execution_ids = {}
        for account in accounts:
            if account.name == "default":
                execution_ids[account.name] = project_id
            else:
                execution_ids[account.name] = _ensure_execution_project(
                    ao=ao, base_project=base_project, account=account, model=model
                )

    value = parse(config_path.read_text(encoding="utf-8"))
    credentials = value.get("credentials")
    if credentials is None:
        credentials = table()
        value["credentials"] = credentials
    credentials["strategy"] = "least-active"
    # Retain profiles removed from future routing so the graph can still see
    # and safely reconcile workers that were spawned in their AO projects.
    # Only policy.credential_profiles determines eligibility for new work.
    profiles = credentials.get("profiles")
    if profiles is None:
        profiles = table()
        credentials["profiles"] = profiles
    per_account_limit = total_capacity if len(accounts) == 1 else 1
    for account in accounts:
        profile = table()
        profile.add("execution_project_id", execution_ids[account.name])
        profile.add("max_workers", per_account_limit)
        if account.config_dir is not None:
            profile.add("claude_config_dir", str(account.config_dir))
        profiles[f"claude-{account.name}"] = profile
    policy = value["policy"]
    routes = policy.get("credential_profiles")
    if routes is None:
        routes = table()
        policy["credential_profiles"] = routes
    routes["claude-code"] = [f"claude-{name}" for name in account_names]
    _write_document(config_path, value)
    load_config(config_path, registry_path=registry_path)
    register_project(
        project_id,
        config_path=config_path,
        accounts=account_names,
        path=registry_path,
    )
    raw_config = base_project.get("config") if base_project is not None else None
    if ao is not None and isinstance(raw_config, dict):
        _install_orchestrator_rules(ao, project_id, raw_config)
    return {
        "project_id": project_id,
        "accounts": account_names,
        "execution_projects": execution_ids,
        "claude_capacity": total_capacity,
        "runner": config.runner.type,
    }


def bind_ao_project_account(
    project_id: str,
    account_name: str,
    *,
    ao_command: str,
    restart_orchestrator: bool = False,
    orchestrator_session_id: str | None = None,
    replacement_prompt: str | None = None,
    require_no_active_workers: bool = False,
    allow_missing_orchestrator: bool = False,
    pending_switch_id: str | None = None,
    authorized_switch_id: str | None = None,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    """Bind one AO project under global allocation and account-switch locks."""

    with global_capacity_lock():
        current_switch_id = account_switch_id(project_id)
        if current_switch_id is not None and current_switch_id != authorized_switch_id:
            raise RuntimeError(
                f"account switch {current_switch_id!r} is already pending for {project_id!r}"
            )
        if pending_switch_id is not None and current_switch_id is not None:
            raise RuntimeError(
                f"account switch {current_switch_id!r} is already pending for {project_id!r}"
            )
        if restart_orchestrator or require_no_active_workers:
            reservations = list_acquisition_records(project_id)
            if reservations:
                work_items = ", ".join(work_item_id for work_item_id, _ in reservations)
                raise RuntimeError(
                    "cannot change account binding while worker acquisitions are reserved for "
                    f"work items: {work_items}"
                )
        with project_account_switch_lock(project_id):
            result = _bind_ao_project_account_unlocked(
                project_id,
                account_name,
                ao_command=ao_command,
                restart_orchestrator=restart_orchestrator,
                orchestrator_session_id=orchestrator_session_id,
                replacement_prompt=replacement_prompt,
                require_no_active_workers=require_no_active_workers,
                allow_missing_orchestrator=allow_missing_orchestrator,
                registry_path=registry_path,
            )
            if pending_switch_id is not None:
                mark_account_switch_pending(project_id, pending_switch_id)
            return result


def _bind_ao_project_account_unlocked(
    project_id: str,
    account_name: str,
    *,
    ao_command: str,
    restart_orchestrator: bool = False,
    orchestrator_session_id: str | None = None,
    replacement_prompt: str | None = None,
    require_no_active_workers: bool = False,
    allow_missing_orchestrator: bool = False,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    """Bind future AO sessions for one project to one Claude login.

    This is the simple manual-session path. It changes AO's project-scoped
    environment directly and does not require a GitHub tracker, supervisor
    config, extra clone, or credential pool.
    """

    account = get_account(account_name, registry_path)
    readiness = _auth_status(account.config_dir)
    if not readiness["logged_in"]:
        raise RuntimeError(f"Claude account {account_name!r} is not logged in")

    ao = CommandAdapter(ao_command)
    response = ao.run_json("project", "get", project_id, "--json")
    project = response.get("project")
    if not isinstance(project, dict):
        raise RuntimeError(f"AO project {project_id!r} was not found")

    session_response = ao.run_json(
        "session",
        "ls",
        "--project",
        project_id,
        "--all",
        "--json",
    )
    active = [
        session
        for session in session_response.get("data", [])
        if not bool(session.get("isTerminated", False))
    ]
    active_workers = [
        str(session.get("id"))
        for session in active
        if str(session.get("role") or session.get("kind")) == "worker"
    ]
    if (restart_orchestrator or require_no_active_workers) and active_workers:
        raise RuntimeError(
            "cannot change account binding while active workers exist: " + ", ".join(active_workers)
        )
    old_orchestrators = [
        str(session.get("id"))
        for session in active
        if str(session.get("role") or session.get("kind")) == "orchestrator"
    ]
    replaced_orchestrators: list[str] = []
    if restart_orchestrator:
        if orchestrator_session_id is not None:
            if orchestrator_session_id not in old_orchestrators:
                if not allow_missing_orchestrator:
                    raise RuntimeError(
                        f"orchestrator session {orchestrator_session_id!r} is not active in "
                        f"project {project_id!r}"
                    )
            else:
                replaced_orchestrators = [orchestrator_session_id]
        elif len(old_orchestrators) > 1:
            raise RuntimeError(
                "multiple orchestrators are active; select one explicitly with --session"
            )
        else:
            replaced_orchestrators = old_orchestrators

    raw_config = dict(project.get("config") or {})
    environment = dict(raw_config.get("env") or {})
    if account.config_dir is None:
        environment.pop("CLAUDE_CONFIG_DIR", None)
    else:
        environment["CLAUDE_CONFIG_DIR"] = str(account.config_dir.expanduser().resolve())
    if environment:
        raw_config["env"] = environment
    else:
        raw_config.pop("env", None)
    ao.run_json(
        "project",
        "set-config",
        project_id,
        "--config-json",
        json.dumps(raw_config, ensure_ascii=False),
        "--json",
    )
    record_execution_credential_identity(project_id, account.config_dir)

    replacement = None
    if restart_orchestrator:
        for session_id in replaced_orchestrators:
            ao.run("session", "kill", session_id, "--project", project_id)

        # Some AO releases remove the project registration when its last
        # orchestrator is terminated. Re-register from the snapshot before
        # spawning so the account switch remains recoverable.
        try:
            ao.run_json("project", "get", project_id, "--json")
        except AdapterCommandError:
            project_path = str(project.get("path") or "")
            if not project_path:
                raise RuntimeError(
                    f"AO removed project {project_id!r} and its path is unavailable"
                ) from None
            add_args = [
                "project",
                "add",
                "--path",
                project_path,
                "--id",
                project_id,
                "--name",
                str(project.get("name") or project_id),
            ]
            orchestrator_agent = str(
                (raw_config.get("orchestrator") or {}).get("agent")
                or project.get("agent")
                or "claude-code"
            )
            worker_agent = str(
                (raw_config.get("worker") or {}).get("agent")
                or project.get("agent")
                or "claude-code"
            )
            add_args.extend(["--orchestrator-agent", orchestrator_agent])
            add_args.extend(["--worker-agent", worker_agent])
            ao.run(*add_args)
            ao.run_json(
                "project",
                "set-config",
                project_id,
                "--config-json",
                json.dumps(raw_config, ensure_ascii=False),
                "--json",
            )

        spawn_args = [
            "spawn",
            "--project",
            project_id,
            "--kind",
            "orchestrator",
            "--name",
            f"orchestrator-{uuid.uuid4().hex[:8]}",
            "--mode",
            "chat",
        ]
        if replacement_prompt:
            spawn_args.extend(["--prompt", replacement_prompt])
        output = ao.run(*spawn_args)
        match = re.search(r"spawned session ([^\s]+)", output)
        if not match:
            raise RuntimeError("AO spawn output did not include a replacement session id")
        replacement = match.group(1)

    return {
        "project_id": project_id,
        "account": account.name,
        "email": readiness.get("email"),
        "organization": readiness.get("organization"),
        "claude_config_dir": str(account.config_dir) if account.config_dir else None,
        "applies_to": "future_sessions",
        "replaced_orchestrators": replaced_orchestrators,
        "orchestrator_session": replacement,
        "active_sessions_still_using_previous_account": [
            str(session.get("id"))
            for session in active
            if str(session.get("id")) not in replaced_orchestrators
        ],
    }


def schedule_ao_project_account_switch(
    project_id: str,
    account_name: str,
    *,
    ao_command: str,
    source_session_id: str | None = None,
    registry_path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    """Schedule an account switch after the calling AO conversation becomes idle."""

    session_id = source_session_id or os.environ.get("AO_SESSION_ID")
    if not session_id:
        raise RuntimeError(
            "switch-account must run inside an AO conversation; from an external terminal "
            "use `oa project bind-account PROJECT --use ACCOUNT --restart`"
        )

    ao = CommandAdapter(ao_command)
    response = ao.run_json("session", "get", session_id, "--project", project_id, "--json")
    session = response.get("session")
    if not isinstance(session, dict):
        raise RuntimeError(f"AO session {session_id!r} was not found")
    role = str(session.get("role") or session.get("kind") or "")
    if role != "orchestrator":
        raise RuntimeError("switch-account must be requested from the project's orchestrator")

    sessions = ao.run_json("session", "ls", "--project", project_id, "--all", "--json").get(
        "data", []
    )
    active_workers = [
        str(item.get("id"))
        for item in sessions
        if not bool(item.get("isTerminated", False))
        and str(item.get("role") or item.get("kind") or "") == "worker"
    ]
    if active_workers:
        raise RuntimeError(
            "cannot switch accounts while active workers exist: " + ", ".join(active_workers)
        )

    switch_id = uuid.uuid4().hex[:12]

    # Save the binding and a durable allocation barrier while the current turn
    # is alive. The final worker recheck occurs under the same global lock used
    # by worker spawn, closing the account-switch/acquisition race.
    binding = bind_ao_project_account(
        project_id,
        account_name,
        ao_command=ao_command,
        restart_orchestrator=False,
        require_no_active_workers=True,
        pending_switch_id=switch_id,
        registry_path=registry_path,
    )

    switch_dir = DATA_ROOT / "account-switches"
    switch_dir.mkdir(parents=True, exist_ok=True)
    log_path = switch_dir / f"{switch_id}.log"
    command = [
        sys.executable,
        "-m",
        "agent_workflow_supervisor.account_switch",
        "--project",
        project_id,
        "--account",
        account_name,
        "--source-session",
        session_id,
        "--ao-command",
        ao_command,
        "--switch-id",
        switch_id,
    ]
    try:
        with log_path.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        with global_capacity_lock():
            attach_account_switch_helper(project_id, switch_id, process.pid)
    except Exception:
        with global_capacity_lock():
            clear_account_switch_pending(project_id, switch_id)
        raise
    return {
        "scheduled": True,
        "switch_id": switch_id,
        "project_id": project_id,
        "account": account_name,
        "source_session": session_id,
        "helper_pid": process.pid,
        "log": str(log_path),
        "binding_saved": binding["applies_to"] == "future_sessions",
        "next": "finish this AO turn; the replacement starts after the session becomes idle",
    }
