import signal
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

import agent_workflow_supervisor.service as service_module
from agent_workflow_supervisor.config import (
    AppConfig,
    ProjectConfig,
    SupervisorConfig,
    TrackerConfig,
)
from agent_workflow_supervisor.models import AgentSession
from agent_workflow_supervisor.service import (
    JobStore,
    deliver_pending_notifications,
    reconcile_job,
)


def _service_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        supervisor=SupervisorConfig(runtime_dir=tmp_path),
        project=ProjectConfig(id="demo"),
        tracker=TrackerConfig(repository="example/demo"),
    )


def _service_record(config: AppConfig, *, pid: int = 1234) -> service_module.ServiceRecord:
    return service_module.ServiceRecord(
        pid=pid,
        token="instance-token",
        project_id=config.project.id,
        config_path=str((config.supervisor.runtime_dir / "demo.toml").resolve()),
    )


def test_service_running_requires_verified_process_identity(monkeypatch, tmp_path: Path) -> None:
    config = _service_config(tmp_path)
    record = _service_record(config)
    service_module._write_service_record(config, record)
    monkeypatch.setattr(service_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(
        service_module,
        "_process_command_line",
        lambda _pid: (
            "python -m agent_workflow_supervisor.service "
            f"--config {record.config_path} --instance-token {record.token}"
        ),
    )

    assert service_module.service_running(config)

    monkeypatch.setattr(service_module, "_process_command_line", lambda _pid: "python unrelated.py")
    assert not service_module.service_running(config)


def test_stop_service_never_signals_unverified_live_pid(monkeypatch, tmp_path: Path) -> None:
    config = _service_config(tmp_path)
    service_module._write_service_record(config, _service_record(config))
    monkeypatch.setattr(service_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(service_module, "_process_command_line", lambda _pid: "python unrelated.py")

    def unexpected_kill(_pid: int, _signal: int) -> None:
        raise AssertionError("unverified process must not be signalled")

    monkeypatch.setattr(service_module.os, "kill", unexpected_kill)

    with pytest.raises(RuntimeError, match="refusing to signal unverified pid"):
        service_module.stop_service(config, timeout_seconds=0)


def test_stop_service_reports_timeout_after_sigterm(monkeypatch, tmp_path: Path) -> None:
    config = _service_config(tmp_path)
    record = _service_record(config)
    service_module._write_service_record(config, record)
    monkeypatch.setattr(service_module, "process_alive", lambda _pid: True)
    monkeypatch.setattr(
        service_module,
        "_process_command_line",
        lambda _pid: (
            "python -m agent_workflow_supervisor.service "
            f"--config {record.config_path} --instance-token {record.token}"
        ),
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(service_module.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    with pytest.raises(TimeoutError, match="did not stop"):
        service_module.stop_service(config, timeout_seconds=0)

    assert signals == [(record.pid, signal.SIGTERM)]


def test_service_lifetime_lock_is_exclusive(tmp_path: Path) -> None:
    config = _service_config(tmp_path)

    with service_module._service_lifetime_lock(config):
        with pytest.raises(RuntimeError, match="lock is already held"):
            with service_module._service_lifetime_lock(config):
                pass


def test_concurrent_service_starts_converge_on_one_daemon(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "concurrent.toml"
    config_path.write_text(
        f"""
[supervisor]
database_path = "{tmp_path / "checkpoints.sqlite"}"
runtime_dir = "{tmp_path / "runtime"}"
poll_interval_seconds = 1
shadow_mode = true

[project]
id = "concurrent-service-test"

[runner]
type = "ao"
command = "ao"

[tracker]
type = "github"
command = "gh"
repository = "example/demo"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(service_module, "ensure_ao_ready", lambda _config: None)
    config = service_module.load_config(config_path, registry_path=service_module.REGISTRY_PATH)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            pids = list(executor.map(service_module.start_service, [config_path, config_path]))

        assert pids[0] == pids[1]
        assert service_module.service_running(config)
    finally:
        if service_module.service_running(config):
            service_module.stop_service(config)


def test_process_service_start_does_not_check_ao(monkeypatch, tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://github.com/example/demo.git",
        ],
        check=True,
        capture_output=True,
    )
    config_path = tmp_path / "process.toml"
    config_path.write_text(
        f"""
[supervisor]
database_path = "{tmp_path / "checkpoints.sqlite"}"
runtime_dir = "{tmp_path / "runtime"}"
poll_interval_seconds = 1

[project]
id = "process-service-test"

[runner]
type = "process"
repository_path = "{tmp_path}"
worktree_root = "{tmp_path / "worktrees"}"

[tracker]
repository = "example/demo"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        service_module,
        "ensure_ao_ready",
        lambda *_args, **_kwargs: pytest.fail("process service must not check AO"),
    )
    config = service_module.load_config(config_path, registry_path=service_module.REGISTRY_PATH)

    try:
        pid = service_module.start_service(config_path)
        assert pid > 0
        assert service_module.service_running(config)
    finally:
        if service_module.service_running(config):
            service_module.stop_service(config)


def test_existing_job_database_migrates_approval_binding_columns(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE supervisor_jobs (
                work_item_id TEXT PRIMARY KEY,
                active INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'queued',
                approval TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO supervisor_jobs (
                work_item_id, active, status, approval, last_error, created_at, updated_at
            ) VALUES ('legacy', 1, 'queued', 'approve', NULL, 'before', 'before')
            """
        )

    store = JobStore(tmp_path)
    job = store.dispatch("42")
    legacy = store.get("legacy")

    assert job.approval_change_id is None
    assert job.approval_target_sha is None
    assert job.origin_session_id is None
    assert job.notification_key is None
    assert job.notified_key is None
    assert job.notification_error is None
    assert legacy is not None
    assert legacy.approval is None


def test_dispatch_queue_is_idempotent_and_rejects_early_approval(tmp_path: Path) -> None:
    store = JobStore(tmp_path)

    first = store.dispatch("42", origin_session_id="orchestrator-1")
    second = store.dispatch("42")

    assert first.work_item_id == "42"
    assert second.created_at == first.created_at
    assert second.origin_session_id == "orchestrator-1"
    assert len(store.list()) == 1
    with pytest.raises(ValueError, match="not waiting for approval"):
        store.approve("42", "approve", change_id="8", target_sha="abc")


class NotificationRunner:
    instance = None

    def __init__(self, _command: str, *, repository: str | None = None) -> None:
        self.repository = repository
        self.sent: list[tuple[str, str]] = []
        self.preferred = AgentSession(
            "orchestrator-1",
            "orchestrator",
            "idle",
            "claude-code",
            project_id="demo",
        )
        self.sessions = [self.preferred]
        NotificationRunner.instance = self

    def get_session(self, _session_id: str) -> AgentSession | None:
        return self.preferred

    def list_sessions(self, _project_id: str) -> list[AgentSession]:
        return self.sessions

    def send(self, session_id: str, message: str) -> bool:
        self.sent.append((session_id, message))
        return True


def test_completed_job_notifies_origin_orchestrator_once(monkeypatch, tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.dispatch("42", origin_session_id="orchestrator-1")
    store.update_result("42", status="completed", active=False)
    runner = NotificationRunner("ao", repository="example/demo")
    monkeypatch.setattr(service_module, "AoRunner", lambda *_args, **_kwargs: runner)

    deliver_pending_notifications(_service_config(tmp_path), store)
    deliver_pending_notifications(_service_config(tmp_path), store)

    assert len(runner.sent) == 1
    assert runner.sent[0][0] == "orchestrator-1"
    message = runner.sent[0][1]
    assert "completed successfully" in message
    assert "oa status --project demo --work-item 42" in message
    notified = store.get("42")
    assert notified is not None
    assert notified.notified_key == "completed"
    assert notified.notification_error is None


def test_notification_falls_back_to_only_active_orchestrator(monkeypatch, tmp_path: Path) -> None:
    class ReplacementRunner(NotificationRunner):
        def __init__(self, command: str, *, repository: str | None = None) -> None:
            super().__init__(command, repository=repository)
            self.preferred = AgentSession(
                "orchestrator-old",
                "orchestrator",
                "terminated",
                "claude-code",
                terminated=True,
                project_id="demo",
            )
            self.sessions = [
                self.preferred,
                AgentSession(
                    "orchestrator-new",
                    "orchestrator",
                    "idle",
                    "claude-code",
                    project_id="demo",
                ),
            ]
            ReplacementRunner.instance = self

    store = JobStore(tmp_path)
    store.dispatch("42", origin_session_id="orchestrator-old")
    store.update_result("42", status="completed", active=False)
    runner = ReplacementRunner("ao", repository="example/demo")
    monkeypatch.setattr(service_module, "AoRunner", lambda *_args, **_kwargs: runner)

    deliver_pending_notifications(_service_config(tmp_path), store)

    assert [session_id for session_id, _message in runner.sent] == ["orchestrator-new"]


def test_new_approval_head_creates_a_new_notification(monkeypatch, tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.dispatch("42", origin_session_id="orchestrator-1")
    runner = NotificationRunner("ao", repository="example/demo")
    monkeypatch.setattr(service_module, "AoRunner", lambda *_args, **_kwargs: runner)

    store.update_result(
        "42",
        status="awaiting_approval",
        active=True,
        approval_gate=("8", "A"),
    )
    deliver_pending_notifications(_service_config(tmp_path), store)
    store.update_result(
        "42",
        status="awaiting_approval",
        active=True,
        approval_gate=("8", "B"),
    )
    deliver_pending_notifications(_service_config(tmp_path), store)

    assert len(runner.sent) == 2
    assert "head A" in runner.sent[0][1]
    assert "head B" in runner.sent[1][1]


def test_ambiguous_notification_target_is_persisted_without_changing_workflow(
    monkeypatch, tmp_path: Path
) -> None:
    class AmbiguousRunner(NotificationRunner):
        def __init__(self, command: str, *, repository: str | None = None) -> None:
            super().__init__(command, repository=repository)
            self.preferred = AgentSession(
                "orchestrator-old",
                "orchestrator",
                "terminated",
                "claude-code",
                terminated=True,
                project_id="demo",
            )
            self.sessions = [
                AgentSession(
                    session_id,
                    "orchestrator",
                    "idle",
                    "claude-code",
                    project_id="demo",
                )
                for session_id in ("orchestrator-a", "orchestrator-b")
            ]

    store = JobStore(tmp_path)
    store.dispatch("42", origin_session_id="orchestrator-old")
    store.update_result("42", status="completed", active=False)
    runner = AmbiguousRunner("ao", repository="example/demo")
    monkeypatch.setattr(service_module, "AoRunner", lambda *_args, **_kwargs: runner)

    deliver_pending_notifications(_service_config(tmp_path), store)

    failed = store.get("42")
    assert failed is not None
    assert failed.status == "completed"
    assert failed.notified_key is None
    assert "cannot choose a replacement orchestrator" in str(failed.notification_error)
    assert runner.sent == []


def test_approval_is_bound_to_current_change_and_head(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.dispatch("42")
    store.update_result(
        "42",
        status="awaiting_approval",
        active=True,
        approval_gate=("8", "abc"),
    )

    with pytest.raises(ValueError, match="not waiting for approval"):
        store.approve("42", "approve", change_id="8", target_sha="different")

    approved = store.approve("42", "approve", change_id="8", target_sha="abc")

    assert approved.approval == "approve"
    assert approved.approval_change_id == "8"
    assert approved.approval_target_sha == "abc"
    assert approved.active

    repeated = store.approve("42", "approve", change_id="8", target_sha="abc")
    assert repeated.approval == "approve"
    with pytest.raises(ValueError, match="already queued"):
        store.approve("42", "reject", change_id="8", target_sha="abc")

    claimed = store.claim_approval("42", change_id="8", target_sha="abc")
    assert claimed == "approve"
    assert store.claim_approval("42", change_id="8", target_sha="abc") is None
    after_claim = store.get("42")
    assert after_claim is not None
    assert after_claim.approval is None


class Snapshot:
    next: tuple[str, ...] = ()
    values: dict = {}


class NewGateGraph:
    def get_state(self, _invocation: dict) -> Snapshot:
        return Snapshot()

    def invoke(self, _input: dict, *, config: dict) -> dict:
        return {
            "__interrupt__": True,
            "project_id": "demo",
            "work_item_id": "42",
            "change_id": "8",
            "change_head_sha": "B",
            "status": "awaiting_approval",
        }


class StalledGraph:
    def get_state(self, _invocation: dict) -> Snapshot:
        return Snapshot()

    def invoke(self, _input: dict, *, config: dict) -> dict:
        return {
            "project_id": "demo",
            "work_item_id": "42",
            "status": "review_stalled",
            "last_error": "review timed out after 2 attempt(s)",
        }


def test_stale_claim_cannot_rebind_to_new_gate(monkeypatch, tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    store.dispatch("42")
    store.update_result(
        "42",
        status="awaiting_approval",
        active=True,
        approval_gate=("8", "A"),
    )
    store.approve("42", "approve", change_id="8", target_sha="A")
    stale_job = store.get("42")
    assert stale_job is not None

    @contextmanager
    def fake_graph_runtime(_config: AppConfig):
        yield NewGateGraph()

    monkeypatch.setattr(service_module, "graph_runtime", fake_graph_runtime)
    app_config = AppConfig(
        supervisor=SupervisorConfig(runtime_dir=tmp_path),
        project=ProjectConfig(id="demo"),
        tracker=TrackerConfig(repository="example/demo"),
    )

    reconcile_job(app_config, store, stale_job)

    rebound = store.get("42")
    assert rebound is not None
    assert rebound.status == "awaiting_approval"
    assert rebound.approval is None
    assert rebound.approval_change_id == "8"
    assert rebound.approval_target_sha == "B"


def test_graph_liveness_error_is_exposed_by_service_status(monkeypatch, tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = store.dispatch("42")

    @contextmanager
    def fake_graph_runtime(_config: AppConfig):
        yield StalledGraph()

    monkeypatch.setattr(service_module, "graph_runtime", fake_graph_runtime)
    reconcile_job(_service_config(tmp_path), store, job)

    stalled = store.get("42")
    assert stalled is not None
    assert stalled.active
    assert stalled.status == "review_stalled"
    assert stalled.last_error == "review timed out after 2 attempt(s)"


def test_invalid_legacy_work_item_id_is_persisted_as_retry_error(tmp_path: Path) -> None:
    store = JobStore(tmp_path)
    job = store.dispatch("not-a-github-issue")

    reconcile_job(_service_config(tmp_path), store, job)

    failed = store.get(job.work_item_id)
    assert failed is not None
    assert failed.status == "retrying_after_error"
    assert "unsupported GitHub issue reference" in str(failed.last_error)
