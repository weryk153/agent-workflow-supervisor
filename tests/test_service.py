import signal
import sqlite3
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
from agent_workflow_supervisor.service import JobStore, reconcile_job


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
    assert legacy is not None
    assert legacy.approval is None


def test_dispatch_queue_is_idempotent_and_rejects_early_approval(tmp_path: Path) -> None:
    store = JobStore(tmp_path)

    first = store.dispatch("42")
    second = store.dispatch("42")

    assert first.work_item_id == "42"
    assert second.created_at == first.created_at
    assert len(store.list()) == 1
    with pytest.raises(ValueError, match="not waiting for approval"):
        store.approve("42", "approve", change_id="8", target_sha="abc")


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
