"""Validated, provider-neutral supervisor configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class SupervisorConfig(BaseModel):
    database_path: Path = Path(".state/checkpoints.sqlite")
    runtime_dir: Path = Path(".state/runtime")
    poll_interval_seconds: float = Field(default=5.0, ge=1.0, le=300.0)
    review_timeout_seconds: float = Field(default=1800.0, ge=60.0, le=86400.0)
    review_max_attempts: int = Field(default=2, ge=1, le=10)
    shadow_mode: bool = True


class ProjectConfig(BaseModel):
    id: str = Field(min_length=1)


class RunnerConfig(BaseModel):
    type: Literal["ao"] = "ao"
    command: str = "ao"


class TrackerConfig(BaseModel):
    type: Literal["github"] = "github"
    command: str = "gh"
    repository: str = Field(pattern=r"^[^/]+/[^/]+$")


class CredentialProfileConfig(BaseModel):
    """Non-secret pointer to an isolated runner configuration.

    AO does not expose per-spawn environment overrides. Each Claude login must
    therefore be represented by a separate AO project whose project-level
    environment selects a distinct CLAUDE_CONFIG_DIR.
    """

    execution_project_id: str = Field(min_length=1)
    max_workers: int = Field(default=1, ge=1)
    claude_config_dir: Path | None = None


class CredentialsConfig(BaseModel):
    strategy: Literal["least-active"] = "least-active"
    profiles: dict[str, CredentialProfileConfig] = Field(default_factory=dict)


class ModelProfileConfig(BaseModel):
    """A non-secret runner selection that can be shared across projects."""

    harness: str = Field(min_length=1)
    model: str = Field(min_length=1)
    provider: str | None = None
    capacity: int = Field(default=1, ge=1)


class RouteRule(BaseModel):
    profile: str | None = Field(default=None, min_length=1)
    harness: str | None = Field(default=None, min_length=1)
    labels_any: set[str] = Field(default_factory=set)
    labels_all: set[str] = Field(default_factory=set)

    @model_validator(mode="after")
    def require_matcher(self) -> RouteRule:
        if not self.labels_any and not self.labels_all:
            raise ValueError("a route must define labels_any or labels_all")
        if (self.profile is None) == (self.harness is None):
            raise ValueError("a route must define exactly one of profile or harness")
        return self


class PolicyConfig(BaseModel):
    default_harness: str = "claude-code"
    default_model_profile: str | None = None
    skip_labels: set[str] = Field(default_factory=lambda: {"ao:skip"})
    approval_labels: set[str] = Field(default_factory=set)
    capacity: dict[str, int] = Field(default_factory=dict)
    models: dict[str, str] = Field(default_factory=dict)
    report_only_harnesses: set[str] = Field(default_factory=set)
    model_profiles: dict[str, ModelProfileConfig] = Field(default_factory=dict)
    credential_profiles: dict[str, list[str]] = Field(default_factory=dict)
    routes: list[RouteRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_capacity(self) -> PolicyConfig:
        invalid = {name: value for name, value in self.capacity.items() if value < 0}
        if invalid:
            raise ValueError(f"capacity values must be non-negative: {invalid}")
        return self


class AppConfig(BaseModel):
    supervisor: SupervisorConfig = Field(default_factory=SupervisorConfig)
    project: ProjectConfig
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    tracker: TrackerConfig
    credentials: CredentialsConfig = Field(default_factory=CredentialsConfig)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)

    @model_validator(mode="after")
    def validate_credential_profiles(self) -> AppConfig:
        configured = set(self.credentials.profiles)
        for harness, names in self.policy.credential_profiles.items():
            if len(names) != len(set(names)):
                raise ValueError(f"credential profile list for {harness!r} contains duplicates")
            missing = set(names) - configured
            if missing:
                raise ValueError(
                    f"credential profiles for {harness!r} are not configured: {sorted(missing)}"
                )
            project_ids = [self.credentials.profiles[name].execution_project_id for name in names]
            if len(project_ids) != len(set(project_ids)):
                raise ValueError(
                    f"credential profiles for {harness!r} must use distinct "
                    "execution_project_id values"
                )
        if (
            self.policy.default_model_profile is not None
            and self.policy.default_model_profile not in self.policy.model_profiles
        ):
            raise ValueError(
                f"default model profile is not configured: {self.policy.default_model_profile!r}"
            )
        missing_routes = sorted(
            {
                rule.profile
                for rule in self.policy.routes
                if rule.profile is not None and rule.profile not in self.policy.model_profiles
            }
        )
        if missing_routes:
            raise ValueError(f"route model profiles are not configured: {missing_routes}")
        return self


def _merge_registry_model_profiles(
    raw: dict[str, Any], *, registry_path: Path | None
) -> dict[str, Any]:
    """Resolve global profiles for one registered project without copying them to its file."""

    if registry_path is None or not registry_path.is_file():
        return raw
    from agent_workflow_supervisor.registry import (  # avoid a module import cycle
        get_model_profile,
        get_project,
    )

    project_id = str(raw.get("project", {}).get("id") or "")
    try:
        project = get_project(project_id, registry_path)
    except KeyError:
        return raw
    if not project.model_profiles:
        return raw

    policy = raw.setdefault("policy", {})
    policy["model_profiles"] = {
        name: {
            "harness": profile.harness,
            "model": profile.model,
            "provider": profile.provider,
            "capacity": profile.capacity,
        }
        for name in project.model_profiles
        for profile in [get_model_profile(name, registry_path)]
    }
    policy["default_model_profile"] = project.default_model_profile
    return raw


def load_config(path: str | Path, *, registry_path: Path | None = None) -> AppConfig:
    config_path = Path(path).expanduser().resolve()
    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)
    raw = _merge_registry_model_profiles(raw, registry_path=registry_path)
    config = AppConfig.model_validate(raw)
    if not config.supervisor.database_path.is_absolute():
        config.supervisor.database_path = (
            config_path.parent / config.supervisor.database_path
        ).resolve()
    if not config.supervisor.runtime_dir.is_absolute():
        config.supervisor.runtime_dir = (
            config_path.parent / config.supervisor.runtime_dir
        ).resolve()
    for profile in config.credentials.profiles.values():
        if profile.claude_config_dir is not None:
            profile.claude_config_dir = profile.claude_config_dir.expanduser()
            if not profile.claude_config_dir.is_absolute():
                profile.claude_config_dir = (
                    config_path.parent / profile.claude_config_dir
                ).resolve()
    return config
