from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

import agent_workflow_supervisor.locking as locking_module
from agent_workflow_supervisor.config import (
    AppConfig,
    CredentialProfileConfig,
    CredentialsConfig,
    ModelProfileConfig,
    PolicyConfig,
    ProjectConfig,
    RouteRule,
    SupervisorConfig,
    TrackerConfig,
)
from agent_workflow_supervisor.graph import SupervisorDependencies, build_supervisor_graph
from agent_workflow_supervisor.models import (
    AgentSession,
    ChangeRequest,
    CheckResult,
    ReviewResult,
    WorkItem,
)


@pytest.fixture(autouse=True)
def isolated_acquisition_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(locking_module, "LOCK_ROOT", tmp_path / "acquisitions")


class FakeRunner:
    def __init__(self, item: WorkItem, review: ReviewResult | None = None) -> None:
        self.item = item
        self.review = review
        self.sessions: list[AgentSession] = []
        self.spawned = 0
        self.triggered: list[str] = []
        self.cancelled_reviews: list[str] = []
        self.terminated: list[str] = []
        self.spawn_calls: list[tuple[str, str | None]] = []
        self.spawn_details: list[tuple[str, str | None]] = []
        self.prompts: list[str] = []

    def list_sessions(self, project_id: str) -> list[AgentSession]:
        return [session for session in self.sessions if session.project_id == project_id]

    def get_session(self, session_id: str) -> AgentSession | None:
        return next((session for session in self.sessions if session.id == session_id), None)

    def spawn_worker(self, *, project_id, work_item, harness, model, credential_profile, prompt):
        self.spawned += 1
        self.spawn_calls.append((project_id, credential_profile))
        self.spawn_details.append((harness, model))
        self.prompts.append(prompt)
        session = AgentSession(
            f"worker-{self.spawned}",
            "worker",
            "working",
            harness,
            work_item_id=work_item.id,
            project_id=project_id,
        )
        self.sessions.append(session)
        return session

    def get_review(self, session_id: str) -> ReviewResult | None:
        return self.review

    def trigger_review(self, session_id: str) -> None:
        self.triggered.append(session_id)

    def cancel_review(self, session_id: str) -> None:
        self.cancelled_reviews.append(session_id)

    def send(self, session_id: str, message: str) -> None:
        pass

    def terminate(self, project_id: str, session_id: str) -> None:
        self.terminated.append(session_id)
        self.sessions = [
            replace(session, terminated=True, status="terminated")
            if session.id == session_id
            else session
            for session in self.sessions
        ]


class FakeTracker:
    def __init__(self, item: WorkItem, change: ChangeRequest | None = None) -> None:
        self.item = item
        self.change = change
        self.merged: list[tuple[str, str]] = []

    def get_work_item(self, work_item_id: str) -> WorkItem:
        return replace(self.item, id=work_item_id)

    def get_change(self, change_id: str) -> ChangeRequest:
        assert self.change is not None
        return self.change

    def merge_change(self, change_id: str, expected_head_sha: str) -> None:
        self.merged.append((change_id, expected_head_sha))


def config(
    *,
    shadow: bool,
    approval_labels: set[str] | None = None,
    merge_mode: str = "automatic",
) -> AppConfig:
    return AppConfig(
        supervisor=SupervisorConfig(shadow_mode=shadow),
        project=ProjectConfig(id="demo"),
        tracker=TrackerConfig(repository="owner/repo"),
        policy=PolicyConfig(
            merge_mode=merge_mode,
            default_harness="claude-code",
            capacity={"claude-code": 2, "codex": 1},
            approval_labels=approval_labels or set(),
            routes=[RouteRule(harness="codex", labels_any={"art"})],
        ),
    )


def run_config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def test_shadow_mode_plans_without_spawning() -> None:
    item = WorkItem("1", "code")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=True), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("shadow"),
    )

    assert result["status"] == "planned_worker"
    assert runner.spawned == 0


def test_existing_worker_is_reused_even_when_harness_is_at_capacity() -> None:
    item = WorkItem("1", "code")
    runner = FakeRunner(item)
    runner.sessions = [
        AgentSession(
            "matching",
            "worker",
            "working",
            "claude-code",
            work_item_id="1",
            project_id="demo",
        ),
        AgentSession(
            "other",
            "worker",
            "working",
            "claude-code",
            work_item_id="2",
            project_id="demo",
        ),
    ]
    tracker = FakeTracker(item)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("reuse"),
    )

    assert result["worker_id"] == "matching"
    assert result["status"] == "worker_running"
    assert runner.spawned == 0


def test_repository_qualified_ao_issue_id_reuses_numeric_dispatch_worker() -> None:
    item = WorkItem("194", "code")
    runner = FakeRunner(item)
    runner.sessions = [
        AgentSession(
            "qualified-worker",
            "worker",
            "working",
            "claude-code",
            work_item_id="github:owner/repo#194",
            project_id="demo",
        )
    ]
    tracker = FakeTracker(item)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "194", "events": []},
        config=run_config("qualified-worker"),
    )

    assert result["worker_id"] == "qualified-worker"
    assert runner.spawned == 0


def test_review_watchdog_restarts_a_timed_out_run_once() -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    clock = Clock(started)
    item = WorkItem("1", "code")
    runner = FakeRunner(
        item,
        ReviewResult(
            status="running",
            target_sha="abc",
            run_id="run-1",
            started_at=started.isoformat(),
        ),
    )
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.supervisor.review_timeout_seconds = 60
    app_config.supervisor.review_max_attempts = 2
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker, now=clock), InMemorySaver()
    )
    invocation = run_config("review-timeout")

    first = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    assert first["status"] == "review_pending"
    assert first["review_attempts"] == 1
    assert runner.triggered == []

    clock.advance(61)
    restarted = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )

    assert restarted["status"] == "review_pending"
    assert restarted["review_attempts"] == 2
    assert runner.cancelled_reviews == ["worker-1"]
    assert runner.triggered == ["worker-1"]


def test_review_watchdog_stalls_after_bounded_attempts() -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    clock = Clock(started)
    item = WorkItem("1", "code")
    runner = FakeRunner(
        item,
        ReviewResult(
            status="running",
            target_sha="abc",
            run_id="run-1",
            started_at=started.isoformat(),
        ),
    )
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.supervisor.review_timeout_seconds = 60
    app_config.supervisor.review_max_attempts = 2
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker, now=clock), InMemorySaver()
    )
    invocation = run_config("review-stalled")

    graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    clock.advance(61)
    graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    clock.advance(61)
    stalled = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )

    assert stalled["status"] == "review_stalled"
    assert stalled["review_attempts"] == 2
    assert "timed out after 2 attempt(s)" in stalled["last_error"]
    assert runner.cancelled_reviews == ["worker-1"]
    assert runner.triggered == ["worker-1"]


def test_external_new_review_run_recovers_a_stalled_workflow() -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    clock = Clock(started)
    item = WorkItem("1", "code")
    runner = FakeRunner(
        item,
        ReviewResult(
            status="running",
            target_sha="abc",
            run_id="run-1",
            started_at=started.isoformat(),
        ),
    )
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.supervisor.review_timeout_seconds = 60
    app_config.supervisor.review_max_attempts = 1
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker, now=clock), InMemorySaver()
    )
    invocation = run_config("review-manual-recovery")

    graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    clock.advance(61)
    stalled = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    assert stalled["status"] == "review_stalled"

    runner.review = replace(
        runner.review,
        run_id="run-2",
        started_at=clock().isoformat(),
    )
    recovered = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )

    assert recovered["status"] == "review_pending"
    assert recovered["review_run_id"] == "run-2"
    assert recovered["review_attempts"] == 1
    assert recovered["last_error"] == ""


def test_new_review_target_resets_the_attempt_budget() -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    clock = Clock(started)
    item = WorkItem("1", "code")
    runner = FakeRunner(
        item,
        ReviewResult(
            status="running",
            target_sha="old",
            run_id="run-old",
            started_at=started.isoformat(),
        ),
    )
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.supervisor.review_timeout_seconds = 60
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker, now=clock), InMemorySaver()
    )
    invocation = run_config("review-new-target")

    graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    clock.advance(61)
    retried = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    assert retried["review_attempts"] == 2

    runner.review = ReviewResult(status="needs_review", target_sha="new")
    fresh = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )

    assert fresh["status"] == "review_pending"
    assert fresh["review_triggered_for_sha"] == "new"
    assert fresh["review_attempts"] == 1
    assert runner.triggered == ["worker-1", "worker-1"]


def test_failed_review_retries_immediately_without_cancel() -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    clock = Clock(started)
    item = WorkItem("1", "code")
    runner = FakeRunner(
        item,
        ReviewResult(
            status="failed",
            target_sha="abc",
            run_id="failed-run",
            started_at=started.isoformat(),
        ),
    )
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker, now=clock), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("review-failed"),
    )

    assert result["status"] == "review_pending"
    assert result["review_attempts"] == 2
    assert runner.cancelled_reviews == []
    assert runner.triggered == ["worker-1"]


def test_missing_review_record_is_retriggered_after_timeout() -> None:
    started = datetime(2026, 9, 1, tzinfo=UTC)
    clock = Clock(started)
    item = WorkItem("1", "code")
    runner = FakeRunner(item, ReviewResult(status="needs_review", target_sha="abc"))
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.supervisor.review_timeout_seconds = 60
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker, now=clock), InMemorySaver()
    )
    invocation = run_config("review-missing")

    graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    runner.review = None
    clock.advance(61)
    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )

    assert result["status"] == "review_pending"
    assert result["review_attempts"] == 2
    assert runner.triggered == ["worker-1", "worker-1"]
    assert runner.cancelled_reviews == []


def test_approved_change_merges_and_cleans_worker() -> None:
    item = WorkItem("1", "code")
    review = ReviewResult(
        status="complete",
        verdict="approved",
        change_id="7",
        target_sha="abc",
    )
    change = ChangeRequest(
        "7", "https://example/pr/7", "OPEN", "abc", mergeable=True, merge_state="CLEAN"
    )
    runner = FakeRunner(item, review)
    tracker = FakeTracker(item, change)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("merge"),
    )

    assert result["status"] == "completed"
    assert tracker.merged == [("7", "abc")]
    assert runner.terminated == ["worker-1"]

    repeated = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("merge"),
    )
    assert repeated["status"] == "completed"
    assert tracker.merged == [("7", "abc")]
    assert runner.spawned == 1


def test_protected_change_interrupts_before_merge_and_can_resume() -> None:
    item = WorkItem("9", "art", labels=frozenset({"art"}))
    review = ReviewResult(
        status="complete",
        verdict="approved",
        change_id="8",
        target_sha="def",
    )
    change = ChangeRequest(
        "8", "https://example/pr/8", "OPEN", "def", mergeable=True, merge_state="CLEAN"
    )
    runner = FakeRunner(item, review)
    tracker = FakeTracker(item, change)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False, approval_labels={"art"}), runner, tracker),
        InMemorySaver(),
    )
    invocation = run_config("approval")

    paused = graph.invoke(
        {"project_id": "demo", "work_item_id": "9", "events": []},
        config=invocation,
    )
    assert paused["__interrupt__"]
    assert tracker.merged == []

    resumed = graph.invoke(Command(resume={"action": "approve"}), config=invocation)
    assert resumed["status"] == "completed"
    assert tracker.merged == [("8", "def")]


def test_manual_merge_mode_interrupts_for_non_protected_change() -> None:
    item = WorkItem("1", "code")
    review = ReviewResult(
        status="complete",
        verdict="approved",
        change_id="7",
        target_sha="abc",
    )
    change = ChangeRequest(
        "7", "https://example/pr/7", "OPEN", "abc", mergeable=True, merge_state="CLEAN"
    )
    runner = FakeRunner(item, review)
    tracker = FakeTracker(item, change)
    graph = build_supervisor_graph(
        SupervisorDependencies(
            config(shadow=False, merge_mode="manual"),
            runner,
            tracker,
        ),
        InMemorySaver(),
    )
    invocation = run_config("manual-merge")

    paused = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=invocation,
    )
    assert paused["__interrupt__"]
    assert tracker.merged == []

    resumed = graph.invoke(Command(resume={"action": "approve"}), config=invocation)
    assert resumed["status"] == "completed"
    assert tracker.merged == [("7", "abc")]


def test_protected_change_revalidates_ci_after_human_approval() -> None:
    item = WorkItem("9", "art", labels=frozenset({"art"}))
    review = ReviewResult(
        status="complete",
        verdict="approved",
        change_id="8",
        target_sha="def",
    )
    change = ChangeRequest(
        "8", "https://example/pr/8", "OPEN", "def", mergeable=True, merge_state="CLEAN"
    )
    runner = FakeRunner(item, review)
    tracker = FakeTracker(item, change)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False, approval_labels={"art"}), runner, tracker),
        InMemorySaver(),
    )
    invocation = run_config("approval-ci-change")

    paused = graph.invoke(
        {"project_id": "demo", "work_item_id": "9", "events": []},
        config=invocation,
    )
    assert paused["__interrupt__"]

    tracker.change = replace(change, checks=(CheckResult("tests", "FAILURE"),))
    resumed = graph.invoke(Command(resume={"action": "approve"}), config=invocation)

    assert resumed["status"] == "waiting_change_gate"
    assert resumed["approval_change_id"] == "8"
    assert resumed["approval_target_sha"] == "def"
    assert tracker.merged == []
    assert runner.terminated == []


def test_approved_review_without_target_sha_fails_closed() -> None:
    item = WorkItem("1", "code")
    review = ReviewResult(status="complete", verdict="approved", change_id="7")
    change = ChangeRequest(
        "7", "https://example/pr/7", "OPEN", "abc", mergeable=True, merge_state="CLEAN"
    )
    runner = FakeRunner(item, review)
    tracker = FakeTracker(item, change)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("missing-review-target"),
    )

    assert result["status"] == "review_invalid"
    assert "target head SHA" in result["last_error"]
    assert tracker.merged == []
    assert runner.terminated == []


def test_route_change_never_spawns_second_worker_for_same_item() -> None:
    item = WorkItem("1", "art", labels=frozenset({"art"}))
    runner = FakeRunner(item)
    runner.sessions = [
        AgentSession(
            "existing-claude",
            "worker",
            "working",
            "claude-code",
            work_item_id="1",
            project_id="demo",
        )
    ]
    tracker = FakeTracker(item)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("route-conflict"),
    )

    assert result["status"] == "worker_route_conflict"
    assert "existing-claude (claude-code)" in result["last_error"]
    assert runner.spawned == 0
    assert len([session for session in runner.sessions if session.active]) == 1


def test_worker_in_retired_credential_project_remains_visible() -> None:
    item = WorkItem("1", "code")
    runner = FakeRunner(item)
    runner.sessions = [
        AgentSession(
            "old-account-worker",
            "worker",
            "working",
            "claude-code",
            work_item_id="1",
            project_id="demo-claude-old",
        )
    ]
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.credentials = CredentialsConfig(
        profiles={
            "claude-old": CredentialProfileConfig(
                execution_project_id="demo-claude-old", max_workers=1
            )
        }
    )
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("retired-profile"),
    )

    assert result["status"] == "worker_running"
    assert result["worker_id"] == "old-account-worker"
    assert result["execution_project_id"] == "demo-claude-old"
    assert runner.spawned == 0


class ConcurrentRunner(FakeRunner):
    def __init__(self, item: WorkItem) -> None:
        super().__init__(item)
        self.first_scan = threading.Barrier(2)
        self.thread_state = threading.local()

    def list_sessions(self, project_id: str) -> list[AgentSession]:
        calls = getattr(self.thread_state, "calls", 0) + 1
        self.thread_state.calls = calls
        if calls == 1:
            self.first_scan.wait(timeout=5)
        return super().list_sessions(project_id)


def test_concurrent_reconciliation_acquires_only_one_worker(tmp_path: Path) -> None:
    item = WorkItem("1", "code")
    runner = ConcurrentRunner(item)
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.supervisor.runtime_dir = tmp_path
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )
    invocation = run_config("concurrent-acquisition")
    input_state = {"project_id": "demo", "work_item_id": "1", "events": []}

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _: graph.invoke(input_state, config=invocation),
                range(2),
            )
        )

    assert runner.spawned == 1
    assert {result["worker_id"] for result in results} == {"worker-1"}
    assert len([session for session in runner.sessions if session.active]) == 1


def test_concurrent_work_items_cannot_exceed_harness_capacity() -> None:
    item = WorkItem("1", "code")
    runner = ConcurrentRunner(item)
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.policy.capacity["claude-code"] = 1
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                graph.invoke,
                {"project_id": "demo", "work_item_id": work_item_id, "events": []},
                config=run_config(f"capacity-{work_item_id}"),
            )
            for work_item_id in ("1", "2")
        ]
        results = [future.result() for future in futures]

    assert runner.spawned == 1
    assert {result["status"] for result in results} == {"worker_running", "waiting_capacity"}
    assert len([session for session in runner.sessions if session.active]) == 1


def test_concurrent_work_items_cannot_exceed_credential_profile_capacity() -> None:
    item = WorkItem("1", "code")
    runner = ConcurrentRunner(item)
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.credentials = CredentialsConfig(
        profiles={
            "claude-work": CredentialProfileConfig(
                execution_project_id="demo-claude-work",
                max_workers=1,
            )
        }
    )
    app_config.policy.credential_profiles = {"claude-code": ["claude-work"]}
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                graph.invoke,
                {"project_id": "demo", "work_item_id": work_item_id, "events": []},
                config=run_config(f"profile-capacity-{work_item_id}"),
            )
            for work_item_id in ("1", "2")
        ]
        results = [future.result() for future in futures]

    assert runner.spawned == 1
    assert {result["status"] for result in results} == {
        "worker_running",
        "waiting_profile_capacity",
    }
    assert runner.spawn_calls == [("demo-claude-work", "claude-work")]


def test_model_profile_capacity_is_global_across_projects() -> None:
    item = WorkItem("1", "local model work")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)

    def configured(project_id: str) -> AppConfig:
        app_config = config(shadow=False)
        app_config.project.id = project_id
        app_config.policy.model_profiles = {
            "local-qwen": ModelProfileConfig(
                harness="local",
                model="qwen",
                capacity=1,
            )
        }
        app_config.policy.default_model_profile = "local-qwen"
        return app_config

    game = build_supervisor_graph(
        SupervisorDependencies(configured("game"), runner, tracker), InMemorySaver()
    )
    website = build_supervisor_graph(
        SupervisorDependencies(configured("website"), runner, tracker), InMemorySaver()
    )

    first = game.invoke(
        {"project_id": "game", "work_item_id": "1", "events": []},
        config=run_config("global-model-game"),
    )
    second = website.invoke(
        {"project_id": "website", "work_item_id": "2", "events": []},
        config=run_config("global-model-website"),
    )

    assert first["status"] == "worker_running"
    assert second["status"] == "waiting_capacity"
    assert runner.spawned == 1


def test_credential_capacity_is_global_for_shared_login_across_projects(
    tmp_path: Path,
) -> None:
    item = WorkItem("1", "Claude work")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    shared_login = tmp_path / "claude-shared"

    def configured(project_id: str) -> AppConfig:
        app_config = config(shadow=False)
        app_config.project.id = project_id
        profile_name = f"claude-{project_id}"
        app_config.credentials = CredentialsConfig(
            profiles={
                profile_name: CredentialProfileConfig(
                    execution_project_id=f"{project_id}-claude-shared",
                    max_workers=1,
                    claude_config_dir=shared_login,
                )
            }
        )
        app_config.policy.credential_profiles = {"claude-code": [profile_name]}
        return app_config

    game = build_supervisor_graph(
        SupervisorDependencies(configured("game"), runner, tracker), InMemorySaver()
    )
    website = build_supervisor_graph(
        SupervisorDependencies(configured("website"), runner, tracker), InMemorySaver()
    )

    first = game.invoke(
        {"project_id": "game", "work_item_id": "1", "events": []},
        config=run_config("global-account-game"),
    )
    second = website.invoke(
        {"project_id": "website", "work_item_id": "2", "events": []},
        config=run_config("global-account-website"),
    )

    assert first["status"] == "worker_running"
    assert second["status"] == "waiting_profile_capacity"
    assert runner.spawned == 1


def test_direct_base_binding_cannot_alias_a_pooled_login_capacity(tmp_path: Path) -> None:
    item = WorkItem("1", "Claude work")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    shared_login = tmp_path / "claude-work"
    app_config = config(shadow=False)
    app_config.project.id = "game"
    app_config.credentials = CredentialsConfig(
        profiles={
            "claude-default": CredentialProfileConfig(
                execution_project_id="game",
                max_workers=1,
            ),
            "claude-work": CredentialProfileConfig(
                execution_project_id="game-claude-work",
                max_workers=1,
                claude_config_dir=shared_login,
            ),
        }
    )
    app_config.policy.credential_profiles = {"claude-code": ["claude-default", "claude-work"]}
    locking_module.record_execution_credential_identity("game", shared_login)
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )

    first = graph.invoke(
        {"project_id": "game", "work_item_id": "1", "events": []},
        config=run_config("bound-base-first"),
    )
    second = graph.invoke(
        {"project_id": "game", "work_item_id": "2", "events": []},
        config=run_config("bound-base-second"),
    )

    assert first["status"] == "worker_running"
    assert second["status"] == "waiting_profile_capacity"
    assert runner.spawn_calls == [("game", "claude-default")]


def test_pending_account_switch_blocks_new_worker() -> None:
    item = WorkItem("1", "code")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    locking_module.mark_account_switch_pending("demo", "switch-1")
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("pending-account-switch"),
    )

    assert result["status"] == "waiting_account_switch"
    assert runner.spawned == 0


def test_unrelated_malformed_acquisition_record_does_not_block_project() -> None:
    item = WorkItem("1", "code")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    locking_module.LOCK_ROOT.mkdir(parents=True)
    (locking_module.LOCK_ROOT / "damaged.json").write_text("{not-json", encoding="utf-8")
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("malformed-foreign-reservation"),
    )

    assert result["status"] == "worker_running"
    assert runner.spawned == 1


def test_pending_other_work_item_reserves_harness_capacity() -> None:
    item = WorkItem("2", "code")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.policy.capacity["claude-code"] = 1
    with locking_module.work_item_acquisition_lock("demo", "1"):
        locking_module.record_pending_acquisition(
            "demo",
            "1",
            execution_project_id="demo",
            harness="claude-code",
        )
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "2", "events": []},
        config=run_config("pending-capacity"),
    )

    assert result["status"] == "waiting_capacity"
    assert runner.spawned == 0


def test_different_runtime_and_profile_configs_share_one_acquisition(tmp_path: Path) -> None:
    item = WorkItem("1", "code")
    runner = ConcurrentRunner(item)
    tracker = FakeTracker(item)

    def configured(profile_name: str, execution_project_id: str, runtime: Path) -> AppConfig:
        app_config = config(shadow=False)
        app_config.supervisor.runtime_dir = runtime
        app_config.credentials = CredentialsConfig(
            profiles={
                profile_name: CredentialProfileConfig(
                    execution_project_id=execution_project_id,
                    max_workers=1,
                )
            }
        )
        app_config.policy.credential_profiles = {"claude-code": [profile_name]}
        return app_config

    graph_a = build_supervisor_graph(
        SupervisorDependencies(
            configured("claude-a", "demo-claude-a", tmp_path / "runtime-a"),
            runner,
            tracker,
        ),
        InMemorySaver(),
    )
    graph_b = build_supervisor_graph(
        SupervisorDependencies(
            configured("claude-b", "demo-claude-b", tmp_path / "runtime-b"),
            runner,
            tracker,
        ),
        InMemorySaver(),
    )
    input_state = {"project_id": "demo", "work_item_id": "1", "events": []}

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                graph.invoke,
                input_state,
                config=run_config(f"cross-config-{index}"),
            )
            for index, graph in enumerate((graph_a, graph_b))
        ]
        results = [future.result() for future in futures]

    assert runner.spawned == 1
    assert {result["worker_id"] for result in results} == {"worker-1"}
    assert len([session for session in runner.sessions if session.active]) == 1


def test_pending_acquisition_fails_closed_after_uncertain_spawn() -> None:
    item = WorkItem("1", "code")
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    with locking_module.work_item_acquisition_lock("demo", "1"):
        locking_module.record_pending_acquisition(
            "demo",
            "1",
            execution_project_id="demo-claude-old",
            harness="claude-code",
        )
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("pending-acquisition"),
    )

    assert result["status"] == "worker_acquisition_pending"
    assert runner.spawned == 0


class TransientLookupRunner(FakeRunner):
    def list_sessions(self, project_id: str) -> list[AgentSession]:
        return []

    def get_session(self, session_id: str) -> AgentSession | None:
        return None


def test_transient_reserved_worker_lookup_never_discards_reservation() -> None:
    item = WorkItem("1", "code")
    runner = TransientLookupRunner(item)
    runner.sessions = [
        AgentSession(
            "reserved-worker",
            "worker",
            "working",
            "claude-code",
            work_item_id="1",
            project_id="demo-claude-a",
        )
    ]
    tracker = FakeTracker(item)
    app_config = config(shadow=False)
    app_config.credentials = CredentialsConfig(
        profiles={
            "claude-b": CredentialProfileConfig(execution_project_id="demo-claude-b", max_workers=1)
        }
    )
    app_config.policy.credential_profiles = {"claude-code": ["claude-b"]}
    with locking_module.work_item_acquisition_lock("demo", "1"):
        locking_module.record_acquired_worker(
            "demo",
            "1",
            execution_project_id="demo-claude-a",
            harness="claude-code",
            worker_id="reserved-worker",
        )
    graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("transient-lookup"),
    )

    assert result["status"] == "worker_reservation_unverified"
    assert runner.spawned == 0
    with locking_module.work_item_acquisition_lock("demo", "1"):
        reservation = locking_module.read_acquisition_record("demo", "1")
    assert reservation is not None
    assert reservation.worker_id == "reserved-worker"


class UncertainSpawnRunner(FakeRunner):
    def __init__(self, item: WorkItem, review: ReviewResult) -> None:
        super().__init__(item, review)
        self.lose_first_response = True

    def spawn_worker(self, **kwargs):
        worker = super().spawn_worker(**kwargs)
        if self.lose_first_response:
            self.lose_first_response = False
            raise RuntimeError("spawn response lost")
        return worker


def test_uncertain_spawn_is_repaired_and_cleaned_up() -> None:
    item = WorkItem("1", "code")
    review = ReviewResult(
        status="complete",
        verdict="approved",
        change_id="7",
        target_sha="abc",
    )
    change = ChangeRequest(
        "7", "https://example/pr/7", "OPEN", "abc", mergeable=True, merge_state="CLEAN"
    )
    runner = UncertainSpawnRunner(item, review)
    tracker = FakeTracker(item, change)
    app_config = config(shadow=False)
    first_graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )

    with pytest.raises(RuntimeError, match="spawn response lost"):
        first_graph.invoke(
            {"project_id": "demo", "work_item_id": "1", "events": []},
            config=run_config("uncertain-first"),
        )
    with locking_module.work_item_acquisition_lock("demo", "1"):
        pending = locking_module.read_acquisition_record("demo", "1")
    assert pending is not None
    assert pending.state == "pending"

    recovery_graph = build_supervisor_graph(
        SupervisorDependencies(app_config, runner, tracker), InMemorySaver()
    )
    recovered = recovery_graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("uncertain-recovery"),
    )

    assert recovered["status"] == "completed"
    assert runner.spawned == 1
    assert runner.terminated == ["worker-1"]
    with locking_module.work_item_acquisition_lock("demo", "1"):
        assert locking_module.read_acquisition_record("demo", "1") is None


def test_divergent_config_recovers_uncertain_spawn_from_reserved_project() -> None:
    item = WorkItem("1", "code")
    review = ReviewResult(
        status="complete",
        verdict="approved",
        change_id="7",
        target_sha="abc",
    )
    change = ChangeRequest(
        "7", "https://example/pr/7", "OPEN", "abc", mergeable=True, merge_state="CLEAN"
    )
    runner = UncertainSpawnRunner(item, review)
    tracker = FakeTracker(item, change)

    first_config = config(shadow=False)
    first_config.credentials = CredentialsConfig(
        profiles={
            "claude-a": CredentialProfileConfig(execution_project_id="demo-claude-a", max_workers=1)
        }
    )
    first_config.policy.credential_profiles = {"claude-code": ["claude-a"]}
    first_graph = build_supervisor_graph(
        SupervisorDependencies(first_config, runner, tracker), InMemorySaver()
    )
    with pytest.raises(RuntimeError, match="spawn response lost"):
        first_graph.invoke(
            {"project_id": "demo", "work_item_id": "1", "events": []},
            config=run_config("divergent-uncertain-first"),
        )

    second_config = config(shadow=False)
    second_config.credentials = CredentialsConfig(
        profiles={
            "claude-b": CredentialProfileConfig(execution_project_id="demo-claude-b", max_workers=1)
        }
    )
    second_config.policy.credential_profiles = {"claude-code": ["claude-b"]}
    recovery_graph = build_supervisor_graph(
        SupervisorDependencies(second_config, runner, tracker), InMemorySaver()
    )
    recovered = recovery_graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("divergent-uncertain-recovery"),
    )

    assert recovered["status"] == "completed"
    assert runner.spawned == 1
    assert runner.spawn_calls == [("demo-claude-a", "claude-a")]
    assert runner.terminated == ["worker-1"]
    with locking_module.work_item_acquisition_lock("demo", "1"):
        assert locking_module.read_acquisition_record("demo", "1") is None


def test_route_conflict_promotes_visible_pending_worker() -> None:
    item = WorkItem("1", "art", labels=frozenset({"art"}))
    runner = FakeRunner(item)
    runner.sessions = [
        AgentSession(
            "uncertain-worker",
            "worker",
            "working",
            "claude-code",
            work_item_id="1",
            project_id="demo",
        )
    ]
    tracker = FakeTracker(item)
    with locking_module.work_item_acquisition_lock("demo", "1"):
        locking_module.record_pending_acquisition(
            "demo",
            "1",
            execution_project_id="demo",
            harness="claude-code",
        )
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("route-conflict-pending"),
    )

    assert result["status"] == "worker_route_conflict"
    assert runner.spawned == 0
    with locking_module.work_item_acquisition_lock("demo", "1"):
        reservation = locking_module.read_acquisition_record("demo", "1")
    assert reservation is not None
    assert reservation.state == "worker"
    assert reservation.worker_id == "uncertain-worker"


def test_stale_review_never_merges() -> None:
    item = WorkItem("1", "code")
    review = ReviewResult(
        status="complete",
        verdict="approved",
        change_id="7",
        target_sha="old",
    )
    change = ChangeRequest(
        "7", "https://example/pr/7", "OPEN", "new", mergeable=True, merge_state="CLEAN"
    )
    runner = FakeRunner(item, review)
    tracker = FakeTracker(item, change)
    graph = build_supervisor_graph(
        SupervisorDependencies(config(shadow=False), runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "1", "events": []},
        config=run_config("stale"),
    )

    assert result["status"] == "review_stale"
    assert tracker.merged == []


def test_claude_workers_use_least_active_isolated_login() -> None:
    item = WorkItem("2", "code")
    runner = FakeRunner(item)
    runner.sessions = [
        AgentSession(
            "existing",
            "worker",
            "working",
            "claude-code",
            work_item_id="1",
            project_id="demo-claude-primary",
        )
    ]
    tracker = FakeTracker(item)
    multi_login = config(shadow=False)
    multi_login.credentials = CredentialsConfig(
        profiles={
            "claude-primary": CredentialProfileConfig(execution_project_id="demo-claude-primary"),
            "claude-secondary": CredentialProfileConfig(
                execution_project_id="demo-claude-secondary"
            ),
        }
    )
    multi_login.policy.credential_profiles = {"claude-code": ["claude-primary", "claude-secondary"]}
    graph = build_supervisor_graph(
        SupervisorDependencies(multi_login, runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "2", "events": []},
        config=run_config("multi-login"),
    )

    assert result["credential_profile"] == "claude-secondary"
    assert result["execution_project_id"] == "demo-claude-secondary"
    assert runner.spawn_calls == [("demo-claude-secondary", "claude-secondary")]


def test_model_profile_is_passed_to_ao_runner() -> None:
    item = WorkItem("3", "local", labels=frozenset({"agent:local"}))
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    profile_config = config(shadow=False)
    profile_config.policy.default_model_profile = "claude"
    profile_config.policy.model_profiles = {
        "claude": ModelProfileConfig(harness="claude-code", model="claude-sonnet", capacity=2),
        "local": ModelProfileConfig(harness="opencode", model="ollama/qwen3-coder", capacity=1),
    }
    profile_config.policy.routes.insert(0, RouteRule(profile="local", labels_any={"agent:local"}))
    graph = build_supervisor_graph(
        SupervisorDependencies(profile_config, runner, tracker), InMemorySaver()
    )

    result = graph.invoke(
        {"project_id": "demo", "work_item_id": "3", "events": []},
        config=run_config("local-model"),
    )

    assert result["model_profile"] == "local"
    assert runner.spawn_details == [("opencode", "ollama/qwen3-coder")]


def test_report_only_worker_completes_without_review_or_pull_request() -> None:
    item = WorkItem("4", "Compare save formats", labels=frozenset({"research"}))
    runner = FakeRunner(item)
    tracker = FakeTracker(item)
    research_config = config(shadow=False)
    research_config.policy.routes.insert(0, RouteRule(harness="agy", labels_any={"research"}))
    research_config.policy.models["agy"] = "gemini-3.7-flash-high"
    research_config.policy.report_only_harnesses = {"agy"}
    graph = build_supervisor_graph(
        SupervisorDependencies(research_config, runner, tracker), InMemorySaver()
    )
    invocation = run_config("report-only")

    running = graph.invoke(
        {"project_id": "demo", "work_item_id": "4", "events": []},
        config=invocation,
    )

    assert running["status"] == "worker_running"
    assert running["report_only"]
    assert runner.spawn_details == [("agy", "gemini-3.7-flash-high")]
    assert "evidence-backed report" in runner.prompts[0]
    assert "Do not modify repository files" in runner.prompts[0]

    runner.sessions[0] = replace(runner.sessions[0], status="idle")
    completed = graph.invoke(
        {"project_id": "demo", "work_item_id": "4", "events": []},
        config=invocation,
    )

    assert completed["status"] == "completed"
    assert runner.terminated == ["worker-1"]
    assert tracker.merged == []


class InitiallyIdleRunner(FakeRunner):
    def spawn_worker(self, **kwargs):
        worker = super().spawn_worker(**kwargs)
        idle_worker = replace(worker, status="idle")
        self.sessions[-1] = idle_worker
        return idle_worker


def test_report_only_worker_is_not_completed_on_first_idle_observation() -> None:
    item = WorkItem("4", "Compare save formats", labels=frozenset({"research"}))
    runner = InitiallyIdleRunner(item)
    tracker = FakeTracker(item)
    research_config = config(shadow=False)
    research_config.policy.routes.insert(0, RouteRule(harness="agy", labels_any={"research"}))
    research_config.policy.report_only_harnesses = {"agy"}
    graph = build_supervisor_graph(
        SupervisorDependencies(research_config, runner, tracker), InMemorySaver()
    )
    invocation = run_config("report-only-initially-idle")

    first = graph.invoke(
        {"project_id": "demo", "work_item_id": "4", "events": []},
        config=invocation,
    )

    assert first["status"] == "worker_running"
    assert first["report_idle_observations"] == 1
    assert runner.terminated == []

    second = graph.invoke(
        {"project_id": "demo", "work_item_id": "4", "events": []},
        config=invocation,
    )

    assert second["status"] == "completed"
    assert runner.terminated == ["worker-1"]
