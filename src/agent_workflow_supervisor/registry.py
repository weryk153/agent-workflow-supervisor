"""Global account and project registry for convenient multi-project operation."""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tomlkit import document, dumps, parse, table

CONFIG_ROOT = (
    Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "agent-workflow-supervisor"
)
REGISTRY_PATH = CONFIG_ROOT / "registry.toml"


@dataclass(frozen=True)
class AccountRecord:
    name: str
    provider: str = "claude-code"
    config_dir: Path | None = None


@dataclass(frozen=True)
class ModelProfileRecord:
    """Reusable, non-secret model selection understood by a runner harness."""

    name: str
    harness: str
    model: str
    provider: str | None = None
    capacity: int = 1


@dataclass(frozen=True)
class ProjectRecord:
    project_id: str
    config_path: Path
    accounts: tuple[str, ...] = ()
    model_profiles: tuple[str, ...] = ()
    default_model_profile: str | None = None


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_UNSET = object()


def _validate_name(value: str, *, kind: str) -> str:
    if not _NAME_PATTERN.fullmatch(value):
        raise ValueError(
            f"{kind} must start with an alphanumeric character and contain only "
            "letters, numbers, dot, underscore, or hyphen"
        )
    return value


def _load(path: Path) -> Any:
    if not path.exists():
        return document()
    return parse(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as temporary:
        temporary.write(dumps(value))
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, path)


@contextmanager
def _registry_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            pass
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


def _ensure_registry_unlocked(path: Path) -> None:
    value = _load(path)
    accounts = value.get("accounts")
    if accounts is None:
        accounts = table()
        value["accounts"] = accounts
    if "default" not in accounts:
        default = table()
        default.add("provider", "claude-code")
        accounts["default"] = default
    if value.get("models") is None:
        value["models"] = table()
    if value.get("projects") is None:
        value["projects"] = table()
    _write(path, value)


def ensure_registry(path: Path = REGISTRY_PATH) -> None:
    with _registry_lock(path):
        _ensure_registry_unlocked(path)


def register_account(
    name: str,
    *,
    config_dir: Path | None,
    provider: str = "claude-code",
    path: Path = REGISTRY_PATH,
) -> AccountRecord:
    _validate_name(name, kind="account name")
    with _registry_lock(path):
        _ensure_registry_unlocked(path)
        value = _load(path)
        record = table()
        record.add("provider", provider)
        if config_dir is not None:
            record.add("config_dir", str(config_dir.expanduser().resolve()))
        value["accounts"][name] = record
        _write(path, value)
    return AccountRecord(name=name, provider=provider, config_dir=config_dir)


def register_model_profile(
    name: str,
    *,
    harness: str,
    model: str,
    provider: str | None = None,
    capacity: int = 1,
    path: Path = REGISTRY_PATH,
) -> ModelProfileRecord:
    """Create or replace a reusable model profile without storing credentials."""

    _validate_name(name, kind="model profile name")
    if not harness.strip():
        raise ValueError("harness must not be empty")
    if not model.strip():
        raise ValueError("model must not be empty")
    if capacity < 1:
        raise ValueError("capacity must be at least 1")
    normalized_provider = provider.strip().lower() if provider and provider.strip() else None
    with _registry_lock(path):
        _ensure_registry_unlocked(path)
        value = _load(path)
        record = table()
        record.add("harness", harness.strip())
        record.add("model", model.strip())
        if normalized_provider:
            record.add("provider", normalized_provider)
        record.add("capacity", capacity)
        value["models"][name] = record
        _write(path, value)
    return ModelProfileRecord(
        name=name,
        harness=harness.strip(),
        model=model.strip(),
        provider=normalized_provider,
        capacity=capacity,
    )


def list_model_profiles(path: Path = REGISTRY_PATH) -> list[ModelProfileRecord]:
    ensure_registry(path)
    value = _load(path)
    result = []
    for name, raw in value["models"].items():
        result.append(
            ModelProfileRecord(
                name=str(name),
                harness=str(raw["harness"]),
                model=str(raw["model"]),
                provider=str(raw["provider"]) if raw.get("provider") else None,
                capacity=int(raw.get("capacity", 1)),
            )
        )
    return result


def get_model_profile(name: str, path: Path = REGISTRY_PATH) -> ModelProfileRecord:
    match = next(
        (profile for profile in list_model_profiles(path) if profile.name == name),
        None,
    )
    if match is None:
        raise KeyError(name)
    return match


def list_accounts(path: Path = REGISTRY_PATH) -> list[AccountRecord]:
    ensure_registry(path)
    value = _load(path)
    result = []
    for name, raw in value["accounts"].items():
        config_dir = raw.get("config_dir")
        result.append(
            AccountRecord(
                name=str(name),
                provider=str(raw.get("provider") or "claude-code"),
                config_dir=Path(str(config_dir)).expanduser() if config_dir else None,
            )
        )
    return result


def get_account(name: str, path: Path = REGISTRY_PATH) -> AccountRecord:
    match = next((account for account in list_accounts(path) if account.name == name), None)
    if match is None:
        raise KeyError(name)
    return match


def _set_project_record(
    value: Any,
    project_id: str,
    *,
    config_path: Path,
    accounts: list[str] | tuple[str, ...] | None = None,
    model_profiles: list[str] | tuple[str, ...] | None = None,
    default_model_profile: str | None | object = _UNSET,
) -> ProjectRecord:
    existing = value["projects"].get(project_id)
    record = table()
    record.add("config_path", str(config_path.expanduser().resolve()))
    if accounts is None and existing is not None:
        selected = list(existing.get("accounts", []))
    else:
        selected = list(accounts or [])
    record.add("accounts", selected)
    if model_profiles is None and existing is not None:
        selected_models = list(existing.get("models", []))
    else:
        selected_models = list(model_profiles or [])
    record.add("models", selected_models)
    if default_model_profile is _UNSET:
        selected_default = existing.get("default_model") if existing is not None else None
    else:
        selected_default = default_model_profile
    if selected_default:
        record.add("default_model", str(selected_default))
    value["projects"][project_id] = record
    return ProjectRecord(
        project_id,
        config_path.expanduser().resolve(),
        tuple(selected),
        tuple(selected_models),
        str(selected_default) if selected_default else None,
    )


def register_project(
    project_id: str,
    *,
    config_path: Path,
    accounts: list[str] | tuple[str, ...] | None = None,
    model_profiles: list[str] | tuple[str, ...] | None = None,
    default_model_profile: str | None | object = _UNSET,
    path: Path = REGISTRY_PATH,
) -> ProjectRecord:
    _validate_name(project_id, kind="project id")
    with _registry_lock(path):
        _ensure_registry_unlocked(path)
        value = _load(path)
        project = _set_project_record(
            value,
            project_id,
            config_path=config_path,
            accounts=accounts,
            model_profiles=model_profiles,
            default_model_profile=default_model_profile,
        )
        _write(path, value)
    return project


def set_project_model_profiles(
    project_id: str,
    profile_names: list[str] | tuple[str, ...],
    *,
    default_profile: str,
    path: Path = REGISTRY_PATH,
) -> ProjectRecord:
    """Assign an ordered model pool to one project using global profile names."""

    if not profile_names:
        raise ValueError("at least one model profile is required")
    if len(profile_names) != len(set(profile_names)):
        raise ValueError("model profile list contains duplicates")
    if default_profile not in profile_names:
        raise ValueError("default model profile must be assigned to the project")
    with _registry_lock(path):
        _ensure_registry_unlocked(path)
        value = _load(path)
        available = {str(name) for name in value["models"]}
        missing = [name for name in profile_names if name not in available]
        if missing:
            raise KeyError(", ".join(missing))
        raw_project = value["projects"].get(project_id)
        if raw_project is None:
            raise KeyError(project_id)
        project = _set_project_record(
            value,
            project_id,
            config_path=Path(str(raw_project["config_path"])),
            accounts=tuple(str(name) for name in raw_project.get("accounts", [])),
            model_profiles=profile_names,
            default_model_profile=default_profile,
        )
        _write(path, value)
    return project


def list_projects(path: Path = REGISTRY_PATH) -> list[ProjectRecord]:
    ensure_registry(path)
    value = _load(path)
    result = []
    for project_id, raw in value["projects"].items():
        result.append(
            ProjectRecord(
                project_id=str(project_id),
                config_path=Path(str(raw["config_path"])).expanduser(),
                accounts=tuple(str(name) for name in raw.get("accounts", [])),
                model_profiles=tuple(str(name) for name in raw.get("models", [])),
                default_model_profile=(
                    str(raw["default_model"]) if raw.get("default_model") else None
                ),
            )
        )
    return result


def get_project(project_id: str, path: Path = REGISTRY_PATH) -> ProjectRecord:
    match = next(
        (project for project in list_projects(path) if project.project_id == project_id),
        None,
    )
    if match is None:
        raise KeyError(project_id)
    return match
