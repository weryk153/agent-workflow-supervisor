from __future__ import annotations

import pytest

import agent_workflow_supervisor.adapters.ao as ao_module
from agent_workflow_supervisor.adapters.ao import AoRunner
from agent_workflow_supervisor.models import WorkItem


class FakeCommandAdapter:
    def __init__(self, _command: str) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> str:
        self.calls.append(args)
        return "spawned session demo-1"

    def run_json(self, *args: str) -> dict:
        assert args == ("session", "get", "demo-1", "--json")
        return {
            "session": {
                "id": "demo-1",
                "kind": "worker",
                "status": "idle",
                "harness": "agy" if "agy" in self.calls[0] else "claude-code",
                "projectId": "demo",
            }
        }


@pytest.mark.parametrize(
    ("harness", "expected_mode"),
    [("agy", "tui"), ("claude-code", "chat")],
)
def test_spawn_uses_supported_mode(monkeypatch, harness: str, expected_mode: str) -> None:
    monkeypatch.setattr(ao_module, "CommandAdapter", FakeCommandAdapter)
    runner = AoRunner("ao")

    session = runner.spawn_worker(
        project_id="demo",
        work_item=WorkItem("42", "Research"),
        harness=harness,
        model=None,
        provider=None,
        credential_profile=None,
        prompt="Investigate without editing.",
    )

    call = runner.cli.calls[0]
    assert call[call.index("--mode") + 1] == expected_mode
    assert session.harness == harness


class ReviewCommandAdapter:
    def __init__(self, _command: str) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str) -> str:
        self.calls.append(args)
        return ""

    def run_json(self, *args: str) -> dict:
        self.calls.append(args)
        return {
            "reviews": [
                {
                    "status": "needs_review",
                    "prNumber": 12,
                    "prUrl": "https://github.com/owner/repo/pull/12",
                    "targetSha": "abc",
                    "latestRun": {
                        "id": "run-7",
                        "status": "failed",
                        "verdict": "unknown",
                        "createdAt": "2026-09-01T00:00:00Z",
                    },
                }
            ]
        }


def test_review_uses_latest_run_liveness_and_can_cancel(monkeypatch) -> None:
    monkeypatch.setattr(ao_module, "CommandAdapter", ReviewCommandAdapter)
    runner = AoRunner("ao")

    review = runner.get_review("demo-1")
    runner.cancel_review("demo-1")

    assert review is not None
    assert review.status == "failed"
    assert review.run_id == "run-7"
    assert review.started_at == "2026-09-01T00:00:00Z"
    assert runner.cli.calls == [
        ("review", "ls", "demo-1", "--json"),
        ("review", "cancel", "demo-1"),
    ]
