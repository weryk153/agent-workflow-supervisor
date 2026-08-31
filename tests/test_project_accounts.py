import json
import subprocess
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_workflow_supervisor.locking as locking_module
import agent_workflow_supervisor.project_accounts as project_accounts_module
from agent_workflow_supervisor.project_accounts import (
    _claude_worker_model,
    _ensure_execution_project,
    _github_repository,
    _install_orchestrator_rules,
    _new_project_config,
    _reconcile_default_account_project,
    bind_ao_project_account,
    schedule_ao_project_account_switch,
    set_project_accounts,
)
from agent_workflow_supervisor.registry import AccountRecord


@pytest.fixture(autouse=True)
def isolated_lock_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(locking_module, "LOCK_ROOT", tmp_path / "locks")


def test_generated_project_config_starts_in_shadow_mode() -> None:
    document = _new_project_config(
        "demo",
        repository="owner/repository",
        ao_command="ao",
    )

    assert document["supervisor"]["shadow_mode"] is True
    assert document["project"]["id"] == "demo"
    assert document["policy"]["capacity"]["claude-code"] == 1
    assert "codex" not in document["policy"]["capacity"]
    assert "models" not in document["policy"]


@pytest.mark.parametrize(
    "remote",
    [
        "https://notgithub.com/owner/repo.git",
        "https://github.com.evil.example/owner/repo.git",
        "ssh://evil.example/path/github.com/owner/repo.git",
        "https://evil.example/github.com/owner/repo.git",
    ],
)
def test_github_repository_parser_rejects_lookalike_hosts(remote: str) -> None:
    with pytest.raises(ValueError, match="only GitHub remotes"):
        _github_repository(remote)


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
        "github.com/owner/repo",
    ],
)
def test_github_repository_parser_accepts_exact_github_host(remote: str) -> None:
    assert _github_repository(remote) == "owner/repo"


class ExistingExecutionProjectAdapter:
    def __init__(self, execution_path: Path) -> None:
        self.execution_path = execution_path
        self.calls: list[tuple[str, ...]] = []
        self.saved_config: dict | None = None

    def run_json(self, *args: str) -> dict:
        self.calls.append(args)
        if args[:3] == ("project", "get", "demo-claude-work"):
            return {
                "project": {
                    "id": "demo-claude-work",
                    "path": str(self.execution_path),
                    "config": {
                        "env": {"CLAUDE_CONFIG_DIR": "/stale"},
                        "worker": {
                            "agent": "codex",
                            "agentConfig": {
                                "model": "stale-model",
                                "permissions": "bypass-permissions",
                            },
                        },
                    },
                }
            }
        if args[:3] == ("project", "set-config", "demo-claude-work"):
            self.saved_config = json.loads(args[args.index("--config-json") + 1])
            return {"project": {"id": "demo-claude-work"}}
        raise AssertionError(args)

    def run(self, *args: str) -> str:
        self.calls.append(args)
        raise AssertionError(f"existing execution project should not be re-added: {args}")


def test_existing_execution_project_is_reconciled_to_current_account_policy(
    monkeypatch, tmp_path: Path
) -> None:
    data_root = tmp_path / "data"
    execution_path = data_root / "execution-projects" / "demo" / "work"
    (execution_path / ".git").mkdir(parents=True)
    config_dir = tmp_path / "claude-work"
    config_dir.mkdir()
    monkeypatch.setattr(project_accounts_module, "DATA_ROOT", data_root)
    monkeypatch.setattr(
        project_accounts_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="git@github.com:owner/repo.git\n"
        ),
    )
    adapter = ExistingExecutionProjectAdapter(execution_path)
    base_project = {
        "id": "demo",
        "repo": "https://github.com/owner/repo.git",
        "defaultBranch": "main",
        "config": {
            "env": {"KEEP": "yes"},
            "agentConfig": {"permissions": "accept-edits"},
            "worker": {"agent": "codex"},
        },
    }

    result = _ensure_execution_project(
        ao=adapter,  # type: ignore[arg-type]
        base_project=base_project,
        account=AccountRecord("work", config_dir=config_dir),
        model="claude-sonnet-5",
    )

    assert result == "demo-claude-work"
    assert adapter.saved_config is not None
    assert adapter.saved_config["env"] == {
        "KEEP": "yes",
        "CLAUDE_CONFIG_DIR": str(config_dir.resolve()),
    }
    assert adapter.saved_config["agentConfig"]["permissions"] == "accept-edits"
    assert adapter.saved_config["worker"] == {
        "agent": "claude-code",
        "agentConfig": {"model": "claude-sonnet-5"},
    }


def test_execution_checkout_remote_mismatch_fails_closed(monkeypatch, tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    execution_path = data_root / "execution-projects" / "demo" / "work"
    (execution_path / ".git").mkdir(parents=True)
    config_dir = tmp_path / "claude-work"
    config_dir.mkdir()
    monkeypatch.setattr(project_accounts_module, "DATA_ROOT", data_root)
    monkeypatch.setattr(
        project_accounts_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout="https://github.com/other/wrong.git\n"
        ),
    )
    adapter = ExistingExecutionProjectAdapter(execution_path)

    with pytest.raises(RuntimeError, match="origin mismatch"):
        _ensure_execution_project(
            ao=adapter,  # type: ignore[arg-type]
            base_project={
                "id": "demo",
                "repo": "https://github.com/owner/repo.git",
                "config": {},
            },
            account=AccountRecord("work", config_dir=config_dir),
            model=None,
        )


def test_default_claude_model_uses_effective_registry_profile() -> None:
    config = SimpleNamespace(
        policy=SimpleNamespace(
            default_model_profile="claude-default",
            model_profiles={
                "claude-default": SimpleNamespace(harness="claude-code", model="claude-sonnet-5")
            },
            models={"claude-code": "legacy-model"},
        )
    )

    assert _claude_worker_model(config) == "claude-sonnet-5"


class DefaultAccountAdapter:
    def __init__(self) -> None:
        self.saved_config: dict | None = None

    def run_json(self, *args: str) -> dict:
        if args[:3] == ("project", "set-config", "demo"):
            self.saved_config = json.loads(args[args.index("--config-json") + 1])
            return {"project": {"id": "demo"}}
        raise AssertionError(args)


def test_default_account_clears_stale_direct_claude_binding() -> None:
    adapter = DefaultAccountAdapter()
    base_project = {
        "id": "demo",
        "config": {
            "env": {"CLAUDE_CONFIG_DIR": "/stale/work-login", "KEEP": "yes"},
            "worker": {"agent": "claude-code"},
        },
    }

    updated = _reconcile_default_account_project(adapter, base_project)  # type: ignore[arg-type]

    assert adapter.saved_config is not None
    assert adapter.saved_config["env"] == {"KEEP": "yes"}
    assert updated["config"] == adapter.saved_config
    assert locking_module.execution_credential_identity("demo") == (True, None)


class AccountPolicyAdapter:
    def __init__(self, _command: str) -> None:
        pass

    def run_json(self, *args: str) -> dict:
        if args[:3] == ("project", "get", "demo"):
            return {
                "project": {
                    "id": "demo",
                    "repo": "https://github.com/example/demo.git",
                    "config": {},
                }
            }
        raise AssertionError(args)

    def run(self, *args: str) -> str:
        if args[:3] == ("project", "set-config", "demo"):
            return "updated"
        raise AssertionError(args)


def test_account_policy_retains_retired_execution_profile(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "demo.toml"
    config_path.write_text(
        """
[supervisor]
shadow_mode = false

[project]
id = "demo"

[tracker]
repository = "example/demo"

[credentials.profiles.claude-old]
execution_project_id = "demo-claude-old"
max_workers = 1

[policy]
default_harness = "claude-code"

[policy.capacity]
claude-code = 2

[policy.credential_profiles]
claude-code = ["claude-old"]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(project_accounts_module, "CommandAdapter", AccountPolicyAdapter)
    monkeypatch.setattr(
        project_accounts_module,
        "get_account",
        lambda _name, _path: AccountRecord("default"),
    )
    monkeypatch.setattr(
        project_accounts_module,
        "_auth_status",
        lambda _path: {"logged_in": True, "email": None, "organization": None},
    )

    set_project_accounts(
        "demo",
        ["default"],
        config_path=config_path,
        registry_path=tmp_path / "registry.toml",
    )

    saved = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert saved["credentials"]["profiles"]["claude-old"]["execution_project_id"] == (
        "demo-claude-old"
    )
    assert saved["credentials"]["profiles"]["claude-default"]["execution_project_id"] == "demo"
    assert saved["policy"]["credential_profiles"]["claude-code"] == ["claude-default"]


class FakeAoCommandAdapter:
    instance = None

    def __init__(self, _command: str) -> None:
        self.calls: list[tuple[str, ...]] = []
        FakeAoCommandAdapter.instance = self

    def run_json(self, *args: str) -> dict:
        self.calls.append(args)
        if args[:3] == ("project", "get", "demo"):
            return {"project": {"id": "demo", "config": {"worker": {"agent": "claude-code"}}}}
        if args[:2] == ("session", "ls"):
            return {
                "data": [
                    {
                        "id": "demo-1",
                        "role": "orchestrator",
                        "isTerminated": False,
                    }
                ]
            }
        if args[:3] == ("project", "set-config", "demo"):
            return {"project": {"id": "demo"}}
        raise AssertionError(args)

    def run(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "spawn":
            return "spawned session demo-2 (idle)"
        if args[:2] == ("session", "kill"):
            return "session killed"
        raise AssertionError(args)


def test_bind_project_account_replaces_orchestrator_after_config_is_saved(monkeypatch) -> None:
    work_dir = Path("/tmp/.claude-work")
    monkeypatch.setattr(
        project_accounts_module,
        "get_account",
        lambda _name, _path: AccountRecord("work", config_dir=work_dir),
    )
    monkeypatch.setattr(
        project_accounts_module,
        "_auth_status",
        lambda _path: {
            "logged_in": True,
            "email": "developer@example.com",
            "organization": "Example",
        },
    )
    monkeypatch.setattr(project_accounts_module, "CommandAdapter", FakeAoCommandAdapter)

    result = bind_ao_project_account(
        "demo",
        "work",
        ao_command="ao",
        restart_orchestrator=True,
    )

    adapter = FakeAoCommandAdapter.instance
    assert adapter is not None
    config_call = next(call for call in adapter.calls if call[:2] == ("project", "set-config"))
    saved = json.loads(config_call[config_call.index("--config-json") + 1])
    assert saved["env"]["CLAUDE_CONFIG_DIR"] == str(work_dir.resolve())
    config_index = adapter.calls.index(config_call)
    kill_index = next(
        index for index, call in enumerate(adapter.calls) if call[:2] == ("session", "kill")
    )
    spawn_index = next(index for index, call in enumerate(adapter.calls) if call[0] == "spawn")
    assert config_index < kill_index < spawn_index
    assert result["orchestrator_session"] == "demo-2"
    assert result["replaced_orchestrators"] == ["demo-1"]
    assert result["active_sessions_still_using_previous_account"] == []
    assert locking_module.execution_credential_identity("demo") == (
        True,
        str(work_dir.resolve()),
    )


class MultipleOrchestratorsAdapter:
    instance = None

    def __init__(self, _command: str) -> None:
        self.calls: list[tuple[str, ...]] = []
        MultipleOrchestratorsAdapter.instance = self

    def run_json(self, *args: str) -> dict:
        self.calls.append(args)
        if args[:3] == ("project", "get", "demo"):
            return {"project": {"id": "demo", "config": {"worker": {"agent": "claude-code"}}}}
        if args[:2] == ("session", "ls"):
            return {
                "data": [
                    {"id": "demo-1", "role": "orchestrator", "isTerminated": False},
                    {"id": "demo-2", "role": "orchestrator", "isTerminated": False},
                ]
            }
        if args[:3] == ("project", "set-config", "demo"):
            return {"project": {"id": "demo"}}
        raise AssertionError(args)

    def run(self, *args: str) -> str:
        self.calls.append(args)
        if args[:2] == ("session", "kill"):
            return "session killed"
        if args[0] == "spawn":
            return "spawned session demo-3 (idle)"
        raise AssertionError(args)


def _prepare_bound_account(monkeypatch) -> None:
    monkeypatch.setattr(
        project_accounts_module,
        "get_account",
        lambda _name, _path: AccountRecord("work", config_dir=Path("/tmp/.claude-work")),
    )
    monkeypatch.setattr(
        project_accounts_module,
        "_auth_status",
        lambda _path: {"logged_in": True, "email": None, "organization": None},
    )
    monkeypatch.setattr(
        project_accounts_module,
        "CommandAdapter",
        MultipleOrchestratorsAdapter,
    )


def test_bind_project_account_restarts_only_selected_orchestrator(monkeypatch) -> None:
    _prepare_bound_account(monkeypatch)

    result = bind_ao_project_account(
        "demo",
        "work",
        ao_command="ao",
        restart_orchestrator=True,
        orchestrator_session_id="demo-2",
    )

    adapter = MultipleOrchestratorsAdapter.instance
    assert adapter is not None
    kill_calls = [call for call in adapter.calls if call[:2] == ("session", "kill")]
    assert kill_calls == [("session", "kill", "demo-2", "--project", "demo")]
    spawn_call = next(call for call in adapter.calls if call[0] == "spawn")
    replacement_name = spawn_call[spawn_call.index("--name") + 1]
    assert replacement_name.startswith("orchestrator-")
    assert replacement_name != "orchestrator"
    assert result["replaced_orchestrators"] == ["demo-2"]
    assert result["orchestrator_session"] == "demo-3"
    assert result["active_sessions_still_using_previous_account"] == ["demo-1"]


def test_bind_project_account_requires_target_before_mutating_multiple_orchestrators(
    monkeypatch,
) -> None:
    _prepare_bound_account(monkeypatch)

    with pytest.raises(RuntimeError, match="select one explicitly with --session"):
        bind_ao_project_account(
            "demo",
            "work",
            ao_command="ao",
            restart_orchestrator=True,
        )

    adapter = MultipleOrchestratorsAdapter.instance
    assert adapter is not None
    assert not any(call[:3] == ("project", "set-config", "demo") for call in adapter.calls)
    assert not any(call[:2] == ("session", "kill") for call in adapter.calls)


class ConcurrentAccountSwitchAdapter:
    current_config: dict = {}
    spawn_bindings: list[str] = []
    spawn_count = 0

    def __init__(self, _command: str) -> None:
        pass

    @classmethod
    def reset(cls) -> None:
        cls.current_config = {}
        cls.spawn_bindings = []
        cls.spawn_count = 0

    def run_json(self, *args: str) -> dict:
        if args[:3] == ("project", "get", "demo"):
            return {
                "project": {
                    "id": "demo",
                    "path": "/tmp/demo",
                    "config": json.loads(json.dumps(self.current_config)),
                }
            }
        if args[:2] == ("session", "ls"):
            return {
                "data": [
                    {"id": "source-a", "role": "orchestrator", "isTerminated": False},
                    {"id": "source-b", "role": "orchestrator", "isTerminated": False},
                ]
            }
        if args[:3] == ("project", "set-config", "demo"):
            type(self).current_config = json.loads(args[args.index("--config-json") + 1])
            time.sleep(0.05)
            return {"project": {"id": "demo"}}
        raise AssertionError(args)

    def run(self, *args: str) -> str:
        if args[:2] == ("session", "kill"):
            time.sleep(0.02)
            return "session killed"
        if args[0] == "spawn":
            binding = str(type(self).current_config["env"]["CLAUDE_CONFIG_DIR"])
            type(self).spawn_bindings.append(binding)
            type(self).spawn_count += 1
            return f"spawned session replacement-{type(self).spawn_count} (idle)"
        raise AssertionError(args)


def test_concurrent_account_switches_hold_binding_through_replacement_spawn(
    monkeypatch, tmp_path: Path
) -> None:
    ConcurrentAccountSwitchAdapter.reset()
    account_dirs = {name: tmp_path / name for name in ("account-a", "account-b")}
    monkeypatch.setattr(
        project_accounts_module,
        "get_account",
        lambda name, _path: AccountRecord(name, config_dir=account_dirs[name]),
    )
    monkeypatch.setattr(
        project_accounts_module,
        "_auth_status",
        lambda _path: {"logged_in": True, "email": None, "organization": None},
    )
    monkeypatch.setattr(
        project_accounts_module,
        "CommandAdapter",
        ConcurrentAccountSwitchAdapter,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                bind_ao_project_account,
                "demo",
                account,
                ao_command="ao",
                restart_orchestrator=True,
                orchestrator_session_id=source,
            )
            for account, source in (
                ("account-a", "source-a"),
                ("account-b", "source-b"),
            )
        ]
        results = [future.result() for future in futures]

    assert {result["account"] for result in results} == {"account-a", "account-b"}
    assert sorted(ConcurrentAccountSwitchAdapter.spawn_bindings) == sorted(
        str(path.resolve()) for path in account_dirs.values()
    )


class RuleCommandAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> str:
        self.calls.append(args)
        return "updated"


def test_managed_ao_rules_are_ao_first_and_preserve_operator_rules() -> None:
    adapter = RuleCommandAdapter()
    raw_config = {
        "orchestratorRules": (
            "Operator-owned rule.\n\n[LANGGRAPH_SUPERVISOR]\nobsolete managed rule"
        )
    }

    _install_orchestrator_rules(adapter, "demo", raw_config)  # type: ignore[arg-type]
    rules = raw_config["orchestratorRules"]

    assert rules.startswith("Operator-owned rule.")
    assert "Treat this AO conversation as the primary user interface" in rules
    assert "project switch-account demo --use ACCOUNT" in rules
    assert "Never run bind-account --restart from inside the session" in rules
    assert rules.count("[LANGGRAPH_SUPERVISOR]") == 1
    assert rules.count("[/LANGGRAPH_SUPERVISOR]") == 1
    assert "obsolete managed rule" not in rules


class SwitchCommandAdapter:
    def __init__(self, _command: str) -> None:
        pass

    def run_json(self, *args: str) -> dict:
        if args[:2] == ("session", "get"):
            return {"session": {"id": "demo-1", "kind": "orchestrator"}}
        if args[:2] == ("session", "ls"):
            return {"data": []}
        raise AssertionError(args)


class FakeProcess:
    pid = 4321


def test_switch_account_schedules_detached_replacement(monkeypatch, tmp_path: Path) -> None:
    launched: list[list[str]] = []
    monkeypatch.setattr(project_accounts_module, "CommandAdapter", SwitchCommandAdapter)
    monkeypatch.setattr(project_accounts_module, "DATA_ROOT", tmp_path)

    def fake_bind(*_args, **kwargs):
        locking_module.mark_account_switch_pending("demo", kwargs["pending_switch_id"])
        return {"applies_to": "future_sessions"}

    monkeypatch.setattr(project_accounts_module, "bind_ao_project_account", fake_bind)

    def fake_popen(command: list[str], **_kwargs) -> FakeProcess:
        launched.append(command)
        return FakeProcess()

    monkeypatch.setattr(project_accounts_module.subprocess, "Popen", fake_popen)

    result = schedule_ao_project_account_switch(
        "demo",
        "work",
        ao_command="ao",
        source_session_id="demo-1",
    )

    assert result["scheduled"] is True
    assert result["source_session"] == "demo-1"
    assert result["helper_pid"] == 4321
    assert launched
    assert "agent_workflow_supervisor.account_switch" in launched[0]
    assert launched[0][launched[0].index("--source-session") + 1] == "demo-1"
    assert launched[0][launched[0].index("--switch-id") + 1] == result["switch_id"]


def test_scheduled_switch_rejects_uncertain_worker_reservation(monkeypatch) -> None:
    monkeypatch.setattr(project_accounts_module, "CommandAdapter", SwitchCommandAdapter)
    with locking_module.work_item_acquisition_lock("demo", "17"):
        locking_module.record_pending_acquisition(
            "demo",
            "17",
            execution_project_id="demo-claude-work",
            harness="claude-code",
        )

    with pytest.raises(RuntimeError, match="worker acquisitions are reserved.*17"):
        schedule_ao_project_account_switch(
            "demo",
            "work",
            ao_command="ao",
            source_session_id="demo-1",
        )

    assert not locking_module.account_switch_pending("demo")


class WorkerAppearsDuringSwitchAdapter:
    session_scans = 0

    def __init__(self, _command: str) -> None:
        pass

    def run_json(self, *args: str) -> dict:
        if args[:2] == ("session", "get"):
            return {"session": {"id": "demo-1", "kind": "orchestrator"}}
        if args[:3] == ("project", "get", "demo"):
            return {"project": {"id": "demo", "config": {}}}
        if args[:2] == ("session", "ls"):
            type(self).session_scans += 1
            if type(self).session_scans == 1:
                return {"data": []}
            return {
                "data": [
                    {"id": "demo-1", "role": "orchestrator", "isTerminated": False},
                    {"id": "worker-race", "role": "worker", "isTerminated": False},
                ]
            }
        if args[:3] == ("project", "set-config", "demo"):
            raise AssertionError("binding must not change after a worker appears")
        raise AssertionError(args)


def test_scheduled_switch_rechecks_workers_under_allocation_lock(
    monkeypatch, tmp_path: Path
) -> None:
    WorkerAppearsDuringSwitchAdapter.session_scans = 0
    monkeypatch.setattr(project_accounts_module, "CommandAdapter", WorkerAppearsDuringSwitchAdapter)
    monkeypatch.setattr(project_accounts_module, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(
        project_accounts_module,
        "get_account",
        lambda _name, _path: AccountRecord("work", config_dir=tmp_path / "claude-work"),
    )
    monkeypatch.setattr(
        project_accounts_module,
        "_auth_status",
        lambda _path: {"logged_in": True, "email": None, "organization": None},
    )

    with pytest.raises(RuntimeError, match="active workers exist: worker-race"):
        schedule_ao_project_account_switch(
            "demo",
            "work",
            ao_command="ao",
            source_session_id="demo-1",
        )

    assert not locking_module.account_switch_pending("demo")
