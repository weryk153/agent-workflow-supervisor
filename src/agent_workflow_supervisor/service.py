"""Background reconciliation service and explicit dispatch queue."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.types import Command

from agent_workflow_supervisor.adapters.ao import AoRunner
from agent_workflow_supervisor.adapters.command import AdapterCommandError, CommandAdapter
from agent_workflow_supervisor.ao_relay import NativeAoRelay
from agent_workflow_supervisor.config import AppConfig, load_config
from agent_workflow_supervisor.identifiers import canonical_github_issue_id
from agent_workflow_supervisor.registry import REGISTRY_PATH
from agent_workflow_supervisor.runtime import graph_runtime, workflow_thread_id

TERMINAL_STATUSES = {
    "approval_rejected",
    "completed",
    "planned_merge",
    "planned_worker",
    "skipped",
}

NOTIFIABLE_STATUSES = TERMINAL_STATUSES | {
    "awaiting_approval",
    "review_stalled",
    "worker_acquisition_pending",
    "worker_reservation_conflict",
    "worker_reservation_unverified",
    "worker_route_conflict",
    "worker_unhealthy",
}

_spawned_children: dict[int, subprocess.Popen[bytes]] = {}
_spawned_children_lock = threading.Lock()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class QueuedWork:
    work_item_id: str
    active: bool
    status: str
    approval: str | None
    approval_change_id: str | None
    approval_target_sha: str | None
    last_error: str | None
    origin_session_id: str | None
    notification_key: str | None
    notified_key: str | None
    notification_error: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ServiceRecord:
    pid: int
    token: str
    project_id: str
    config_path: str


class JobStore:
    def __init__(self, runtime_dir: Path) -> None:
        runtime_dir.mkdir(parents=True, exist_ok=True)
        self.path = runtime_dir / "jobs.sqlite"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except Exception:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
            else:
                with connection:
                    yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS supervisor_jobs (
                    work_item_id TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'queued',
                    approval TEXT,
                    approval_change_id TEXT,
                    approval_target_sha TEXT,
                    last_error TEXT,
                    origin_session_id TEXT,
                    notification_key TEXT,
                    notified_key TEXT,
                    notification_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(supervisor_jobs)").fetchall()
            }
            if "approval_change_id" not in columns:
                connection.execute("ALTER TABLE supervisor_jobs ADD COLUMN approval_change_id TEXT")
            if "approval_target_sha" not in columns:
                connection.execute(
                    "ALTER TABLE supervisor_jobs ADD COLUMN approval_target_sha TEXT"
                )
            for column in (
                "origin_session_id",
                "notification_key",
                "notified_key",
                "notification_error",
            ):
                if column not in columns:
                    connection.execute(f"ALTER TABLE supervisor_jobs ADD COLUMN {column} TEXT")
            # Older releases could queue a decision before these binding
            # columns existed. Such a decision cannot be proven to authorize
            # any current gate, so migration must invalidate it.
            connection.execute(
                """
                UPDATE supervisor_jobs
                SET approval = NULL
                WHERE approval IS NOT NULL
                  AND (approval_change_id IS NULL OR approval_target_sha IS NULL)
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> QueuedWork:
        return QueuedWork(
            work_item_id=str(row["work_item_id"]),
            active=bool(row["active"]),
            status=str(row["status"]),
            approval=str(row["approval"]) if row["approval"] else None,
            approval_change_id=(
                str(row["approval_change_id"]) if row["approval_change_id"] else None
            ),
            approval_target_sha=(
                str(row["approval_target_sha"]) if row["approval_target_sha"] else None
            ),
            last_error=str(row["last_error"]) if row["last_error"] else None,
            origin_session_id=(str(row["origin_session_id"]) if row["origin_session_id"] else None),
            notification_key=(str(row["notification_key"]) if row["notification_key"] else None),
            notified_key=str(row["notified_key"]) if row["notified_key"] else None,
            notification_error=(
                str(row["notification_error"]) if row["notification_error"] else None
            ),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def dispatch(self, work_item_id: str, *, origin_session_id: str | None = None) -> QueuedWork:
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO supervisor_jobs (
                    work_item_id, active, status, approval, last_error,
                    origin_session_id, notification_key, notified_key,
                    notification_error, created_at, updated_at
                ) VALUES (?, 1, 'queued', NULL, NULL, ?, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(work_item_id) DO UPDATE SET
                    active = 1,
                    status = CASE
                        WHEN supervisor_jobs.status IN ('completed', 'skipped')
                        THEN 'queued'
                        ELSE supervisor_jobs.status
                    END,
                    approval = CASE
                        WHEN supervisor_jobs.status IN ('completed', 'skipped')
                        THEN NULL
                        ELSE supervisor_jobs.approval
                    END,
                    approval_change_id = CASE
                        WHEN supervisor_jobs.status IN ('completed', 'skipped')
                        THEN NULL
                        ELSE supervisor_jobs.approval_change_id
                    END,
                    approval_target_sha = CASE
                        WHEN supervisor_jobs.status IN ('completed', 'skipped')
                        THEN NULL
                        ELSE supervisor_jobs.approval_target_sha
                    END,
                    origin_session_id = COALESCE(
                        excluded.origin_session_id,
                        supervisor_jobs.origin_session_id
                    ),
                    notification_key = CASE
                        WHEN supervisor_jobs.status IN ('completed', 'skipped')
                        THEN NULL
                        ELSE supervisor_jobs.notification_key
                    END,
                    notified_key = CASE
                        WHEN supervisor_jobs.status IN ('completed', 'skipped')
                        THEN NULL
                        ELSE supervisor_jobs.notified_key
                    END,
                    notification_error = CASE
                        WHEN supervisor_jobs.status IN ('completed', 'skipped')
                        THEN NULL
                        ELSE supervisor_jobs.notification_error
                    END,
                    last_error = NULL,
                    updated_at = excluded.updated_at
                """,
                (work_item_id, origin_session_id, now, now),
            )
        job = self.get(work_item_id)
        assert job is not None
        return job

    def approve(
        self,
        work_item_id: str,
        decision: str,
        *,
        change_id: str,
        target_sha: str,
    ) -> QueuedWork:
        """Queue a decision only for the exact approval gate currently exposed."""

        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        if not change_id or not target_sha:
            raise ValueError("approval gate must identify a change id and target SHA")
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM supervisor_jobs WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(work_item_id)
            job = self._row(row)
            if (
                not job.active
                or job.status != "awaiting_approval"
                or job.approval_change_id != change_id
                or job.approval_target_sha != target_sha
            ):
                raise ValueError(
                    "workflow is not waiting for approval of "
                    f"change {change_id} at head {target_sha}"
                )
            if job.approval is not None and job.approval != decision:
                raise ValueError("an approval decision is already queued for this gate")
            connection.execute(
                """
                UPDATE supervisor_jobs
                SET active = 1, approval = ?, updated_at = ?
                WHERE work_item_id = ?
                  AND status = 'awaiting_approval'
                  AND approval_change_id = ?
                  AND approval_target_sha = ?
                """,
                (decision, now, work_item_id, change_id, target_sha),
            )
        job = self.get(work_item_id)
        assert job is not None
        return job

    def get(self, work_item_id: str) -> QueuedWork | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM supervisor_jobs WHERE work_item_id = ?",
                (work_item_id,),
            ).fetchone()
        return self._row(row) if row else None

    def claim_approval(self, work_item_id: str, *, change_id: str, target_sha: str) -> str | None:
        """Atomically consume one exact decision before resuming its graph gate.

        A crash after this claim may require the user to approve again, but it
        can never replay the decision against a later gate.
        """

        decision: str | None = None
        with self._transaction(immediate=True) as connection:
            row = connection.execute(
                """
                SELECT approval
                FROM supervisor_jobs
                WHERE work_item_id = ?
                  AND active = 1
                  AND status = 'awaiting_approval'
                  AND approval_change_id = ?
                  AND approval_target_sha = ?
                  AND approval IS NOT NULL
                """,
                (work_item_id, change_id, target_sha),
            ).fetchone()
            if row is not None:
                decision = str(row["approval"])
                connection.execute(
                    """
                    UPDATE supervisor_jobs
                    SET approval = NULL,
                        status = 'approval_resuming',
                        updated_at = ?
                    WHERE work_item_id = ?
                      AND approval_change_id = ?
                      AND approval_target_sha = ?
                    """,
                    (utc_now(), work_item_id, change_id, target_sha),
                )
        return decision

    def clear_approval(self, work_item_id: str) -> None:
        """Invalidate any decision and its binding without changing job state."""

        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE supervisor_jobs
                SET approval = NULL,
                    approval_change_id = NULL,
                    approval_target_sha = NULL,
                    updated_at = ?
                WHERE work_item_id = ?
                """,
                (utc_now(), work_item_id),
            )

    def list(self, *, active_only: bool = False) -> list[QueuedWork]:
        query = "SELECT * FROM supervisor_jobs"
        if active_only:
            query += " WHERE active = 1"
        query += " ORDER BY created_at"
        with self._transaction() as connection:
            rows = connection.execute(query).fetchall()
        return [self._row(row) for row in rows]

    def pending_notifications(self) -> list[QueuedWork]:
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM supervisor_jobs
                WHERE origin_session_id IS NOT NULL
                  AND notification_key IS NOT NULL
                  AND (notified_key IS NULL OR notified_key != notification_key)
                ORDER BY updated_at
                """
            ).fetchall()
        return [self._row(row) for row in rows]

    def mark_notified(self, work_item_id: str, notification_key: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE supervisor_jobs
                SET notified_key = ?, notification_error = NULL
                WHERE work_item_id = ? AND notification_key = ?
                """,
                (notification_key, work_item_id, notification_key),
            )

    def record_notification_error(
        self, work_item_id: str, notification_key: str, error: str
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE supervisor_jobs
                SET notification_error = ?
                WHERE work_item_id = ? AND notification_key = ?
                """,
                (error, work_item_id, notification_key),
            )

    def update_result(
        self,
        work_item_id: str,
        *,
        status: str,
        active: bool,
        last_error: str | None = None,
        clear_approval: bool = False,
        approval_gate: tuple[str, str] | None = None,
    ) -> None:
        if status == "awaiting_approval" and approval_gate is None:
            raise ValueError("awaiting_approval requires a bound change id and target SHA")
        gate_change_id, gate_target_sha = approval_gate or (None, None)
        notification_key = _notification_key(status, approval_gate)
        approval_clause = ", approval = NULL" if clear_approval else ""
        with self._transaction() as connection:
            connection.execute(
                f"""
                UPDATE supervisor_jobs
                SET status = ?, active = ?, last_error = ?, updated_at = ?,
                    approval_change_id = ?, approval_target_sha = ?,
                    notification_key = COALESCE(?, notification_key),
                    notification_error = CASE
                        WHEN ? IS NOT NULL AND notification_key IS NOT ? THEN NULL
                        ELSE notification_error
                    END
                    {approval_clause}
                WHERE work_item_id = ?
                """,
                (
                    status,
                    int(active),
                    last_error,
                    utc_now(),
                    gate_change_id,
                    gate_target_sha,
                    notification_key,
                    notification_key,
                    notification_key,
                    work_item_id,
                ),
            )


def _notification_key(status: str, approval_gate: tuple[str, str] | None = None) -> str | None:
    if status not in NOTIFIABLE_STATUSES:
        return None
    if status == "awaiting_approval":
        change_id, target_sha = approval_gate or ("", "")
        return f"{status}:{change_id}:{target_sha}"
    return status


def _notification_message(config: AppConfig, job: QueuedWork) -> str:
    status_command = f"oa status --project {config.project.id} --work-item {job.work_item_id}"
    if job.status == "awaiting_approval":
        return (
            f"Durable supervisor update for work item #{job.work_item_id}: the workflow is "
            f"waiting for approval of change #{job.approval_change_id} at head "
            f"{job.approval_target_sha}. Inform the user and ask them to explicitly approve "
            f"or reject this exact gate. Run `{status_command}` first if you need the current "
            "details. Do not approve, reject, or redispatch on the user's behalf."
        )
    if job.status == "completed":
        return (
            f"Durable supervisor update for work item #{job.work_item_id}: the workflow "
            f"completed successfully. Run `{status_command}` and summarize the final delivery "
            "for the user, including the pull request and merge result. Do not redispatch it."
        )
    if job.status in {"approval_rejected", "skipped"}:
        return (
            f"Durable supervisor update for work item #{job.work_item_id}: the workflow ended "
            f"with status `{job.status}`. Run `{status_command}` and summarize the outcome for "
            "the user. Do not redispatch it unless the user explicitly asks."
        )
    detail = f" Error: {job.last_error}" if job.last_error else ""
    return (
        f"Durable supervisor update for work item #{job.work_item_id}: the workflow needs "
        f"attention at status `{job.status}`.{detail} Run `{status_command}` and explain the "
        "blocker and safest next action to the user. Do not mutate or redispatch anything "
        "without explicit authorization."
    )


def _notification_target(runner: AoRunner, project_id: str, preferred_session_id: str) -> str:
    preferred = runner.get_session(preferred_session_id)
    if (
        preferred is not None
        and preferred.active
        and preferred.role == "orchestrator"
        and preferred.project_id in {None, project_id}
    ):
        return preferred.id

    active_orchestrators = [
        session
        for session in runner.list_sessions(project_id)
        if session.active and session.role == "orchestrator"
    ]
    if len(active_orchestrators) == 1:
        return active_orchestrators[0].id
    if not active_orchestrators:
        raise RuntimeError(f"no active orchestrator is available for AO project {project_id}")
    identifiers = ", ".join(session.id for session in active_orchestrators)
    raise RuntimeError(
        f"cannot choose a replacement orchestrator for AO project {project_id}: {identifiers}"
    )


def deliver_pending_notifications(config: AppConfig, store: JobStore) -> None:
    if config.runner.type != "ao":
        return
    runner = AoRunner(config.runner.command, repository=config.tracker.repository)
    for job in store.pending_notifications():
        assert job.origin_session_id is not None
        assert job.notification_key is not None
        try:
            target = _notification_target(runner, config.project.id, job.origin_session_id)
            runner.send(target, _notification_message(config, job))
        except Exception as error:
            store.record_notification_error(
                job.work_item_id,
                job.notification_key,
                f"{type(error).__name__}: {error}",
            )
        else:
            store.mark_notified(job.work_item_id, job.notification_key)


def runtime_paths(config: AppConfig) -> tuple[Path, Path]:
    runtime_dir = config.supervisor.runtime_dir
    return runtime_dir / "supervisor.pid", runtime_dir / "supervisor.log"


def _read_service_record(config: AppConfig) -> ServiceRecord | None:
    pid_path, _ = runtime_paths(config)
    try:
        raw = json.loads(pid_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"invalid supervisor ownership record: {pid_path}") from error
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("pid"), int)
        or int(raw["pid"]) <= 0
        or not isinstance(raw.get("token"), str)
        or not raw["token"]
        or not isinstance(raw.get("project_id"), str)
        or not raw["project_id"]
        or not isinstance(raw.get("config_path"), str)
        or not raw["config_path"]
    ):
        raise RuntimeError(f"invalid supervisor ownership record: {pid_path}")
    return ServiceRecord(
        pid=int(raw["pid"]),
        token=str(raw["token"]),
        project_id=str(raw["project_id"]),
        config_path=str(raw["config_path"]),
    )


def _write_service_record(config: AppConfig, record: ServiceRecord) -> None:
    pid_path, _ = runtime_paths(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=pid_path.parent, delete=False
    ) as temporary:
        json.dump(asdict(record), temporary, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, pid_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_pid(config: AppConfig) -> int | None:
    try:
        record = _read_service_record(config)
    except RuntimeError:
        return None
    return record.pid if record else None


def process_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    with _spawned_children_lock:
        child = _spawned_children.get(pid)
        if child is not None and child.poll() is not None:
            _spawned_children.pop(pid, None)
            return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _track_spawned_child(process: subprocess.Popen[bytes]) -> None:
    with _spawned_children_lock:
        _spawned_children[process.pid] = process


def _terminate_spawned_child(process: subprocess.Popen[bytes]) -> None:
    """Stop and reap a non-winning or failed service child started here."""

    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    finally:
        with _spawned_children_lock:
            _spawned_children.pop(process.pid, None)


def _process_command_line(pid: int) -> str | None:
    if os.name != "posix":
        return None
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            return None
    completed = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "command="],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _record_owned_by_service(config: AppConfig, record: ServiceRecord) -> bool:
    if record.project_id != config.project.id or not process_alive(record.pid):
        return False
    command_line = _process_command_line(record.pid)
    return bool(
        command_line
        and "agent_workflow_supervisor.service" in command_line
        and record.token in command_line
        and record.config_path in command_line
    )


def service_running(config: AppConfig) -> bool:
    record = _read_service_record(config)
    return bool(record and _record_owned_by_service(config, record))


@contextmanager
def _service_lifetime_lock(config: AppConfig) -> Iterator[None]:
    lock_path = config.supervisor.runtime_dir / "supervisor.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise RuntimeError("supervisor service lock is already held") from error
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("supervisor service lock is already held") from error
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def ensure_ao_ready(config: AppConfig, timeout_seconds: float = 20.0) -> None:
    cli = CommandAdapter(config.runner.command, timeout_seconds=5)

    def ready() -> bool:
        try:
            response = cli.run_json("status", "--json")
        except (AdapterCommandError, subprocess.TimeoutExpired):
            return False
        return response.get("ready") == "ready" and response.get("health") == "ok"

    if ready():
        return
    if sys.platform == "darwin":
        subprocess.run(
            ["open", "-a", "Agent Orchestrator"],
            check=False,
            capture_output=True,
            text=True,
        )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(0.5)
    raise RuntimeError("Agent Orchestrator did not become ready")


def start_service(config_path: Path) -> int:
    resolved_config_path = config_path.expanduser().resolve()
    config = load_config(resolved_config_path, registry_path=REGISTRY_PATH)
    record = _read_service_record(config)
    if record is not None:
        if _record_owned_by_service(config, record):
            return record.pid
        if process_alive(record.pid):
            raise RuntimeError(
                f"refusing to replace unverified live pid {record.pid} in the supervisor record"
            )
        runtime_paths(config)[0].unlink(missing_ok=True)

    if config.runner.type == "ao":
        ensure_ao_ready(config)
    pid_path, log_path = runtime_paths(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    instance_token = uuid.uuid4().hex
    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_workflow_supervisor.service",
                "--config",
                str(resolved_config_path),
                "--instance-token",
                instance_token,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _track_spawned_child(process)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = _read_service_record(config)
        if current is not None and _record_owned_by_service(config, current):
            if current.pid != process.pid:
                _terminate_spawned_child(process)
            return current.pid
        if process.poll() is not None and current is not None and process_alive(current.pid):
            raise RuntimeError(
                f"supervisor ownership record points to unverified pid {current.pid}; "
                f"inspect {pid_path}"
            )
        time.sleep(0.1)
    _terminate_spawned_child(process)
    raise RuntimeError("supervisor service did not write its pid file")


def stop_service(config: AppConfig, timeout_seconds: float = 10.0) -> bool:
    record = _read_service_record(config)
    if record is None:
        return False
    if not process_alive(record.pid):
        runtime_paths(config)[0].unlink(missing_ok=True)
        return False
    if not _record_owned_by_service(config, record):
        raise RuntimeError(
            f"refusing to signal unverified pid {record.pid}; inspect the supervisor record"
        )
    os.kill(record.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not process_alive(record.pid):
            return True
        time.sleep(0.1)
    raise TimeoutError(f"supervisor pid {record.pid} did not stop within {timeout_seconds}s")


def reconcile_job(config: AppConfig, store: JobStore, job: QueuedWork) -> None:
    try:
        canonical_work_item_id = canonical_github_issue_id(
            job.work_item_id,
            config.tracker.repository,
            strict=True,
        )
        invocation = {
            "configurable": {"thread_id": workflow_thread_id(config, canonical_work_item_id)}
        }
        with graph_runtime(config) as graph:
            snapshot = graph.get_state(invocation)
            if "approval_gate" in snapshot.next:
                values = dict(snapshot.values)
                gate = (
                    str(values.get("change_id") or ""),
                    str(values.get("change_head_sha") or ""),
                )
                if not all(gate):
                    raise RuntimeError("approval gate is missing its change id or target SHA")
                decision = store.claim_approval(
                    job.work_item_id,
                    change_id=gate[0],
                    target_sha=gate[1],
                )
                if decision is None:
                    store.update_result(
                        job.work_item_id,
                        status="awaiting_approval",
                        active=True,
                        clear_approval=True,
                        approval_gate=gate,
                    )
                    return
                result = graph.invoke(Command(resume={"action": decision}), config=invocation)
            else:
                # A decision is valid only while its exact interrupt is the
                # current graph state. Clear stale/legacy/crash-window state
                # before advancing to any possible future gate.
                store.clear_approval(job.work_item_id)
                result = graph.invoke(
                    {
                        "project_id": config.project.id,
                        "work_item_id": canonical_work_item_id,
                        "events": [],
                    },
                    config=invocation,
                )

        if result.get("__interrupt__"):
            status = "awaiting_approval"
            approval_gate = (
                str(result.get("change_id") or ""),
                str(result.get("change_head_sha") or ""),
            )
            if not all(approval_gate):
                raise RuntimeError("approval interrupt is missing its change id or target SHA")
        else:
            status = str(result.get("status") or "unknown")
            approval_gate = None
        store.update_result(
            job.work_item_id,
            status=status,
            active=status not in TERMINAL_STATUSES,
            last_error=str(result.get("last_error") or "") or None,
            # Publishing any gate always starts with no queued decision. The
            # user must authorize the exact change/head after it is visible.
            clear_approval=True,
            approval_gate=approval_gate,
        )
    except Exception as error:  # service boundary: persist and retry on the next tick
        store.update_result(
            job.work_item_id,
            status="retrying_after_error",
            active=True,
            last_error=f"{type(error).__name__}: {error}",
        )


def serve(config_path: Path, instance_token: str) -> None:
    config = load_config(config_path, registry_path=REGISTRY_PATH)
    pid_path, _ = runtime_paths(config)
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    with _service_lifetime_lock(config):
        existing = _read_service_record(config)
        if existing is not None and existing.pid != os.getpid() and process_alive(existing.pid):
            raise RuntimeError(f"supervisor ownership record already names live pid {existing.pid}")

        stopping = False

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        record = ServiceRecord(
            pid=os.getpid(),
            token=instance_token,
            project_id=config.project.id,
            config_path=str(config_path.expanduser().resolve()),
        )
        _write_service_record(config, record)
        store = JobStore(config.supervisor.runtime_dir)
        native_relay = (
            NativeAoRelay(config.runner.command)
            if config.runner.type == "ao" and config.supervisor.ao_native_relay
            else None
        )
        print(json.dumps({"event": "service_started", "pid": os.getpid()}), flush=True)
        try:
            while not stopping:
                jobs = store.list(active_only=True)
                if jobs:
                    runner_ready = True
                    if config.runner.type == "ao":
                        try:
                            ensure_ao_ready(config, timeout_seconds=10)
                        except Exception as error:
                            runner_ready = False
                            for job in jobs:
                                store.update_result(
                                    job.work_item_id,
                                    status="waiting_for_ao",
                                    active=True,
                                    last_error=f"{type(error).__name__}: {error}",
                                )
                    if runner_ready:
                        for job in jobs:
                            if stopping:
                                break
                            reconcile_job(config, store, job)
                deliver_pending_notifications(config, store)
                if native_relay is not None:
                    try:
                        native_relay.reconcile()
                    except Exception as error:
                        print(
                            json.dumps(
                                {
                                    "event": "ao_native_relay_error",
                                    "error": f"{type(error).__name__}: {error}",
                                }
                            ),
                            flush=True,
                        )
                deadline = time.monotonic() + config.supervisor.poll_interval_seconds
                while not stopping and time.monotonic() < deadline:
                    time.sleep(min(0.25, deadline - time.monotonic()))
        finally:
            try:
                current = _read_service_record(config)
                if current == record:
                    pid_path.unlink(missing_ok=True)
            finally:
                print(json.dumps({"event": "service_stopped", "pid": os.getpid()}), flush=True)


def job_as_dict(job: QueuedWork) -> dict[str, Any]:
    return asdict(job)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--instance-token", required=True)
    arguments = parser.parse_args()
    serve(arguments.config.expanduser().resolve(), arguments.instance_token)


if __name__ == "__main__":
    main()
