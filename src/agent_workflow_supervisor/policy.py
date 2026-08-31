"""Pure routing and capacity policy."""

from __future__ import annotations

from dataclasses import dataclass

from agent_workflow_supervisor.config import AppConfig, CredentialProfileConfig, PolicyConfig
from agent_workflow_supervisor.models import AgentSession, WorkItem


@dataclass(frozen=True)
class ModelSelection:
    profile: str | None
    harness: str
    model: str | None
    capacity: int | None = None


def _selection_for_profile(name: str, policy: PolicyConfig) -> ModelSelection:
    profile = policy.model_profiles[name]
    return ModelSelection(name, profile.harness, profile.model, profile.capacity)


def select_model(item: WorkItem, policy: PolicyConfig) -> ModelSelection | None:
    """Resolve a work item to one effective harness/model selection."""

    if item.labels & policy.skip_labels:
        return None

    for rule in policy.routes:
        any_matches = not rule.labels_any or bool(item.labels & rule.labels_any)
        all_matches = not rule.labels_all or rule.labels_all <= item.labels
        if any_matches and all_matches:
            if rule.profile is not None:
                return _selection_for_profile(rule.profile, policy)
            assert rule.harness is not None
            return ModelSelection(
                None,
                rule.harness,
                policy.models.get(rule.harness),
            )

    for name in policy.model_profiles:
        if f"agent:{name}" in item.labels:
            return _selection_for_profile(name, policy)

    if policy.default_model_profile is not None:
        return _selection_for_profile(policy.default_model_profile, policy)

    return ModelSelection(
        None,
        policy.default_harness,
        policy.models.get(policy.default_harness),
    )


def route_work_item(item: WorkItem, policy: PolicyConfig) -> str | None:
    """Backward-compatible harness-only view of :func:`select_model`."""

    selection = select_model(item, policy)
    return selection.harness if selection else None


def active_count(sessions: list[AgentSession], harness: str) -> int:
    return sum(
        1
        for session in sessions
        if session.role == "worker" and session.harness == harness and session.active
    )


def has_capacity(sessions: list[AgentSession], harness: str, policy: PolicyConfig) -> bool:
    limit = policy.capacity.get(harness)
    return limit is None or active_count(sessions, harness) < limit


def has_model_capacity(
    sessions: list[AgentSession], selection: ModelSelection, policy: PolicyConfig
) -> bool:
    """Enforce both legacy harness limits and the selected profile's safe limit.

    AO's session listing currently exposes the harness but not the exact model.
    Counting all sessions on the same harness is conservative and prevents a
    local backend from being overcommitted when multiple profiles share it.
    """

    if not has_capacity(sessions, selection.harness, policy):
        return False
    if selection.capacity is None:
        return True
    return active_count(sessions, selection.harness) < selection.capacity


def requires_approval(item: WorkItem, policy: PolicyConfig) -> bool:
    return bool(item.labels & policy.approval_labels)


def select_credential_profile(
    sessions: list[AgentSession], harness: str, config: AppConfig
) -> tuple[str, CredentialProfileConfig] | None:
    """Choose the least-active eligible non-secret credential profile."""

    names = config.policy.credential_profiles.get(harness, [])
    if not names:
        return None

    candidates: list[tuple[int, int, str, CredentialProfileConfig]] = []
    for order, name in enumerate(names):
        profile = config.credentials.profiles[name]
        count = sum(
            1
            for session in sessions
            if session.role == "worker"
            and session.harness == harness
            and session.project_id == profile.execution_project_id
            and session.active
        )
        if count < profile.max_workers:
            candidates.append((count, order, name, profile))

    if not candidates:
        return None
    _, _, name, profile = min(candidates)
    return name, profile
