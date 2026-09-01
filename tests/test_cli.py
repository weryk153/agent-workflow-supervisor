import tomllib
from pathlib import Path

import pytest
import typer

import agent_workflow_supervisor.cli as cli_module
from agent_workflow_supervisor.config import (
    AppConfig,
    ProjectConfig,
    SupervisorConfig,
    TrackerConfig,
)
from agent_workflow_supervisor.models import AgentSession
from agent_workflow_supervisor.registry import ModelProfileRecord, ProjectRecord


def test_setup_process_mode_never_installs_ao_rules(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "source" / "process.toml"
    source.parent.mkdir()
    source.write_text(
        """
[project]
id = "demo"

[runner]
type = "process"
repository_path = "."
worktree_root = "../worktrees"

[tracker]
repository = "owner/repo"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    installed = tmp_path / "installed" / "config.toml"
    monkeypatch.setattr(cli_module, "DEFAULT_CONFIG_PATH", installed)
    monkeypatch.setattr(cli_module, "register_account", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "register_project", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        cli_module,
        "install_ao_orchestrator_rules",
        lambda *_args, **_kwargs: pytest.fail("process setup must not call AO"),
    )
    rendered: list[dict] = []
    monkeypatch.setattr(cli_module, "_print", rendered.append)

    cli_module.setup(source)

    saved = tomllib.loads(installed.read_text(encoding="utf-8"))
    assert saved["runner"]["repository_path"] == str(source.parent)
    assert saved["runner"]["worktree_root"] == str(tmp_path / "worktrees")
    assert rendered[0]["runner"] == "process"
    assert rendered[0]["ao_rules"] is None


def test_dispatch_canonicalizes_qualified_github_issue(monkeypatch, tmp_path: Path) -> None:
    config = AppConfig(
        supervisor=SupervisorConfig(runtime_dir=tmp_path),
        project=ProjectConfig(id="demo"),
        tracker=TrackerConfig(repository="owner/repo"),
    )
    dispatched: list[tuple[str, str | None]] = []

    class Store:
        def __init__(self, _runtime_dir: Path) -> None:
            pass

        def dispatch(self, work_item_id: str, *, origin_session_id: str | None = None):
            dispatched.append((work_item_id, origin_session_id))
            return object()

    monkeypatch.setattr(cli_module, "_resolve_config_path", lambda *_args: tmp_path / "demo.toml")
    monkeypatch.setattr(cli_module, "_load_runtime_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "start_service", lambda _path: 1234)
    monkeypatch.setattr(cli_module, "JobStore", Store)
    monkeypatch.setattr(cli_module, "job_as_dict", lambda _job: {})
    monkeypatch.setattr(cli_module, "_print", lambda _value: None)
    monkeypatch.setattr(
        cli_module,
        "AoRunner",
        lambda *_args, **_kwargs: type(
            "Runner",
            (),
            {
                "get_session": lambda _self, _session_id: AgentSession(
                    "demo-orchestrator",
                    "orchestrator",
                    "idle",
                    "claude-code",
                    project_id="demo",
                )
            },
        )(),
    )

    monkeypatch.setenv("AO_SESSION_ID", "demo-orchestrator")
    cli_module.dispatch("github:owner/repo#0194")

    assert dispatched == [("194", "demo-orchestrator")]


def test_dispatch_rejects_worker_as_notification_origin(monkeypatch, tmp_path: Path) -> None:
    config = AppConfig(
        supervisor=SupervisorConfig(runtime_dir=tmp_path),
        project=ProjectConfig(id="demo"),
        tracker=TrackerConfig(repository="owner/repo"),
    )
    monkeypatch.setattr(cli_module, "_resolve_config_path", lambda *_args: tmp_path / "demo.toml")
    monkeypatch.setattr(cli_module, "_load_runtime_config", lambda _path: config)
    monkeypatch.setenv("AO_SESSION_ID", "demo-worker")
    monkeypatch.setattr(
        cli_module,
        "AoRunner",
        lambda *_args, **_kwargs: type(
            "Runner",
            (),
            {
                "get_session": lambda _self, _session_id: AgentSession(
                    "demo-worker",
                    "worker",
                    "working",
                    "claude-code",
                    project_id="demo",
                )
            },
        )(),
    )
    monkeypatch.setattr(
        cli_module,
        "start_service",
        lambda _path: pytest.fail("invalid origin must be rejected before service start"),
    )

    with pytest.raises(typer.BadParameter, match="active orchestrator"):
        cli_module.dispatch("194")


def test_bind_account_session_requires_restart(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "bind_ao_project_account",
        lambda *_args, **_kwargs: pytest.fail("AO must not be changed"),
    )

    with pytest.raises(typer.BadParameter, match="--session requires --restart"):
        cli_module.project_bind_account(
            "demo",
            "work",
            restart=False,
            session_id="demo-1",
        )


def test_model_update_does_not_mutate_registry_when_service_cannot_stop(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "demo.toml"
    config_path.touch()
    project = ProjectRecord(
        "demo",
        config_path,
        accounts=("work",),
        model_profiles=("claude",),
        default_model_profile="claude",
    )
    monkeypatch.setattr(cli_module, "list_projects", lambda: [project])
    monkeypatch.setattr(
        cli_module,
        "get_model_profile",
        lambda _name: ModelProfileRecord("claude", "claude-code", "old-model"),
    )
    monkeypatch.setattr(cli_module, "_load_runtime_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "service_running", lambda _config: True)
    monkeypatch.setattr(
        cli_module,
        "stop_service",
        lambda _config: (_ for _ in ()).throw(TimeoutError("still running")),
    )
    monkeypatch.setattr(
        cli_module,
        "register_model_profile",
        lambda *_args, **_kwargs: pytest.fail("registry must not be mutated"),
    )

    with pytest.raises(TimeoutError, match="still running"):
        cli_module.model_add(
            "claude",
            harness="claude-code",
            model="new-model",
            provider=None,
            capacity=1,
        )


def test_model_update_reconciles_account_projects_and_restarts_service(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "demo.toml"
    config_path.touch()
    project = ProjectRecord(
        "demo",
        config_path,
        accounts=("work",),
        model_profiles=("claude",),
        default_model_profile="claude",
    )
    calls: list[tuple[str, object]] = []
    new_profile = ModelProfileRecord("claude", "claude-code", "claude-sonnet-5")
    monkeypatch.setattr(cli_module, "list_projects", lambda: [project])
    monkeypatch.setattr(
        cli_module,
        "get_model_profile",
        lambda _name: ModelProfileRecord("claude", "claude-code", "old-model"),
    )
    monkeypatch.setattr(cli_module, "_load_runtime_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "service_running", lambda _config: True)
    monkeypatch.setattr(cli_module, "stop_service", lambda _config: calls.append(("stop", "demo")))
    monkeypatch.setattr(
        cli_module,
        "register_model_profile",
        lambda *_args, **_kwargs: new_profile,
    )
    monkeypatch.setattr(
        cli_module,
        "set_project_accounts",
        lambda project_id, accounts, **_kwargs: calls.append(
            ("accounts", (project_id, tuple(accounts)))
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "start_service",
        lambda path: calls.append(("start", path)),
    )
    rendered: list[dict] = []
    monkeypatch.setattr(cli_module, "_print", rendered.append)

    cli_module.model_add(
        "claude",
        harness="claude-code",
        model="claude-sonnet-5",
        provider=None,
        capacity=1,
    )

    assert calls == [
        ("stop", "demo"),
        ("accounts", ("demo", ("work",))),
        ("start", config_path),
    ]
    assert rendered == [
        {
            "name": "claude",
            "harness": "claude-code",
            "model": "claude-sonnet-5",
            "provider": None,
            "capacity": 1,
            "restarted_projects": ["demo"],
        }
    ]


def test_project_model_assignment_rolls_back_when_account_reconciliation_fails(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "demo.toml"
    config_path.touch()
    previous = ProjectRecord(
        "demo",
        config_path,
        accounts=("work",),
        model_profiles=("old",),
        default_model_profile="old",
    )
    updated = ProjectRecord(
        "demo",
        config_path,
        accounts=("work",),
        model_profiles=("new",),
        default_model_profile="new",
    )
    restored: list[tuple[tuple, dict]] = []
    reconciliation_attempts = 0

    monkeypatch.setattr(
        cli_module,
        "resolve_or_create_project_config",
        lambda *_args, **_kwargs: config_path,
    )
    monkeypatch.setattr(cli_module, "get_project", lambda _project_id: previous)
    monkeypatch.setattr(cli_module, "_load_runtime_config", lambda _path: object())
    monkeypatch.setattr(cli_module, "service_running", lambda _config: False)
    monkeypatch.setattr(cli_module, "set_project_model_profiles", lambda *_args, **_kwargs: updated)

    def reconcile(*_args, **_kwargs):
        nonlocal reconciliation_attempts
        reconciliation_attempts += 1
        if reconciliation_attempts == 1:
            raise RuntimeError("AO reconciliation failed")
        return {}

    monkeypatch.setattr(cli_module, "set_project_accounts", reconcile)
    monkeypatch.setattr(
        cli_module,
        "register_project",
        lambda *args, **kwargs: restored.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match="AO reconciliation failed"):
        cli_module.project_models("demo", selected="new", default="new")

    assert reconciliation_attempts == 2
    assert restored == [
        (
            ("demo",),
            {
                "config_path": config_path,
                "accounts": ("work",),
                "model_profiles": ("old",),
                "default_model_profile": "old",
            },
        )
    ]


def test_project_merge_mode_can_be_read_and_changed(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "demo.toml"
    config_path.write_text(
        """
[project]
id = "demo"

[tracker]
repository = "owner/repo"

[policy]
merge_mode = "manual"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "resolve_or_create_project_config",
        lambda *_args, **_kwargs: config_path,
    )
    monkeypatch.setattr(cli_module, "service_running", lambda _config: False)
    rendered: list[dict] = []
    monkeypatch.setattr(cli_module, "_print", rendered.append)

    cli_module.project_merge_mode("demo")
    cli_module.project_merge_mode("demo", selected="automatic")

    assert rendered[0]["merge_mode"] == "manual"
    assert rendered[1] == {
        "project_id": "demo",
        "merge_mode": "automatic",
        "config": str(config_path),
        "restarted": False,
    }
    assert 'merge_mode = "automatic"' in config_path.read_text(encoding="utf-8")


def test_project_merge_mode_rejects_unknown_value(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "demo.toml"
    config_path.write_text(
        """
[project]
id = "demo"

[tracker]
repository = "owner/repo"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "resolve_or_create_project_config",
        lambda *_args, **_kwargs: config_path,
    )

    with pytest.raises(typer.BadParameter, match="automatic or manual"):
        cli_module.project_merge_mode("demo", selected="always")
