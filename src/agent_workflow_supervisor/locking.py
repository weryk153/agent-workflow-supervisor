"""Canonical cross-process locks and reservations for worker acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


def _canonical_user_home() -> Path:
    if os.name != "nt":
        try:
            import pwd

            return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        except (KeyError, OSError):
            pass
    return Path.home().resolve()


# Deliberately independent of project configuration. Two valid config files for
# the same AO project must contend on the same acquisition namespace.
LOCK_ROOT = (
    _canonical_user_home() / ".local" / "share" / "agent-workflow-supervisor" / "acquisitions"
)


@dataclass(frozen=True)
class AcquisitionRecord:
    state: Literal["pending", "worker"]
    execution_project_id: str
    harness: str
    worker_id: str | None = None
    model_profile: str | None = None
    model_capacity: int | None = None
    credential_key: str | None = None
    credential_capacity: int | None = None


def _key(project_id: str, work_item_id: str) -> str:
    return hashlib.sha256(f"{project_id}\0{work_item_id}".encode()).hexdigest()


def _paths(project_id: str, work_item_id: str) -> tuple[Path, Path]:
    key = _key(project_id, work_item_id)
    return LOCK_ROOT / f"{key}.lock", LOCK_ROOT / f"{key}.json"


def _global_capacity_path() -> Path:
    return LOCK_ROOT / "global-capacity.lock"


def _project_account_switch_path(project_id: str) -> Path:
    key = hashlib.sha256(f"account-switch\0{project_id}".encode()).hexdigest()
    return LOCK_ROOT / f"{key}.account-switch.lock"


def _execution_credential_path(execution_project_id: str) -> Path:
    key = hashlib.sha256(f"execution-credential\0{execution_project_id}".encode()).hexdigest()
    return LOCK_ROOT / f"{key}.credential-binding"


def _ensure_root() -> None:
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(LOCK_ROOT, 0o700)
    except OSError:
        # Some Windows filesystems do not implement POSIX modes.
        pass


@contextmanager
def _exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    _ensure_root()
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def global_capacity_lock() -> Iterator[None]:
    """Serialize allocation of project and user-global worker resources."""

    with _exclusive_file_lock(_global_capacity_path()):
        yield


@contextmanager
def project_account_switch_lock(project_id: str) -> Iterator[None]:
    """Serialize AO project binding, targeted replacement, and spawn."""

    with _exclusive_file_lock(_project_account_switch_path(project_id)):
        yield


@contextmanager
def work_item_acquisition_lock(project_id: str, work_item_id: str) -> Iterator[None]:
    """Serialize reservation changes for one AO work item."""

    lock_path, _ = _paths(project_id, work_item_id)
    with _exclusive_file_lock(lock_path):
        yield


@contextmanager
def worker_acquisition_locks(project_id: str, work_item_id: str) -> Iterator[None]:
    """Hold global capacity then work-item identity locks in canonical order."""

    with global_capacity_lock():
        with work_item_acquisition_lock(project_id, work_item_id):
            yield


def _decode_acquisition_record(
    raw: object,
    record_path: Path,
    *,
    expected_project_id: str | None = None,
    expected_work_item_id: str | None = None,
) -> tuple[str, str, AcquisitionRecord]:
    if not isinstance(raw, dict):
        raise RuntimeError(f"invalid worker acquisition record: {record_path}")
    project_id = raw.get("project_id")
    work_item_id = raw.get("work_item_id")
    state = raw.get("state")
    execution_project_id = raw.get("execution_project_id")
    harness = raw.get("harness")
    worker_id = raw.get("worker_id")
    model_profile = raw.get("model_profile")
    model_capacity = raw.get("model_capacity")
    credential_key = raw.get("credential_key")
    credential_capacity = raw.get("credential_capacity")
    if (
        not isinstance(project_id, str)
        or not project_id
        or not isinstance(work_item_id, str)
        or not work_item_id
        or (expected_project_id is not None and project_id != expected_project_id)
        or (expected_work_item_id is not None and work_item_id != expected_work_item_id)
        or state not in {"pending", "worker"}
        or not isinstance(execution_project_id, str)
        or not execution_project_id
        or not isinstance(harness, str)
        or not harness
        or (state == "worker" and (not isinstance(worker_id, str) or not worker_id))
        or (model_profile is not None and not isinstance(model_profile, str))
        or (
            model_capacity is not None
            and (not isinstance(model_capacity, int) or model_capacity < 1)
        )
        or (credential_key is not None and not isinstance(credential_key, str))
        or (
            credential_capacity is not None
            and (not isinstance(credential_capacity, int) or credential_capacity < 1)
        )
    ):
        raise RuntimeError(f"invalid worker acquisition record: {record_path}")
    return (
        project_id,
        work_item_id,
        AcquisitionRecord(
            state=state,
            execution_project_id=execution_project_id,
            harness=harness,
            worker_id=worker_id if isinstance(worker_id, str) else None,
            model_profile=model_profile or None,
            model_capacity=model_capacity,
            credential_key=credential_key or None,
            credential_capacity=credential_capacity,
        ),
    )


def read_acquisition_record(project_id: str, work_item_id: str) -> AcquisitionRecord | None:
    """Read a reservation while the caller holds the matching lock."""

    _, record_path = _paths(project_id, work_item_id)
    try:
        raw = json.loads(record_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"invalid worker acquisition record: {record_path}") from error
    return _decode_acquisition_record(
        raw,
        record_path,
        expected_project_id=project_id,
        expected_work_item_id=work_item_id,
    )[2]


def list_acquisition_records(project_id: str) -> list[tuple[str, AcquisitionRecord]]:
    """List one project's reservations without trusting unrelated records."""

    _ensure_root()
    records: list[tuple[str, AcquisitionRecord]] = []
    for record_path in sorted(LOCK_ROOT.glob("*.json")):
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # The filename is deliberately one-way hashed, so an unreadable
            # foreign record cannot safely be attributed to this project.
            # Exact work-item reads still fail closed.
            continue
        if not isinstance(raw, dict) or raw.get("project_id") != project_id:
            continue
        _, work_item_id, record = _decode_acquisition_record(
            raw,
            record_path,
            expected_project_id=project_id,
        )
        records.append((work_item_id, record))
    return records


def list_all_acquisition_records() -> list[tuple[str, str, AcquisitionRecord]]:
    """List valid global resource reservations while the global lock is held.

    A malformed record remains fail-closed for its exact work item through
    :func:`read_acquisition_record`, but is skipped here so unrelated projects
    do not lose all allocation capability because of one damaged file.
    """

    _ensure_root()
    records: list[tuple[str, str, AcquisitionRecord]] = []
    for record_path in sorted(LOCK_ROOT.glob("*.json")):
        try:
            raw = json.loads(record_path.read_text(encoding="utf-8"))
            records.append(_decode_acquisition_record(raw, record_path))
        except (json.JSONDecodeError, OSError, RuntimeError):
            continue
    return records


def _write_acquisition_record(
    project_id: str, work_item_id: str, record: AcquisitionRecord
) -> None:
    _ensure_root()
    _, record_path = _paths(project_id, work_item_id)
    payload = {
        "project_id": project_id,
        "work_item_id": work_item_id,
        "state": record.state,
        "execution_project_id": record.execution_project_id,
        "harness": record.harness,
        "worker_id": record.worker_id,
        "model_profile": record.model_profile,
        "model_capacity": record.model_capacity,
        "credential_key": record.credential_key,
        "credential_capacity": record.credential_capacity,
        "owner_pid": os.getpid(),
    }
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=LOCK_ROOT, delete=False
    ) as temporary:
        json.dump(payload, temporary, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, record_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def record_pending_acquisition(
    project_id: str,
    work_item_id: str,
    *,
    execution_project_id: str,
    harness: str,
    model_profile: str | None = None,
    model_capacity: int | None = None,
    credential_key: str | None = None,
    credential_capacity: int | None = None,
) -> None:
    _write_acquisition_record(
        project_id,
        work_item_id,
        AcquisitionRecord(
            "pending",
            execution_project_id,
            harness,
            model_profile=model_profile,
            model_capacity=model_capacity,
            credential_key=credential_key,
            credential_capacity=credential_capacity,
        ),
    )


def record_acquired_worker(
    project_id: str,
    work_item_id: str,
    *,
    execution_project_id: str,
    harness: str,
    worker_id: str,
    model_profile: str | None = None,
    model_capacity: int | None = None,
    credential_key: str | None = None,
    credential_capacity: int | None = None,
) -> None:
    _write_acquisition_record(
        project_id,
        work_item_id,
        AcquisitionRecord(
            "worker",
            execution_project_id,
            harness,
            worker_id,
            model_profile,
            model_capacity,
            credential_key,
            credential_capacity,
        ),
    )


def _account_switch_marker_path(project_id: str) -> Path:
    key = hashlib.sha256(f"account-switch-pending\0{project_id}".encode()).hexdigest()
    return LOCK_ROOT / f"{key}.account-switch.pending"


def _write_account_switch_marker(project_id: str, payload: dict[str, object]) -> None:
    _ensure_root()
    marker_path = _account_switch_marker_path(project_id)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=LOCK_ROOT, delete=False
    ) as temporary:
        json.dump(payload, temporary, sort_keys=True)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, marker_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_account_switch_marker(project_id: str) -> dict[str, object] | None:
    marker_path = _account_switch_marker_path(project_id)
    try:
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"invalid account-switch marker: {marker_path}") from error
    if not isinstance(raw, dict):
        raise RuntimeError(f"invalid account-switch marker: {marker_path}")
    return raw


def _process_alive(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def mark_account_switch_pending(project_id: str, switch_id: str) -> None:
    """Block new worker acquisition until a scheduled account switch settles."""

    _write_account_switch_marker(
        project_id,
        {
            "project_id": project_id,
            "switch_id": switch_id,
            "owner_pid": os.getpid(),
            "helper_pid": None,
        },
    )


def attach_account_switch_helper(project_id: str, switch_id: str, helper_pid: int) -> None:
    """Transfer marker liveness from the scheduling process to its helper."""

    raw = _read_account_switch_marker(project_id)
    if raw is None or raw.get("switch_id") != switch_id:
        raise RuntimeError(f"account switch {switch_id!r} is not pending for {project_id!r}")
    raw["helper_pid"] = helper_pid
    _write_account_switch_marker(project_id, raw)


def account_switch_pending(project_id: str) -> bool:
    raw = _read_account_switch_marker(project_id)
    if raw is None:
        return False
    responsible_pid = raw.get("helper_pid") or raw.get("owner_pid")
    if _process_alive(responsible_pid):
        return True
    # The scheduler/helper died without reaching its finally block. Reclaim
    # the barrier under the caller's global allocation lock.
    _account_switch_marker_path(project_id).unlink(missing_ok=True)
    return False


def account_switch_id(project_id: str) -> str | None:
    """Return the active scheduled switch id, rejecting a damaged marker."""

    if not account_switch_pending(project_id):
        return None
    marker_path = _account_switch_marker_path(project_id)
    raw = _read_account_switch_marker(project_id)
    assert raw is not None
    switch_id = raw.get("switch_id")
    if not isinstance(switch_id, str) or not switch_id:
        raise RuntimeError(f"invalid account-switch marker: {marker_path}")
    return switch_id


def clear_account_switch_pending(project_id: str, switch_id: str) -> None:
    """Clear only the marker created by the named switch operation."""

    marker_path = _account_switch_marker_path(project_id)
    try:
        raw = json.loads(marker_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (json.JSONDecodeError, OSError):
        return
    if isinstance(raw, dict) and raw.get("switch_id") == switch_id:
        marker_path.unlink(missing_ok=True)


def record_execution_credential_identity(
    execution_project_id: str, config_dir: str | Path | None
) -> None:
    """Persist the login identity currently applied to one AO project."""

    _ensure_root()
    identity = str(Path(config_dir).expanduser().resolve()) if config_dir is not None else None
    binding_path = _execution_credential_path(execution_project_id)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=LOCK_ROOT, delete=False
    ) as temporary:
        json.dump(
            {
                "execution_project_id": execution_project_id,
                "claude_config_dir": identity,
            },
            temporary,
            sort_keys=True,
        )
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    try:
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, binding_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def execution_credential_identity(execution_project_id: str) -> tuple[bool, str | None]:
    """Return whether AO project identity is known and its config directory."""

    binding_path = _execution_credential_path(execution_project_id)
    try:
        raw = json.loads(binding_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, None
    except (json.JSONDecodeError, OSError) as error:
        raise RuntimeError(f"invalid execution credential binding: {binding_path}") from error
    if not isinstance(raw, dict) or raw.get("execution_project_id") != execution_project_id:
        raise RuntimeError(f"invalid execution credential binding: {binding_path}")
    config_dir = raw.get("claude_config_dir")
    if config_dir is not None and not isinstance(config_dir, str):
        raise RuntimeError(f"invalid execution credential binding: {binding_path}")
    return True, config_dir


def clear_acquisition_record(
    project_id: str, work_item_id: str, *, worker_id: str | None = None
) -> None:
    """Clear a reservation, optionally only when it names one worker."""

    _, record_path = _paths(project_id, work_item_id)
    if worker_id is not None:
        record = read_acquisition_record(project_id, work_item_id)
        if record is None or record.state != "worker" or record.worker_id != worker_id:
            return
    record_path.unlink(missing_ok=True)
