"""Domain values shared by workflow and provider adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class WorkItem:
    id: str
    title: str
    body: str = ""
    labels: frozenset[str] = frozenset()
    url: str = ""


@dataclass(frozen=True)
class AgentSession:
    id: str
    role: str
    status: str
    harness: str
    terminated: bool = False
    work_item_id: str | None = None
    project_id: str | None = None
    display_name: str = ""
    created_at: str = ""
    updated_at: str = ""
    last_activity_at: str = ""

    @property
    def active(self) -> bool:
        return not self.terminated and self.status not in {
            "completed",
            "done",
            "error",
            "exited",
            "failed",
            "merged",
            "terminated",
        }


ReviewVerdict = Literal["approved", "changes_requested", "pending", "unknown"]


@dataclass(frozen=True)
class ReviewResult:
    status: str
    verdict: ReviewVerdict = "unknown"
    feedback: str = ""
    change_id: str | None = None
    change_url: str = ""
    target_sha: str = ""
    run_id: str = ""
    started_at: str = ""


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str

    @property
    def successful(self) -> bool:
        return self.status.upper() in {"SUCCESS", "NEUTRAL", "SKIPPED"}


@dataclass(frozen=True)
class ChangeRequest:
    id: str
    url: str
    state: str
    head_sha: str
    draft: bool = False
    mergeable: bool = False
    merge_state: str = "UNKNOWN"
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def ready(self) -> bool:
        return (
            self.state.upper() == "OPEN"
            and not self.draft
            and self.mergeable
            and self.merge_state.upper() == "CLEAN"
            and all(check.successful for check in self.checks)
        )
