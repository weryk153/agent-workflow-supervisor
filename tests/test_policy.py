from agent_workflow_supervisor.config import (
    AppConfig,
    CredentialProfileConfig,
    CredentialsConfig,
    ModelProfileConfig,
    PolicyConfig,
    ProjectConfig,
    RouteRule,
    TrackerConfig,
)
from agent_workflow_supervisor.models import AgentSession, WorkItem
from agent_workflow_supervisor.policy import (
    has_capacity,
    has_model_capacity,
    requires_approval,
    route_work_item,
    select_credential_profile,
    select_model,
)


def policy() -> PolicyConfig:
    return PolicyConfig(
        default_harness="claude-code",
        skip_labels={"skip"},
        approval_labels={"art"},
        capacity={"claude-code": 2, "codex": 1},
        routes=[RouteRule(harness="codex", labels_any={"art", "agent:codex"})],
    )


def test_route_is_ordered_and_skip_has_priority() -> None:
    assert route_work_item(WorkItem("1", "art", labels=frozenset({"art"})), policy()) == "codex"
    assert (
        route_work_item(WorkItem("2", "skip", labels=frozenset({"skip", "art"})), policy()) is None
    )
    assert route_work_item(WorkItem("3", "code"), policy()) == "claude-code"


def test_capacity_counts_only_active_workers_for_same_harness() -> None:
    sessions = [
        AgentSession("1", "worker", "working", "claude-code"),
        AgentSession("2", "worker", "idle", "claude-code"),
        AgentSession("3", "orchestrator", "idle", "claude-code"),
        AgentSession("4", "worker", "terminated", "claude-code", terminated=True),
    ]
    assert not has_capacity(sessions, "claude-code", policy())
    assert has_capacity(sessions, "codex", policy())


def test_protected_label_requires_approval() -> None:
    assert requires_approval(WorkItem("1", "art", labels=frozenset({"art"})), policy())


def test_exited_session_is_not_active() -> None:
    assert not AgentSession("1", "worker", "exited", "claude-code").active


def test_profile_selection_respects_per_login_capacity_and_order() -> None:
    app_config = AppConfig(
        project=ProjectConfig(id="demo"),
        tracker=TrackerConfig(repository="owner/repo"),
        credentials=CredentialsConfig(
            profiles={
                "one": CredentialProfileConfig(execution_project_id="demo-one"),
                "two": CredentialProfileConfig(execution_project_id="demo-two"),
            }
        ),
        policy=PolicyConfig(credential_profiles={"claude-code": ["one", "two"]}),
    )
    sessions = [AgentSession("1", "worker", "working", "claude-code", project_id="demo-one")]

    selected = select_credential_profile(sessions, "claude-code", app_config)

    assert selected is not None
    assert selected[0] == "two"


def test_model_profile_selects_harness_model_and_profile_capacity() -> None:
    model_policy = PolicyConfig(
        default_model_profile="claude",
        model_profiles={
            "claude": ModelProfileConfig(harness="claude-code", model="claude-sonnet", capacity=2),
            "local": ModelProfileConfig(harness="opencode", model="ollama/qwen3-coder", capacity=1),
        },
        routes=[RouteRule(profile="local", labels_any={"agent:local"})],
    )

    local = select_model(WorkItem("1", "local", labels=frozenset({"agent:local"})), model_policy)
    default = select_model(WorkItem("2", "default"), model_policy)

    assert local is not None
    assert (local.profile, local.harness, local.model) == (
        "local",
        "opencode",
        "ollama/qwen3-coder",
    )
    assert default is not None and default.profile == "claude"
    sessions = [AgentSession("1", "worker", "working", "opencode")]
    assert not has_model_capacity(sessions, local, model_policy)

    model_policy.routes = []
    namespaced = select_model(
        WorkItem("3", "local", labels=frozenset({"agent:local"})), model_policy
    )
    assert namespaced is not None and namespaced.profile == "local"
