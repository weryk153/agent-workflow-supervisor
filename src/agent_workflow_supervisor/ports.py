"""Provider ports. Adapters implement these protocols without changing the graph."""

from __future__ import annotations

from typing import Protocol

from agent_workflow_supervisor.models import (
    AgentSession,
    ChangeRequest,
    ReviewResult,
    WorkItem,
)


class RunnerPort(Protocol):
    def list_sessions(self, project_id: str) -> list[AgentSession]: ...

    def get_session(self, session_id: str) -> AgentSession | None: ...

    def spawn_worker(
        self,
        *,
        project_id: str,
        work_item: WorkItem,
        harness: str,
        model: str | None,
        credential_profile: str | None,
        prompt: str,
    ) -> AgentSession: ...

    def get_review(self, session_id: str) -> ReviewResult | None: ...

    def trigger_review(self, session_id: str) -> None: ...

    def send(self, session_id: str, message: str) -> None: ...

    def terminate(self, project_id: str, session_id: str) -> None: ...


class TrackerPort(Protocol):
    def get_work_item(self, work_item_id: str) -> WorkItem: ...

    def get_change(self, change_id: str) -> ChangeRequest: ...

    def merge_change(self, change_id: str, expected_head_sha: str) -> None: ...
