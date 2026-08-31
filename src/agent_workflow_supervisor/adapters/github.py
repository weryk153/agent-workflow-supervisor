"""GitHub issue and pull-request adapter implemented through gh."""

from __future__ import annotations

from agent_workflow_supervisor.adapters.command import CommandAdapter
from agent_workflow_supervisor.models import ChangeRequest, CheckResult, WorkItem


class GitHubTracker:
    def __init__(self, repository: str, command: str = "gh") -> None:
        self.repository = repository
        self.cli = CommandAdapter(command)

    def get_work_item(self, work_item_id: str) -> WorkItem:
        raw = self.cli.run_json(
            "issue",
            "view",
            work_item_id,
            "--repo",
            self.repository,
            "--json",
            "number,title,body,labels,url",
        )
        return WorkItem(
            id=str(raw["number"]),
            title=str(raw["title"]),
            body=str(raw.get("body") or ""),
            labels=frozenset(str(label["name"]) for label in raw.get("labels", [])),
            url=str(raw.get("url") or ""),
        )

    def get_change(self, change_id: str) -> ChangeRequest:
        raw = self.cli.run_json(
            "pr",
            "view",
            change_id,
            "--repo",
            self.repository,
            "--json",
            "number,url,state,isDraft,mergeable,mergeStateStatus,headRefOid,statusCheckRollup",
        )
        checks = tuple(
            CheckResult(
                name=str(check.get("name") or check.get("context") or "unnamed"),
                status=str(check.get("conclusion") or check.get("state") or "UNKNOWN"),
            )
            for check in raw.get("statusCheckRollup", [])
        )
        return ChangeRequest(
            id=str(raw["number"]),
            url=str(raw["url"]),
            state=str(raw["state"]),
            head_sha=str(raw["headRefOid"]),
            draft=bool(raw.get("isDraft", False)),
            mergeable=str(raw.get("mergeable")) == "MERGEABLE",
            merge_state=str(raw.get("mergeStateStatus") or "UNKNOWN"),
            checks=checks,
        )

    def merge_change(self, change_id: str, expected_head_sha: str) -> None:
        self.cli.run(
            "pr",
            "merge",
            change_id,
            "--repo",
            self.repository,
            "--squash",
            "--delete-branch",
            "--match-head-commit",
            expected_head_sha,
        )
