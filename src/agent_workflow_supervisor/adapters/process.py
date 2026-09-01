"""AO-independent runner backed by durable local processes and git worktrees."""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_workflow_supervisor.config import CredentialProfileConfig, RunnerConfig
from agent_workflow_supervisor.identifiers import canonical_github_repository
from agent_workflow_supervisor.models import AgentSession, ReviewResult, WorkItem


class ProcessRunnerError(RuntimeError):
    """Raised when a process runner operation cannot be completed safely."""


_spawned_helpers: dict[int, subprocess.Popen[bytes]] = {}
_spawned_helpers_lock = threading.Lock()
_DRIVER_SENTINEL = "oas-driver-sentinel"


def _reap_spawned_helpers() -> None:
    with _spawned_helpers_lock:
        finished = [pid for pid, child in _spawned_helpers.items() if child.poll() is not None]
        for pid in finished:
            _spawned_helpers.pop(pid, None)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _seconds_since(timestamp: str) -> float:
    value = datetime.fromisoformat(timestamp)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return (datetime.now(UTC) - value).total_seconds()


def _process_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    with _spawned_helpers_lock:
        child = _spawned_helpers.get(pid)
        if child is not None and child.poll() is not None:
            _spawned_helpers.pop(pid, None)
            return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _process_group_alive(process_group_id: int | None) -> bool:
    if process_group_id is None or process_group_id <= 0 or os.name != "posix":
        return False
    try:
        os.killpg(process_group_id, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _process_table() -> dict[int, str]:
    if os.name != "posix":
        return {}
    completed = subprocess.run(
        ["ps", "-axww", "-o", "pid=,command="],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if completed.returncode != 0:
        return {}
    processes: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            processes[int(fields[0])] = fields[1]
        except ValueError:
            continue
    return processes


def _owned_task_processes(pid: int | None, session_id: str, helper_token: str | None) -> set[int]:
    if not helper_token:
        return set()
    owned: set[int] = set()
    with _spawned_helpers_lock:
        child = _spawned_helpers.get(pid) if pid is not None else None
        if child is not None and child.poll() is None:
            arguments = child.args
            rendered = " ".join(arguments) if isinstance(arguments, list) else str(arguments)
            if (
                session_id in rendered
                and helper_token in rendered
                and "agent_workflow_supervisor.process_worker" in rendered
            ):
                owned.add(pid)  # type: ignore[arg-type]
    for candidate, command_line in _process_table().items():
        if helper_token not in command_line:
            continue
        helper = (
            session_id in command_line
            and "agent_workflow_supervisor.process_worker" in command_line
        )
        driver = _DRIVER_SENTINEL in command_line
        if helper or driver:
            owned.add(candidate)
    return {candidate for candidate in owned if _process_alive(candidate)}


def _helper_owned(pid: int | None, session_id: str, helper_token: str | None) -> bool:
    return bool(_owned_task_processes(pid, session_id, helper_token))


def _stop_helper(
    pid: int | None,
    session_id: str,
    helper_token: str | None,
    timeout_seconds: float = 5.0,
) -> bool:
    """Stop only a helper whose live command still names the expected session."""

    owned = _owned_task_processes(pid, session_id, helper_token)
    if not owned:
        return False
    groups: set[int] = set()
    for candidate in owned:
        try:
            groups.add(os.getpgid(candidate) if os.name == "posix" else candidate)
        except (OSError, ProcessLookupError):
            continue
    for process_group in groups:
        try:
            if os.name == "posix":
                os.killpg(process_group, signal.SIGTERM)
            else:
                os.kill(process_group, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _owned_task_processes(pid, session_id, helper_token):
            return True
        time.sleep(0.05)
    for process_group in groups:
        try:
            if os.name == "posix":
                os.killpg(process_group, signal.SIGKILL)
            else:
                os.kill(process_group, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    return not _owned_task_processes(pid, session_id, helper_token)


def _driver_wrapper_argv(argv: list[str], helper_token: str) -> list[str]:
    if os.name != "posix":
        return argv
    script = (
        'child=""; guard=""; '
        "cleanup() { "
        'test -z "$child" || kill -TERM "$child" 2>/dev/null; '
        'test -z "$guard" || kill -TERM "$guard" 2>/dev/null; '
        "}; "
        "trap 'cleanup' TERM INT HUP; "
        'exec 3<&0; "$@" <&3 & child=$!; exec 3<&-; '
        '/bin/sh -c \'trap "" TERM INT HUP; '
        'while kill -0 "$1" 2>/dev/null; do sleep 1; done\' '
        '"${0}-guard" "$child" & guard=$!; '
        'wait "$child"; status=$?; '
        'kill -TERM "$guard" 2>/dev/null || true; wait "$guard" 2>/dev/null || true; '
        'exit "$status"'
    )
    return ["/bin/sh", "-c", script, f"{_DRIVER_SENTINEL}:{helper_token}", *argv]


def _terminate_process_group(process: subprocess.Popen[str], timeout_seconds: float = 5.0) -> None:
    """Boundedly stop a process and every child in its newly-created group."""

    if process.poll() is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.communicate(timeout=timeout_seconds)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        pass


def _command_parts(command: str) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ProcessRunnerError("runner command must not be empty")
    return parts


def _safe_name(value: str, *, fallback: str = "work") -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._") or fallback


@dataclass(frozen=True)
class _PullRequest:
    id: str
    url: str
    state: str
    head_sha: str


class ProcessStateStore:
    """Small SQLite control plane shared by the supervisor and detached helpers."""

    SESSION_FIELDS = {
        "status",
        "terminated",
        "provider_session_id",
        "pid",
        "helper_token",
        "exit_code",
        "last_error",
        "updated_at",
    }
    REVIEW_FIELDS = {
        "status",
        "verdict",
        "feedback",
        "pid",
        "helper_token",
        "last_error",
        "updated_at",
    }

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
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
                CREATE TABLE IF NOT EXISTS process_sessions (
                    session_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    work_item_id TEXT NOT NULL,
                    harness TEXT NOT NULL,
                    model TEXT,
                    provider TEXT,
                    credential_profile TEXT,
                    credential_config_dir TEXT,
                    status TEXT NOT NULL,
                    terminated INTEGER NOT NULL DEFAULT 0,
                    report_only INTEGER NOT NULL DEFAULT 0,
                    worktree_path TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    provider_session_id TEXT,
                    pid INTEGER,
                    helper_token TEXT,
                    exit_code INTEGER,
                    last_error TEXT,
                    log_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(process_sessions)").fetchall()
            }
            if "provider" not in columns:
                connection.execute("ALTER TABLE process_sessions ADD COLUMN provider TEXT")
            if "helper_token" not in columns:
                connection.execute("ALTER TABLE process_sessions ADD COLUMN helper_token TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS process_reviews (
                    session_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    verdict TEXT NOT NULL DEFAULT 'unknown',
                    feedback TEXT NOT NULL DEFAULT '',
                    change_id TEXT NOT NULL,
                    change_url TEXT NOT NULL,
                    target_sha TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    pid INTEGER,
                    helper_token TEXT,
                    last_error TEXT,
                    log_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES process_sessions(session_id)
                )
                """
            )
            review_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(process_reviews)").fetchall()
            }
            if "helper_token" not in review_columns:
                connection.execute("ALTER TABLE process_reviews ADD COLUMN helper_token TEXT")

    def insert_session(self, values: Mapping[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self._transaction(immediate=True) as connection:
            connection.execute(
                f"INSERT INTO process_sessions ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )

    def update_session(self, session_id: str, **values: Any) -> None:
        invalid = set(values) - self.SESSION_FIELDS
        if invalid:
            raise ValueError(f"invalid process session fields: {sorted(invalid)}")
        values.setdefault("updated_at", _utc_now())
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._transaction(immediate=True) as connection:
            connection.execute(
                f"UPDATE process_sessions SET {assignments} WHERE session_id = ?",
                (*values.values(), session_id),
            )

    def update_session_for_helper(self, session_id: str, helper_token: str, **values: Any) -> bool:
        """Update only while the same launch still owns the active session turn."""

        invalid = set(values) - self.SESSION_FIELDS
        if invalid:
            raise ValueError(f"invalid process session fields: {sorted(invalid)}")
        values.setdefault("updated_at", _utc_now())
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE process_sessions SET {assignments}
                WHERE session_id = ? AND helper_token = ?
                  AND status IN ('starting', 'working')
                """,
                (*values.values(), session_id, helper_token),
            )
            return cursor.rowcount == 1

    def mark_session_launched(self, session_id: str, pid: int, helper_token: str) -> bool:
        """Acknowledge the outer helper without overwriting a promoted driver PID."""

        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_sessions
                SET status = 'working', pid = ?, updated_at = ?
                WHERE session_id = ? AND helper_token = ? AND (
                    (status = 'starting' AND pid IS NULL)
                    OR (status = 'working' AND pid = ?)
                  )
                """,
                (pid, _utc_now(), session_id, helper_token, pid),
            )
            return cursor.rowcount == 1

    def promote_session_driver(
        self,
        session_id: str,
        helper_pid: int,
        driver_pid: int,
        helper_token: str,
    ) -> bool:
        """CAS the outer helper PID to its token-bearing driver group leader."""

        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_sessions
                SET pid = ?, updated_at = ?
                WHERE session_id = ? AND status = 'working' AND pid = ?
                  AND helper_token = ?
                """,
                (driver_pid, _utc_now(), session_id, helper_pid, helper_token),
            )
            return cursor.rowcount == 1

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        with self._transaction() as connection:
            return connection.execute(
                "SELECT * FROM process_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()

    def list_sessions(self, project_id: str) -> list[sqlite3.Row]:
        with self._transaction() as connection:
            return connection.execute(
                """
                SELECT * FROM process_sessions
                WHERE project_id = ?
                ORDER BY created_at
                """,
                (project_id,),
            ).fetchall()

    def put_review(self, values: Mapping[str, Any]) -> None:
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        updates = ", ".join(f"{name} = excluded.{name}" for name in values if name != "session_id")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                f"""
                INSERT INTO process_reviews ({columns}) VALUES ({placeholders})
                ON CONFLICT(session_id) DO UPDATE SET {updates}
                """,
                tuple(values.values()),
            )

    def claim_review(self, values: Mapping[str, Any]) -> bool:
        """Atomically reserve a reviewer slot for one session."""

        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        updates = ", ".join(f"{name} = excluded.{name}" for name in values if name != "session_id")
        session_id = str(values["session_id"])
        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT status FROM process_reviews WHERE session_id = ?", (session_id,)
            ).fetchone()
            if existing is not None and str(existing["status"]) in {
                "starting",
                "running",
                "posting",
            }:
                return False
            connection.execute(
                f"""
                INSERT INTO process_reviews ({columns}) VALUES ({placeholders})
                ON CONFLICT(session_id) DO UPDATE SET {updates}
                """,
                tuple(values.values()),
            )
            return True

    def update_review(self, session_id: str, **values: Any) -> None:
        invalid = set(values) - self.REVIEW_FIELDS
        if invalid:
            raise ValueError(f"invalid process review fields: {sorted(invalid)}")
        values.setdefault("updated_at", _utc_now())
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._transaction(immediate=True) as connection:
            connection.execute(
                f"UPDATE process_reviews SET {assignments} WHERE session_id = ?",
                (*values.values(), session_id),
            )

    def update_review_for_run(self, session_id: str, run_id: str, **values: Any) -> bool:
        invalid = set(values) - self.REVIEW_FIELDS
        if invalid:
            raise ValueError(f"invalid process review fields: {sorted(invalid)}")
        values.setdefault("updated_at", _utc_now())
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE process_reviews SET {assignments}
                WHERE session_id = ? AND run_id = ?
                """,
                (*values.values(), session_id, run_id),
            )
            return cursor.rowcount == 1

    def update_review_for_helper(
        self,
        session_id: str,
        run_id: str,
        helper_token: str,
        **values: Any,
    ) -> bool:
        """Update only while the same detached review task still owns its run."""

        invalid = set(values) - self.REVIEW_FIELDS
        if invalid:
            raise ValueError(f"invalid process review fields: {sorted(invalid)}")
        values.setdefault("updated_at", _utc_now())
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE process_reviews SET {assignments}
                WHERE session_id = ? AND run_id = ? AND helper_token = ?
                  AND status IN ('starting', 'running', 'posting')
                """,
                (*values.values(), session_id, run_id, helper_token),
            )
            return cursor.rowcount == 1

    def begin_review_post(
        self,
        session_id: str,
        run_id: str,
        helper_token: str,
        verdict: str,
        feedback: str,
    ) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET status = 'posting', verdict = ?, feedback = ?, updated_at = ?
                WHERE session_id = ? AND run_id = ? AND status IN ('starting', 'running')
                  AND helper_token = ?
                """,
                (verdict, feedback, _utc_now(), session_id, run_id, helper_token),
            )
            return cursor.rowcount == 1

    def claim_review_comment(self, session_id: str, run_id: str, helper_token: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET status = 'starting', pid = NULL, helper_token = ?, updated_at = ?
                WHERE session_id = ? AND run_id = ? AND status = 'failed'
                  AND verdict IN ('approved', 'changes_requested')
                """,
                (helper_token, _utc_now(), session_id, run_id),
            )
            return cursor.rowcount == 1

    def claim_feedback(self, session_id: str, run_id: str, helper_token: str) -> bool:
        """Claim one feedback delivery before starting its external process."""

        now = _utc_now()
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET status = 'feedback_starting', helper_token = ?, updated_at = ?
                WHERE session_id = ? AND run_id = ?
                  AND status = 'completed' AND verdict = 'changes_requested'
                """,
                (helper_token, now, session_id, run_id),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE process_sessions
                SET status = 'starting', pid = NULL, helper_token = ?, exit_code = NULL,
                    last_error = NULL, updated_at = ?
                WHERE session_id = ?
                """,
                (helper_token, now, session_id),
            )
            return True

    def mark_feedback_launched(self, session_id: str, run_id: str, helper_token: str) -> bool:
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET status = 'feedback_sent', updated_at = ?
                WHERE session_id = ? AND run_id = ?
                  AND status IN ('feedback_starting', 'feedback_sent')
                  AND helper_token = ?
                """,
                (_utc_now(), session_id, run_id, helper_token),
            )
            return cursor.rowcount == 1

    def recover_unlaunched_feedback(self, session_id: str, run_id: str, helper_token: str) -> bool:
        """Roll back only a claim that never recorded a helper PID."""

        now = _utc_now()
        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET status = 'completed', helper_token = NULL, updated_at = ?
                WHERE session_id = ? AND run_id = ?
                  AND status = 'feedback_starting' AND helper_token = ?
                  AND EXISTS (
                    SELECT 1 FROM process_sessions
                    WHERE process_sessions.session_id = process_reviews.session_id
                      AND process_sessions.status IN ('starting', 'working')
                      AND process_sessions.helper_token = ?
                  )
                """,
                (now, session_id, run_id, helper_token, helper_token),
            )
            if cursor.rowcount != 1:
                return False
            connection.execute(
                """
                UPDATE process_sessions
                SET status = 'finished', pid = NULL, helper_token = NULL, updated_at = ?
                WHERE session_id = ? AND status IN ('starting', 'working')
                  AND helper_token = ?
                """,
                (now, session_id, helper_token),
            )
            return True

    def mark_review_launched(
        self, session_id: str, run_id: str, pid: int, helper_token: str
    ) -> bool:
        """Acknowledge the outer helper without overwriting a promoted driver PID."""

        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET status = 'running', pid = ?, updated_at = ?
                WHERE session_id = ? AND run_id = ? AND helper_token = ? AND (
                    (status = 'starting' AND pid IS NULL)
                    OR (status = 'running' AND pid = ?)
                  )
                """,
                (pid, _utc_now(), session_id, run_id, helper_token, pid),
            )
            return cursor.rowcount == 1

    def promote_review_driver(
        self,
        session_id: str,
        run_id: str,
        helper_pid: int,
        driver_pid: int,
        helper_token: str,
    ) -> bool:
        """CAS the outer review helper PID to its driver process-group leader."""

        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET pid = ?, updated_at = ?
                WHERE session_id = ? AND run_id = ? AND status = 'running'
                  AND pid = ? AND helper_token = ?
                """,
                (driver_pid, _utc_now(), session_id, run_id, helper_pid, helper_token),
            )
            return cursor.rowcount == 1

    def mark_review_post_process(
        self, session_id: str, run_id: str, pid: int, helper_token: str
    ) -> bool:
        """Replace the completed reviewer PID with the owned comment process PID."""

        with self._transaction(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE process_reviews
                SET pid = ?, updated_at = ?
                WHERE session_id = ? AND run_id = ? AND status = 'posting'
                  AND helper_token = ?
                """,
                (pid, _utc_now(), session_id, run_id, helper_token),
            )
            return cursor.rowcount == 1

    def get_review(self, session_id: str) -> sqlite3.Row | None:
        with self._transaction() as connection:
            return connection.execute(
                "SELECT * FROM process_reviews WHERE session_id = ?", (session_id,)
            ).fetchone()

    def delete_review(self, session_id: str) -> None:
        with self._transaction(immediate=True) as connection:
            connection.execute("DELETE FROM process_reviews WHERE session_id = ?", (session_id,))


class ProcessRunner:
    """Run supported coding CLIs without using AO as an execution plane."""

    def __init__(
        self,
        config: RunnerConfig,
        *,
        repository: str,
        tracker_command: str,
        runtime_dir: Path,
        credentials: Mapping[str, CredentialProfileConfig],
        report_only_harnesses: set[str],
    ) -> None:
        if config.type != "process":
            raise ValueError("ProcessRunner requires runner.type = 'process'")
        if os.name != "posix":
            raise ProcessRunnerError(
                "process runner currently requires a POSIX platform so detached driver "
                "ownership and process-group shutdown can fail closed; use AO mode on "
                "native Windows"
            )
        if config.repository_path is None or config.worktree_root is None:
            raise ValueError("process runner paths were not resolved")
        self.config = config
        self.repository = repository
        self.repository_path = config.repository_path
        self.worktree_root = config.worktree_root
        self.tracker_command = _command_parts(tracker_command)
        self.git_command = _command_parts(config.git_command)
        self.credentials = credentials
        self.report_only_harnesses = report_only_harnesses
        self.runtime_dir = runtime_dir / "process-runner"
        self.task_dir = self.runtime_dir / "tasks"
        self.log_dir = self.runtime_dir / "logs"
        self.state = ProcessStateStore(self.runtime_dir / "state.sqlite")
        self._validate_repository()

    def _run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: int = 120,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProcessRunnerError(f"unable to run {command[0]}: {error}") from error
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise ProcessRunnerError(f"{command[0]} exited with {completed.returncode}: {detail}")
        return completed

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int = 120,
    ) -> str:
        return self._run(
            [*self.git_command, *args], cwd=cwd, check=check, timeout=timeout
        ).stdout.strip()

    def _validate_repository(self) -> None:
        root = self._git("-C", str(self.repository_path), "rev-parse", "--show-toplevel")
        if Path(root).resolve() != self.repository_path.resolve():
            raise ProcessRunnerError(
                f"runner repository_path must be the git worktree root: {root}"
            )
        if self.config.verify_repository_remote:
            remote = self._git("-C", str(self.repository_path), "remote", "get-url", "origin")
            actual = canonical_github_repository(remote).casefold()
            expected = self.repository.casefold()
            if actual != expected:
                raise ProcessRunnerError(
                    f"runner repository origin mismatch: expected {expected}, found {actual}"
                )

    def _default_branch_ref(self) -> str:
        symbolic = self._git(
            "-C",
            str(self.repository_path),
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            check=False,
        )
        if symbolic:
            return symbolic
        self._git("-C", str(self.repository_path), "fetch", "origin", timeout=180)
        symbolic = self._git(
            "-C",
            str(self.repository_path),
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            check=False,
        )
        if symbolic:
            return symbolic
        response = self._run(
            [
                *self.tracker_command,
                "repo",
                "view",
                self.repository,
                "--json",
                "defaultBranchRef",
            ]
        )
        try:
            branch = json.loads(response.stdout)["defaultBranchRef"]["name"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ProcessRunnerError("unable to determine the repository default branch") from error
        return f"origin/{branch}"

    def _create_worktree(self, session_id: str, work_item_id: str) -> tuple[Path, str]:
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        worktree = self.worktree_root / session_id
        branch = f"oas/{_safe_name(work_item_id)[:32]}-{session_id.rsplit('-', 1)[-1]}"
        self._git(
            "-C",
            str(self.repository_path),
            "fetch",
            "origin",
            timeout=180,
        )
        self._git(
            "-C",
            str(self.repository_path),
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            self._default_branch_ref(),
            timeout=180,
        )
        return worktree, branch

    def _find_pull_request(self, branch: str) -> _PullRequest | None:
        completed = self._run(
            [
                *self.tracker_command,
                "pr",
                "list",
                "--repo",
                self.repository,
                "--head",
                branch,
                "--state",
                "all",
                "--limit",
                "1",
                "--json",
                "number,url,state,headRefOid",
            ]
        )
        try:
            raw = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ProcessRunnerError("tracker returned invalid JSON while locating PR") from error
        if not isinstance(raw, list) or not raw:
            return None
        item = raw[0]
        return _PullRequest(
            id=str(item["number"]),
            url=str(item.get("url") or ""),
            state=str(item.get("state") or "UNKNOWN"),
            head_sha=str(item.get("headRefOid") or ""),
        )

    def _session_value(self, row: sqlite3.Row) -> AgentSession:
        return AgentSession(
            id=str(row["session_id"]),
            role="worker",
            status=str(row["status"]),
            harness=str(row["harness"]),
            terminated=bool(row["terminated"]),
            work_item_id=str(row["work_item_id"]),
            project_id=str(row["project_id"]),
        )

    def _refresh_session(self, row: sqlite3.Row) -> sqlite3.Row:
        session_id = str(row["session_id"])
        status = str(row["status"])
        if status in {"starting", "working"}:
            pid = int(row["pid"]) if row["pid"] else None
            helper_token = str(row["helper_token"] or "") or None
            launch_in_progress = (
                status == "starting" and _seconds_since(str(row["updated_at"])) < 30
            )
            if launch_in_progress or self._task_process_active(
                pid=pid,
                session_id=session_id,
                helper_token=helper_token,
            ):
                return row
            review = self.state.get_review(session_id)
            feedback_token = str(review["helper_token"] or "") if review is not None else ""
            feedback_claim = (
                review is not None
                and str(review["status"]) in {"feedback_starting", "feedback_sent"}
                and feedback_token
                and feedback_token == helper_token
                and self._task_exists(feedback_token)
            )
            if feedback_claim:
                if str(review["status"]) == "feedback_sent":
                    self._remove_task_for_token(feedback_token)
                    self.state.update_session(
                        session_id, status="finished", pid=None, last_error=None
                    )
                    recovered = self.state.get_session(session_id)
                    assert recovered is not None
                    return recovered
                if _seconds_since(str(row["updated_at"])) < 30:
                    return row
                if self.state.recover_unlaunched_feedback(
                    session_id,
                    str(review["run_id"]),
                    feedback_token,
                ):
                    self._remove_task_for_token(feedback_token)
                    recovered = self.state.get_session(session_id)
                    assert recovered is not None
                    return recovered
            if self._task_exists(helper_token):
                self._remove_task_for_token(helper_token or "")
                if bool(row["report_only"]):
                    self.state.update_session(
                        session_id,
                        status="failed",
                        pid=None,
                        last_error="detached report worker ended without recording a result",
                    )
                else:
                    # The provider outlived a hard-killed state helper. Its exit
                    # code/output are unavailable, but a pushed PR is still a
                    # durable success signal, so enter the normal discovery window.
                    self.state.update_session(
                        session_id,
                        status="finished",
                        pid=None,
                        last_error=None,
                    )
            else:
                self.state.update_session(
                    session_id,
                    status="failed",
                    pid=None,
                    last_error="detached worker ended without recording a result",
                )
        elif status in {"finished", "idle", "pr_open"}:
            pull_request = self._find_pull_request(str(row["branch"]))
            if pull_request is not None:
                pull_request_state = pull_request.state.casefold()
                if pull_request_state == "merged":
                    next_status = "merged"
                elif pull_request_state == "open":
                    next_status = "pr_open"
                else:
                    next_status = "failed"
            elif status == "finished":
                if bool(row["report_only"]):
                    next_status = "idle"
                else:
                    finished_at = datetime.fromisoformat(str(row["updated_at"]))
                    if finished_at.tzinfo is None:
                        finished_at = finished_at.replace(tzinfo=UTC)
                    age = (datetime.now(UTC) - finished_at).total_seconds()
                    next_status = (
                        "failed" if age >= self.config.pr_discovery_timeout_seconds else "finished"
                    )
            else:
                next_status = status
            if next_status != status:
                error = None
                if next_status == "failed":
                    error = (
                        f"pull request was {pull_request.state.casefold()} without merging"
                        if pull_request is not None
                        else "worker finished successfully but did not open a pull request"
                    )
                self.state.update_session(session_id, status=next_status, last_error=error)
        refreshed = self.state.get_session(session_id)
        assert refreshed is not None
        return refreshed

    def list_sessions(self, project_id: str) -> list[AgentSession]:
        return [
            self._session_value(self._refresh_session(row))
            for row in self.state.list_sessions(project_id)
        ]

    def get_session(self, session_id: str) -> AgentSession | None:
        row = self.state.get_session(session_id)
        return self._session_value(self._refresh_session(row)) if row is not None else None

    def _credential_config_dir(self, name: str | None) -> str | None:
        if name is None:
            return None
        try:
            profile = self.credentials[name]
        except KeyError as error:
            raise ProcessRunnerError(f"credential profile is not configured: {name}") from error
        return str(profile.claude_config_dir) if profile.claude_config_dir is not None else None

    def _task_path(self, task_id: str) -> Path:
        self.task_dir.mkdir(parents=True, exist_ok=True)
        return self.task_dir / f"{task_id}.json"

    def _remove_task_for_token(self, helper_token: str) -> None:
        if helper_token:
            for path in self.task_dir.glob(f"*{_safe_name(helper_token)}*.json"):
                path.unlink(missing_ok=True)

    def _task_exists(self, helper_token: str | None) -> bool:
        return bool(helper_token) and any(self.task_dir.glob(f"*{_safe_name(helper_token)}*.json"))

    def _task_process_active(
        self,
        *,
        pid: int | None,
        session_id: str,
        helper_token: str | None,
    ) -> bool:
        if _helper_owned(pid, session_id, helper_token):
            return True
        # If both the process worker and its token-bearing sentinel were killed,
        # a provider child may still remain in the sentinel's original group.
        # The surviving private task file and stored group leader let us retain
        # the worktree without ever signalling a possibly-reused raw PID.
        if not self._task_exists(helper_token):
            return False
        if os.name == "posix":
            return _process_group_alive(pid)
        # Non-POSIX platforms do not expose an equivalent token-bearing process
        # group here. A raw PID may therefore retain state/worktrees, but is
        # never used as sufficient ownership proof for a destructive signal.
        return _process_alive(pid)

    def _write_task(self, task_id: str, payload: Mapping[str, Any]) -> Path:
        path = self._task_path(task_id)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False, sort_keys=True)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        try:
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return path

    def _launch(self, task_path: Path) -> int:
        _reap_spawned_helpers()
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_workflow_supervisor.process_worker",
                "--task",
                str(task_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        with _spawned_helpers_lock:
            _spawned_helpers[process.pid] = process
        return process.pid

    def _prepare_log(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _driver_payload(
        self,
        *,
        harness: str,
        model: str | None,
        provider: str | None,
        worktree: Path,
        prompt: str,
        provider_session_id: str | None,
        credential_config_dir: str | None,
    ) -> dict[str, Any]:
        try:
            command = self.config.commands[harness]
        except KeyError as error:
            raise ProcessRunnerError(
                f"process runner does not have a command for harness {harness!r}"
            ) from error
        return {
            "harness": harness,
            "command": command,
            "model": model,
            "provider": provider,
            "worktree": str(worktree),
            "prompt": prompt,
            "provider_session_id": provider_session_id,
            "credential_config_dir": credential_config_dir,
            "manage_claude_config_dir": harness == "claude-code",
            "claude_permission_mode": self.config.claude_permission_mode,
            "claude_allowed_tools": self.config.claude_allowed_tools,
            "codex_sandbox": self.config.codex_sandbox,
            "codex_approve_for_me": self.config.codex_approve_for_me,
        }

    def spawn_worker(
        self,
        *,
        project_id: str,
        work_item: WorkItem,
        harness: str,
        model: str | None,
        provider: str | None,
        credential_profile: str | None,
        prompt: str,
    ) -> AgentSession:
        session_id = (
            f"{_safe_name(project_id)[:24]}-{_safe_name(work_item.id)[:20]}-{uuid.uuid4().hex[:10]}"
        )
        worktree, branch = self._create_worktree(session_id, work_item.id)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{session_id}.log"
        self._prepare_log(log_path)
        now = _utc_now()
        credential_config_dir = self._credential_config_dir(credential_profile)
        self.state.insert_session(
            {
                "session_id": session_id,
                "project_id": project_id,
                "work_item_id": work_item.id,
                "harness": harness,
                "model": model,
                "provider": provider,
                "credential_profile": credential_profile,
                "credential_config_dir": credential_config_dir,
                "status": "starting",
                "terminated": 0,
                "report_only": int(harness in self.report_only_harnesses),
                "worktree_path": str(worktree),
                "branch": branch,
                "provider_session_id": None,
                "pid": None,
                "helper_token": session_id,
                "exit_code": None,
                "last_error": None,
                "log_path": str(log_path),
                "created_at": now,
                "updated_at": now,
            }
        )
        if harness in self.report_only_harnesses:
            direct_context = (
                "\n\nYou are running in an isolated git worktree on branch "
                f"{branch}. This is a report-only task: do not modify repository files, "
                "commit, push, or open a pull request. Do not wait for an interactive user "
                "reply."
            )
        else:
            direct_context = (
                "\n\nYou are running in an isolated git worktree on branch "
                f"{branch}. Verify the implementation, commit it, push this branch, and open "
                "a pull request against the repository default branch. Do not wait for an "
                "interactive user reply."
            )
        task_path = self._write_task(
            session_id,
            {
                "kind": "worker",
                "state_path": str(self.state.path),
                "session_id": session_id,
                "helper_token": session_id,
                "log_path": str(log_path),
                **self._driver_payload(
                    harness=harness,
                    model=model,
                    provider=provider,
                    worktree=worktree,
                    prompt=prompt + direct_context,
                    provider_session_id=None,
                    credential_config_dir=credential_config_dir,
                ),
            },
        )
        try:
            pid = self._launch(task_path)
        except Exception as error:
            self.state.update_session(session_id, status="failed", last_error=str(error))
            raise
        self.state.mark_session_launched(session_id, pid, session_id)
        row = self.state.get_session(session_id)
        assert row is not None
        return self._session_value(row)

    def _review_value(self, row: sqlite3.Row) -> ReviewResult:
        verdict = str(row["verdict"])
        if str(row["status"]) not in {
            "completed",
            "feedback_starting",
            "feedback_sent",
        }:
            verdict = "pending"
        elif verdict not in {"approved", "changes_requested", "pending", "unknown"}:
            verdict = "unknown"
        return ReviewResult(
            status=str(row["status"]),
            verdict=verdict,  # type: ignore[arg-type]
            feedback=str(row["feedback"]),
            change_id=str(row["change_id"]),
            change_url=str(row["change_url"]),
            target_sha=str(row["target_sha"]),
            run_id=str(row["run_id"]),
            started_at=str(row["started_at"]),
        )

    def get_review(self, session_id: str) -> ReviewResult | None:
        row = self.state.get_review(session_id)
        if row is None:
            return None
        status = str(row["status"])
        reviewer_pid = int(row["pid"]) if row["pid"] else None
        reviewer_token = str(row["helper_token"] or "") or None
        reviewer_active = self._task_process_active(
            pid=reviewer_pid,
            session_id=session_id,
            helper_token=reviewer_token,
        )
        launch_in_progress = status == "starting" and _seconds_since(str(row["updated_at"])) < 30
        if (
            status in {"starting", "running", "posting"}
            and not reviewer_active
            and not launch_in_progress
        ):
            self.state.update_review_for_run(
                session_id,
                str(row["run_id"]),
                status="failed",
                last_error="detached reviewer ended without recording a result",
            )
            self._remove_task_for_token(reviewer_token or "")
        elif status in {"feedback_starting", "feedback_sent"}:
            session = self.state.get_session(session_id)
            if session is not None:
                pull_request = self._find_pull_request(str(session["branch"]))
                if pull_request is not None and pull_request.head_sha != str(row["target_sha"]):
                    self.state.delete_review(session_id)
                    return None
                if status == "feedback_starting":
                    feedback_token = str(row["helper_token"] or "")
                    helper_active = self._task_process_active(
                        pid=int(session["pid"]) if session["pid"] else None,
                        session_id=session_id,
                        helper_token=feedback_token,
                    )
                    if not helper_active and _seconds_since(str(row["updated_at"])) >= 30:
                        recovered = self.state.recover_unlaunched_feedback(
                            session_id, str(row["run_id"]), feedback_token
                        )
                        if recovered:
                            self._remove_task_for_token(feedback_token)
                if status == "feedback_sent" and str(session["status"]) in {
                    "finished",
                    "idle",
                    "pr_open",
                }:
                    age = _seconds_since(str(session["updated_at"]))
                    if age >= self.config.pr_discovery_timeout_seconds:
                        self.state.update_session(
                            session_id,
                            status="failed",
                            last_error=(
                                "worker finished after review feedback without updating "
                                "the pull request head"
                            ),
                        )
        refreshed = self.state.get_review(session_id)
        return self._review_value(refreshed) if refreshed is not None else None

    def _retry_review_comment(self, session: sqlite3.Row, review: sqlite3.Row) -> None:
        session_id = str(session["session_id"])
        run_id = str(review["run_id"])
        helper_token = f"{run_id}-comment-{uuid.uuid4().hex}"
        task_path = self._write_task(
            f"{session_id}-review-comment-{helper_token}",
            {
                "kind": "review_comment",
                "state_path": str(self.state.path),
                "session_id": session_id,
                "helper_token": helper_token,
                "run_id": run_id,
                "log_path": str(review["log_path"]),
                "repository": self.repository,
                "tracker_command": self.tracker_command,
                "change_id": str(review["change_id"]),
                "expected_head_sha": str(review["target_sha"]),
                "git_command": self.git_command,
                "worktree": str(session["worktree_path"]),
                "verdict": str(review["verdict"]),
                "feedback": str(review["feedback"]),
            },
        )
        if not self.state.claim_review_comment(session_id, run_id, helper_token):
            task_path.unlink(missing_ok=True)
            return
        try:
            pid = self._launch(task_path)
        except Exception as error:
            self.state.update_review_for_run(
                session_id, run_id, status="failed", last_error=str(error)
            )
            task_path.unlink(missing_ok=True)
            raise
        self.state.mark_review_launched(session_id, run_id, pid, helper_token)

    def trigger_review(self, session_id: str) -> None:
        session = self.state.get_session(session_id)
        if session is None:
            raise ProcessRunnerError(f"unknown process session: {session_id}")
        pull_request = self._find_pull_request(str(session["branch"]))
        if pull_request is None or pull_request.state.casefold() != "open":
            raise ProcessRunnerError("cannot review a worker without an open pull request")
        worktree = Path(str(session["worktree_path"]))
        local_head = self._git("-C", str(worktree), "rev-parse", "HEAD")
        if local_head != pull_request.head_sha:
            raise ProcessRunnerError(
                "refusing to review a pull request whose head does not match the "
                f"worker worktree: local {local_head}, remote {pull_request.head_sha}"
            )
        worktree_status = self._git(
            "-C", str(worktree), "status", "--porcelain=v1", "--untracked-files=all"
        )
        if worktree_status:
            raise ProcessRunnerError(
                "refusing to review a dirty worker worktree; commit or remove all "
                "tracked and untracked changes first"
            )
        existing = self.state.get_review(session_id)
        if existing is not None and str(existing["status"]) in {
            "starting",
            "running",
            "posting",
        }:
            if (
                str(existing["status"]) == "starting"
                and _seconds_since(str(existing["updated_at"])) < 30
            ) or self._task_process_active(
                pid=int(existing["pid"]) if existing["pid"] else None,
                session_id=session_id,
                helper_token=str(existing["helper_token"] or "") or None,
            ):
                return
            self.state.update_review_for_run(
                session_id,
                str(existing["run_id"]),
                status="failed",
                last_error="detached reviewer ended without recording a result",
            )
            self._remove_task_for_token(str(existing["helper_token"] or ""))
            existing = self.state.get_review(session_id)
        if (
            existing is not None
            and str(existing["status"]) == "failed"
            and str(existing["verdict"]) in {"approved", "changes_requested"}
            and str(existing["target_sha"]) == pull_request.head_sha
        ):
            self._retry_review_comment(session, existing)
            return
        run_id = uuid.uuid4().hex
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{session_id}-review-{run_id[:10]}.log"
        self._prepare_log(log_path)
        now = _utc_now()
        review_values = {
            "session_id": session_id,
            "status": "starting",
            "verdict": "pending",
            "feedback": "",
            "change_id": pull_request.id,
            "change_url": pull_request.url,
            "target_sha": pull_request.head_sha,
            "run_id": run_id,
            "started_at": now,
            "pid": None,
            "helper_token": run_id,
            "last_error": None,
            "log_path": str(log_path),
            "updated_at": now,
        }
        if not self.state.claim_review(review_values):
            return
        credential_config_dir = self._credential_config_dir(self.config.review_credential_profile)
        prompt = (
            f"Review pull request {pull_request.url} at exact head SHA "
            f"{pull_request.head_sha}. Inspect the complete diff and relevant tests. Do not "
            "edit files, commit, push, or merge. Your final response must end with one JSON "
            "object and no text after it, using exactly this shape: "
            '{"verdict":"approved|changes_requested","feedback":"specific review notes"}. '
            "Request changes for any correctness, security, data-loss, or missing-test issue; "
            "otherwise approve."
        )
        task_path = self._write_task(
            f"{session_id}-review-{run_id}",
            {
                "kind": "review",
                "state_path": str(self.state.path),
                "session_id": session_id,
                "helper_token": run_id,
                "log_path": str(log_path),
                "repository": self.repository,
                "tracker_command": self.tracker_command,
                "change_id": pull_request.id,
                "run_id": run_id,
                "expected_head_sha": pull_request.head_sha,
                "git_command": self.git_command,
                **self._driver_payload(
                    harness=self.config.review_harness,
                    model=self.config.review_model,
                    provider=self.config.review_provider,
                    worktree=worktree,
                    prompt=prompt,
                    provider_session_id=None,
                    credential_config_dir=credential_config_dir,
                ),
                "claude_permission_mode": "plan",
                "claude_allowed_tools": [],
                "codex_sandbox": "read-only",
                "codex_approve_for_me": False,
            },
        )
        try:
            pid = self._launch(task_path)
        except Exception as error:
            self.state.update_review_for_run(
                session_id, run_id, status="failed", last_error=str(error)
            )
            raise
        self.state.mark_review_launched(session_id, run_id, pid, run_id)

    def cancel_review(self, session_id: str) -> None:
        review = self.state.get_review(session_id)
        if review is None:
            return
        if str(review["status"]) not in {"starting", "running", "posting"}:
            return
        pid = int(review["pid"]) if review["pid"] else None
        run_id = str(review["run_id"])
        helper_token = str(review["helper_token"] or "") or None
        _stop_helper(
            pid,
            session_id,
            helper_token,
        )
        still_active = self._task_process_active(
            pid=pid,
            session_id=session_id,
            helper_token=helper_token,
        )
        values: dict[str, Any] = {"status": "cancelled", "verdict": "unknown"}
        if not still_active:
            values["pid"] = None
            self._remove_task_for_token(helper_token or "")
        self.state.update_review_for_run(session_id, run_id, **values)

    def send(self, session_id: str, message: str) -> bool:
        session = self.state.get_session(session_id)
        if session is None:
            raise ProcessRunnerError(f"unknown process session: {session_id}")
        review = self.state.get_review(session_id)
        if review is not None and str(review["status"]) == "feedback_sent":
            return True
        if review is not None and str(review["status"]) == "feedback_starting":
            feedback_token = str(review["helper_token"] or "")
            feedback_run_id = str(review["run_id"])
            helper_active = self._task_process_active(
                pid=int(session["pid"]) if session["pid"] else None,
                session_id=session_id,
                helper_token=feedback_token,
            )
            if helper_active or _seconds_since(str(review["updated_at"])) < 30:
                return False
            if not self.state.recover_unlaunched_feedback(
                session_id, feedback_run_id, feedback_token
            ):
                return False
            self._remove_task_for_token(feedback_token)
            session = self.state.get_session(session_id)
            review = self.state.get_review(session_id)
            assert session is not None
        pid = int(session["pid"]) if session["pid"] else None
        if str(session["status"]) in {
            "starting",
            "working",
        } and self._task_process_active(
            pid=pid,
            session_id=session_id,
            helper_token=str(session["helper_token"] or "") or None,
        ):
            return False
        provider_session_id = str(session["provider_session_id"] or "") or None
        if provider_session_id is None:
            raise ProcessRunnerError(f"{session['harness']} did not expose a resumable session id")
        task_id = f"{session_id}-resume-{uuid.uuid4().hex}"
        task_path = self._write_task(
            task_id,
            {
                "kind": "worker",
                "state_path": str(self.state.path),
                "session_id": session_id,
                "helper_token": task_id,
                "feedback_run_id": str(review["run_id"]) if review is not None else None,
                "log_path": str(session["log_path"]),
                **self._driver_payload(
                    harness=str(session["harness"]),
                    model=str(session["model"] or "") or None,
                    provider=str(session["provider"] or "") or None,
                    worktree=Path(str(session["worktree_path"])),
                    prompt=message,
                    provider_session_id=provider_session_id,
                    credential_config_dir=(
                        str(session["credential_config_dir"])
                        if session["credential_config_dir"]
                        else None
                    ),
                ),
            },
        )
        review_run_id = str(review["run_id"]) if review is not None else ""
        if review is not None:
            if not self.state.claim_feedback(session_id, review_run_id, task_id):
                task_path.unlink(missing_ok=True)
                current = self.state.get_review(session_id)
                return current is not None and str(current["status"]) == "feedback_sent"
        else:
            self.state.update_session(
                session_id,
                status="starting",
                pid=None,
                helper_token=task_id,
                exit_code=None,
                last_error=None,
            )
        try:
            pid = self._launch(task_path)
        except Exception as error:
            self.state.update_session(session_id, status="failed", last_error=str(error))
            if review is not None:
                self.state.update_review_for_run(
                    session_id,
                    review_run_id,
                    status="completed",
                    last_error=f"feedback launch failed: {error}",
                )
            task_path.unlink(missing_ok=True)
            raise
        self.state.mark_session_launched(session_id, pid, task_id)
        if review is None:
            return True
        current = self.state.get_review(session_id)
        return current is not None and str(current["status"]) == "feedback_sent"

    def terminate(self, project_id: str, session_id: str) -> None:
        session = self.state.get_session(session_id)
        if session is None or str(session["project_id"]) != project_id:
            return
        pid = int(session["pid"]) if session["pid"] else None
        helper_token = str(session["helper_token"] or "") or None
        worker_was_active = str(session["status"]) in {"starting", "working"}
        worker_verified = self._task_process_active(
            pid=pid,
            session_id=session_id,
            helper_token=helper_token,
        )
        if worker_verified:
            _stop_helper(pid, session_id, helper_token)
        worker_still_active = self._task_process_active(
            pid=pid,
            session_id=session_id,
            helper_token=helper_token,
        )
        worker_unverified = worker_was_active and not worker_verified
        review = self.state.get_review(session_id)
        review_was_active = review is not None and str(review["status"]) in {
            "starting",
            "running",
            "posting",
        }
        review_still_active = False
        review_unverified = False
        review_token: str | None = None
        if review is not None:
            review_pid = int(review["pid"]) if review["pid"] else None
            review_token = str(review["helper_token"] or "") or None
            review_verified = self._task_process_active(
                pid=review_pid,
                session_id=session_id,
                helper_token=review_token,
            )
            if review_verified:
                _stop_helper(review_pid, session_id, review_token)
            review_still_active = self._task_process_active(
                pid=review_pid,
                session_id=session_id,
                helper_token=review_token,
            )
            review_unverified = review_was_active and not review_verified
        if review_was_active:
            self.cancel_review(session_id)
        self.state.update_session(session_id, status="terminated", terminated=1)
        worktree = Path(str(session["worktree_path"]))
        cleanup_safe = not (
            worker_still_active or worker_unverified or review_still_active or review_unverified
        )
        if worktree.is_dir() and cleanup_safe:
            self._git(
                "-C",
                str(self.repository_path),
                "worktree",
                "remove",
                str(worktree),
                check=False,
            )
            self._remove_task_for_token(helper_token or "")
            self._remove_task_for_token(review_token or "")


def _driver_argv(task: Mapping[str, Any]) -> list[str]:
    harness = str(task["harness"])
    command = _command_parts(str(task["command"]))
    model = str(task.get("model") or "")
    provider = str(task.get("provider") or "")
    worktree = str(task["worktree"])
    provider_session_id = str(task.get("provider_session_id") or "")
    if harness == "claude-code":
        argv = [
            *command,
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            str(task["claude_permission_mode"]),
        ]
        if model:
            argv.extend(["--model", model])
        allowed_tools = [str(tool) for tool in task.get("claude_allowed_tools") or []]
        if allowed_tools:
            argv.extend(["--allowedTools", *allowed_tools])
        if task["claude_permission_mode"] == "bypassPermissions":
            argv.append("--dangerously-skip-permissions")
        if provider_session_id:
            argv.extend(["--resume", provider_session_id])
        return argv
    if harness == "codex":
        argv = [
            *command,
            "exec",
            "--json",
            "--color",
            "never",
            "--cd",
            worktree,
        ]
        if not task.get("codex_approve_for_me"):
            argv.extend(["--sandbox", str(task["codex_sandbox"])])
        if provider in {"lmstudio", "ollama"}:
            argv.extend(["--oss", "--local-provider", provider])
            model = model.removeprefix(f"{provider}/")
        if model:
            argv.extend(["--model", model])
        if task.get("codex_approve_for_me"):
            argv.append("--approve-for-me")
        if provider_session_id:
            argv.extend(["resume", provider_session_id, "-"])
        else:
            argv.append("-")
        return argv
    if harness == "opencode":
        argv = [*command, "run", "--format", "json", "--dir", worktree]
        if model:
            argv.extend(["--model", model])
        if provider_session_id:
            argv.extend(["--session", provider_session_id])
        return argv
    raise ProcessRunnerError(f"unsupported process harness: {harness}")


def _find_session_id(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("session_id", "sessionId", "sessionID", "thread_id", "threadId"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            found = _find_session_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_session_id(child)
            if found:
                return found
    return None


def _json_values(output: str) -> list[Any]:
    values: list[Any] = []
    try:
        values.append(json.loads(output))
    except json.JSONDecodeError:
        pass
    for line in output.splitlines():
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return values


def _extract_session_id(output: str) -> str | None:
    for value in _json_values(output):
        if found := _find_session_id(value):
            return found
    return None


def _candidate_texts(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key in ("result", "text", "content", "message", "output"):
            if key in value:
                yield from _candidate_texts(value[key])
        for child in value.values():
            yield from _candidate_texts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _candidate_texts(child)


def _extract_review_result(output: str) -> tuple[str, str]:
    candidates = [output]
    for value in _json_values(output):
        candidates.extend(_candidate_texts(value))
    decoder = json.JSONDecoder()
    for candidate in reversed(candidates):
        for match in reversed(list(re.finditer(r"\{", candidate))):
            try:
                value, _ = decoder.raw_decode(candidate[match.start() :])
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            verdict = str(value.get("verdict") or "")
            feedback = str(value.get("feedback") or "")
            if verdict in {"approved", "changes_requested"}:
                return verdict, feedback
    raise ProcessRunnerError("reviewer did not return the required verdict JSON")


def _verify_task_checkout(task: Mapping[str, Any]) -> None:
    expected = str(task.get("expected_head_sha") or "")
    if not expected:
        return
    completed = subprocess.run(
        [
            *[str(part) for part in task["git_command"]],
            "-C",
            str(task["worktree"]),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    actual = completed.stdout.strip()
    if completed.returncode != 0 or actual != expected:
        raise ProcessRunnerError(
            "review worktree head changed or does not match the claimed pull request head: "
            f"expected {expected}, found {actual or 'unavailable'}"
        )
    status = subprocess.run(
        [
            *[str(part) for part in task["git_command"]],
            "-C",
            str(task["worktree"]),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if status.returncode != 0 or status.stdout.strip():
        raise ProcessRunnerError(
            "review worktree is not clean; refusing to accept a verdict from a "
            "reviewer that observed or created uncommitted changes"
        )


def _post_review_comment(
    task: Mapping[str, Any],
    state: ProcessStateStore,
    verdict: str,
    feedback: str,
) -> None:
    body = (
        f"agent-workflow-supervisor review: **{verdict}**\n\n"
        f"{feedback or 'No blocking findings.'}\n\n"
        f"<!-- agent-workflow-supervisor-review:{task['run_id']} -->"
    )
    command = [
        *[str(part) for part in task["tracker_command"]],
        "pr",
        "review",
        str(task["change_id"]),
        "--repo",
        str(task["repository"]),
        "--comment",
        "--body",
        body,
    ]
    posted = subprocess.Popen(
        _driver_wrapper_argv(command, str(task["helper_token"])),
        cwd=str(task["worktree"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
    )
    if not state.mark_review_post_process(
        str(task["session_id"]),
        str(task["run_id"]),
        posted.pid,
        str(task["helper_token"]),
    ):
        _terminate_process_group(posted)
        raise ProcessRunnerError("review comment task no longer owns its review run")
    try:
        stdout, stderr = posted.communicate(timeout=120)
    except subprocess.TimeoutExpired as error:
        _terminate_process_group(posted)
        raise ProcessRunnerError("review comment command timed out") from error
    if posted.returncode != 0:
        detail = stderr.strip() or stdout.strip() or "unknown error"
        raise ProcessRunnerError(f"unable to post review comment: {detail}")


def run_process_task(task_path: Path) -> None:
    """Execute one detached worker/reviewer turn and persist its terminal state."""

    task = json.loads(task_path.read_text(encoding="utf-8"))
    state = ProcessStateStore(Path(str(task["state_path"])))
    session_id = str(task["session_id"])
    kind = str(task["kind"])
    helper_token = str(task["helper_token"])
    review_task = kind in {"review", "review_comment"}
    if review_task:
        acquired = state.mark_review_launched(
            session_id, str(task["run_id"]), os.getpid(), helper_token
        )
    else:
        acquired = state.mark_session_launched(session_id, os.getpid(), helper_token)
    if not acquired:
        task_path.unlink(missing_ok=True)
        return
    feedback_run_id = str(task.get("feedback_run_id") or "")
    feedback_confirmed = False
    process: subprocess.Popen[str] | None = None
    log_path = Path(str(task["log_path"]))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    if task.get("manage_claude_config_dir"):
        if task.get("credential_config_dir"):
            environment["CLAUDE_CONFIG_DIR"] = str(task["credential_config_dir"])
        else:
            environment.pop("CLAUDE_CONFIG_DIR", None)
    run_id = str(task.get("run_id") or "")
    try:
        if kind == "review_comment":
            verdict = str(task["verdict"])
            feedback = str(task.get("feedback") or "")
            _verify_task_checkout(task)
            if not state.begin_review_post(session_id, run_id, helper_token, verdict, feedback):
                return
            _post_review_comment(task, state, verdict, feedback)
            state.update_review_for_helper(
                session_id,
                run_id,
                helper_token,
                status="completed",
                pid=None,
                last_error=None,
            )
            return

        argv = _driver_argv(task)
        if kind == "review":
            _verify_task_checkout(task)
        process = subprocess.Popen(
            _driver_wrapper_argv(argv, helper_token),
            cwd=str(task["worktree"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=os.name == "posix",
        )
        if review_task:
            driver_claimed = state.promote_review_driver(
                session_id,
                run_id,
                os.getpid(),
                process.pid,
                helper_token,
            )
        else:
            driver_claimed = state.promote_session_driver(
                session_id,
                os.getpid(),
                process.pid,
                helper_token,
            )
        if not driver_claimed:
            process.terminate()
            process.communicate()
            return
        assert process.stdin is not None
        process.stdin.write(str(task["prompt"]))
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
        if feedback_run_id:
            feedback_confirmed = state.mark_feedback_launched(
                session_id, feedback_run_id, helper_token
            )
            if not feedback_confirmed:
                process.terminate()
        output, stderr = process.communicate()
        completed = subprocess.CompletedProcess(argv, process.returncode, output, stderr)
        if feedback_run_id and not feedback_confirmed:
            return
        output = completed.stdout
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{_utc_now()}] $ {shlex.join(argv)}\n")
            log_file.write(output)
            if completed.stderr:
                log_file.write("\n[stderr]\n")
                log_file.write(completed.stderr)
            if output and not output.endswith("\n"):
                log_file.write("\n")
        if completed.returncode != 0:
            detail = completed.stderr.strip().splitlines()
            message = detail[-1] if detail else f"driver exited with {completed.returncode}"
            if kind == "review":
                state.update_review_for_helper(
                    session_id,
                    run_id,
                    helper_token,
                    status="failed",
                    verdict="unknown",
                    last_error=message,
                )
            else:
                state.update_session_for_helper(
                    session_id,
                    helper_token,
                    status="failed",
                    exit_code=completed.returncode,
                    last_error=message,
                )
            return
        if kind == "review":
            verdict, feedback = _extract_review_result(output)
            _verify_task_checkout(task)
            if not state.begin_review_post(session_id, run_id, helper_token, verdict, feedback):
                return
            _post_review_comment(task, state, verdict, feedback)
            state.update_review_for_helper(
                session_id,
                run_id,
                helper_token,
                status="completed",
                verdict=verdict,
                feedback=feedback,
                pid=None,
                last_error=None,
            )
        else:
            existing = state.get_session(session_id)
            provider_session_id = _extract_session_id(output)
            if provider_session_id is None and existing is not None:
                provider_session_id = str(existing["provider_session_id"] or "") or None
            state.update_session_for_helper(
                session_id,
                helper_token,
                status="finished",
                provider_session_id=provider_session_id,
                pid=None,
                exit_code=0,
                last_error=None,
            )
    except Exception as error:
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
        if review_task:
            review = state.get_review(session_id)
            values: dict[str, Any] = {
                "status": "failed",
                "pid": None,
                "last_error": f"{type(error).__name__}: {error}",
            }
            if review is None or str(review["verdict"]) not in {
                "approved",
                "changes_requested",
            }:
                values["verdict"] = "unknown"
            state.update_review_for_helper(session_id, run_id, helper_token, **values)
        else:
            recovered = False
            if feedback_run_id and not feedback_confirmed:
                recovered = state.recover_unlaunched_feedback(
                    session_id, feedback_run_id, helper_token
                )
            if not recovered:
                state.update_session_for_helper(
                    session_id,
                    helper_token,
                    status="failed",
                    pid=None,
                    last_error=f"{type(error).__name__}: {error}",
                )
    finally:
        # Task files can contain issue or review text. Remove them once the
        # detached process has durably recorded its result.
        task_path.unlink(missing_ok=True)


def wait_until_stopped(pid: int, timeout_seconds: float = 5.0) -> bool:
    """Test helper for bounded process-group shutdown checks."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.05)
    return not _process_alive(pid)
