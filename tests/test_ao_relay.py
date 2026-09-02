from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from agent_workflow_supervisor.ao_relay import NativeAoRelay
from agent_workflow_supervisor.models import AgentSession


def _session(
    session_id: str,
    role: str,
    status: str,
    *,
    project_id: str = "demo",
    terminated: bool = False,
    work_item_id: str | None = None,
) -> AgentSession:
    return AgentSession(
        session_id,
        role,
        status,
        "claude-code",
        terminated=terminated,
        work_item_id=work_item_id,
        project_id=project_id,
        display_name=session_id,
        created_at=datetime.now(UTC).isoformat(),
        last_activity_at=datetime.now(UTC).isoformat(),
    )


class RelayRunner:
    def __init__(self, sessions: list[AgentSession]) -> None:
        self.sessions = {session.id: session for session in sessions}
        self.messages: dict[str, list[dict]] = {}
        self.sent: list[tuple[str, str]] = []

    def list_active_sessions(self) -> list[AgentSession]:
        return [session for session in self.sessions.values() if session.active]

    def get_session(self, session_id: str) -> AgentSession | None:
        return self.sessions.get(session_id)

    def conversation_messages(self, session_id: str, *, limit: int = 200) -> list[dict]:
        return self.messages.get(session_id, [])[-limit:]

    def send(self, session_id: str, message: str) -> bool:
        self.sent.append((session_id, message))
        return True


def test_native_worker_idle_report_is_relayed_once(tmp_path: Path) -> None:
    orchestrator = _session("demo-1", "orchestrator", "idle")
    worker = _session("demo-2", "worker", "working", work_item_id="42")
    runner = RelayRunner([orchestrator, worker])
    relay = NativeAoRelay("ao", state_dir=tmp_path, runner=runner)  # type: ignore[arg-type]

    relay.reconcile()
    assert runner.sent == []

    runner.sessions[worker.id] = _session(worker.id, "worker", "idle", work_item_id="42")
    runner.messages[worker.id] = [
        {
            "sequence": 17,
            "role": "assistant",
            "origin": "provider",
            "text": "Implemented the fix in commit abc123.",
        }
    ]
    relay.reconcile()
    relay.reconcile()

    assert len(runner.sent) == 1
    target, message = runner.sent[0]
    assert target == orchestrator.id
    assert "worker demo-2" in message
    assert "work item 42" in message
    assert "Implemented the fix in commit abc123." in message
    assert "untrusted worker output" in message


def test_automation_sender_proves_parent_with_multiple_orchestrators(tmp_path: Path) -> None:
    orchestrator_a = _session("demo-1", "orchestrator", "idle")
    orchestrator_b = _session("demo-2", "orchestrator", "working")
    worker = _session("demo-3", "worker", "working")
    runner = RelayRunner([orchestrator_a, orchestrator_b, worker])
    runner.messages[worker.id] = [
        {
            "sequence": 4,
            "role": "user",
            "origin": "automation",
            "text": "[from demo-2] Continue with the requested test.",
        }
    ]
    relay = NativeAoRelay("ao", state_dir=tmp_path, runner=runner)  # type: ignore[arg-type]

    relay.reconcile()

    tracked = relay.store.get(worker.id)
    assert tracked is not None
    assert tracked.orchestrator_id == orchestrator_b.id
    assert tracked.binding_source == "automation_sender"


def test_ambiguous_project_does_not_guess_parent(tmp_path: Path) -> None:
    runner = RelayRunner(
        [
            _session("demo-1", "orchestrator", "idle"),
            _session("demo-2", "orchestrator", "idle"),
            _session("demo-3", "worker", "working"),
        ]
    )
    relay = NativeAoRelay("ao", state_dir=tmp_path, runner=runner)  # type: ignore[arg-type]

    relay.reconcile()
    runner.sessions["demo-3"] = _session("demo-3", "worker", "idle")
    runner.messages["demo-3"] = [
        {
            "sequence": 8,
            "role": "assistant",
            "origin": "provider",
            "text": "Finished.",
        }
    ]
    relay.reconcile()

    tracked = relay.store.get("demo-3")
    assert tracked is not None
    assert tracked.orchestrator_id is None
    assert "no unambiguous active orchestrator" in str(tracked.notification_error)
    status = relay.store.status()
    assert status["pending_notifications"] == 1
    assert status["errors"][0]["worker_id"] == "demo-3"
    assert runner.sent == []


def test_terminal_tui_worker_sends_status_without_inventing_output(tmp_path: Path) -> None:
    orchestrator = _session("demo-1", "orchestrator", "idle")
    worker = _session("demo-2", "worker", "working")
    runner = RelayRunner([orchestrator, worker])
    relay = NativeAoRelay("ao", state_dir=tmp_path, runner=runner)  # type: ignore[arg-type]
    relay.reconcile()

    runner.sessions[worker.id] = _session(worker.id, "worker", "terminated", terminated=True)
    relay.reconcile()

    assert len(runner.sent) == 1
    assert "No structured final response is available" in runner.sent[0][1]


def test_waiting_input_is_relayed_once_per_lifecycle_event(tmp_path: Path) -> None:
    orchestrator = _session("demo-1", "orchestrator", "idle")
    worker = _session("demo-2", "worker", "working")
    runner = RelayRunner([orchestrator, worker])
    relay = NativeAoRelay("ao", state_dir=tmp_path, runner=runner)  # type: ignore[arg-type]
    relay.reconcile()

    waiting = _session(worker.id, "worker", "waiting_input")
    runner.sessions[worker.id] = waiting
    runner.messages[worker.id] = [
        {
            "sequence": 11,
            "role": "assistant",
            "origin": "provider",
            "text": "Which target branch should I use?",
        }
    ]
    relay.reconcile()
    relay.reconcile()

    assert len(runner.sent) == 1
    assert "waiting_input" in runner.sent[0][1]
    assert "Which target branch" in runner.sent[0][1]


def test_pending_update_falls_back_to_replacement_orchestrator(tmp_path: Path) -> None:
    old = _session("demo-1", "orchestrator", "idle")
    worker = _session("demo-2", "worker", "working")
    runner = RelayRunner([old, worker])
    relay = NativeAoRelay("ao", state_dir=tmp_path, runner=runner)  # type: ignore[arg-type]
    relay.reconcile()

    runner.sessions[old.id] = _session(old.id, "orchestrator", "terminated", terminated=True)
    replacement = _session("demo-3", "orchestrator", "idle")
    runner.sessions[replacement.id] = replacement
    runner.sessions[worker.id] = _session(worker.id, "worker", "idle")
    runner.messages[worker.id] = [
        {
            "sequence": 9,
            "role": "assistant",
            "origin": "provider",
            "text": "Finished after the orchestrator replacement.",
        }
    ]

    relay.reconcile()

    assert [target for target, _message in runner.sent] == [replacement.id]
    tracked = relay.store.get(worker.id)
    assert tracked is not None
    assert tracked.orchestrator_id == replacement.id
    assert tracked.binding_source == "replacement_project_orchestrator"
