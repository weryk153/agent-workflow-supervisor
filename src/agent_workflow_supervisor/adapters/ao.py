"""Agent Orchestrator adapter implemented through the AO CLI."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from agent_workflow_supervisor.adapters.command import AdapterCommandError, CommandAdapter
from agent_workflow_supervisor.identifiers import canonical_github_issue_id
from agent_workflow_supervisor.models import AgentSession, ReviewResult, WorkItem


class AoRunner:
    def __init__(self, command: str = "ao", *, repository: str | None = None) -> None:
        self.cli = CommandAdapter(command)
        self.repository = repository

    def _session(self, raw: dict[str, object], project_id: str | None = None) -> AgentSession:
        raw_work_item_id = str(raw["issueId"]) if raw.get("issueId") is not None else None
        work_item_id = (
            canonical_github_issue_id(raw_work_item_id, self.repository)
            if raw_work_item_id is not None and self.repository is not None
            else raw_work_item_id
        )
        return AgentSession(
            id=str(raw["id"]),
            role=str(raw.get("role") or raw.get("kind") or "worker"),
            status=str(raw.get("status") or "unknown"),
            harness=str(raw.get("harness") or "unknown"),
            terminated=bool(raw.get("isTerminated", False)),
            work_item_id=work_item_id,
            project_id=str(raw.get("projectId") or project_id or "") or None,
            display_name=str(raw.get("displayName") or ""),
            created_at=str(raw.get("createdAt") or ""),
            updated_at=str(raw.get("updatedAt") or ""),
            last_activity_at=str(
                raw.get("lastActivityAt") or (raw.get("activity") or {}).get("lastActivityAt") or ""
            ),
        )

    def list_sessions(self, project_id: str) -> list[AgentSession]:
        response = self.cli.run_json(
            "session",
            "ls",
            "--project",
            project_id,
            "--all",
            "--include-terminated",
            "--json",
        )
        return [self._session(item, project_id) for item in response.get("data", [])]

    def list_active_sessions(self) -> list[AgentSession]:
        response = self.cli.run_json("session", "ls", "--all", "--json")
        return [self._session(item) for item in response.get("data", [])]

    def get_session(self, session_id: str) -> AgentSession | None:
        try:
            response = self.cli.run_json("session", "get", session_id, "--json")
        except AdapterCommandError:
            return None
        raw = response.get("session")
        return self._session(raw) if isinstance(raw, dict) else None

    def spawn_worker(
        self,
        *,
        project_id: str,
        work_item: WorkItem,
        harness: str,
        model: str | None,
        provider: str | None,
        credential_profile: str | None,
        prompt: str,
    ) -> AgentSession:
        safe_id = re.sub(r"[^A-Za-z0-9]+", "-", work_item.id).strip("-") or "work"
        args = [
            "spawn",
            "--project",
            project_id,
            "--kind",
            "worker",
            "--name",
            f"work-{safe_id}"[:20],
            "--harness",
            harness,
            "--issue",
            work_item.id,
            "--prompt",
            prompt,
        ]
        if model:
            args.extend(["--model", model])
        output = self.cli.run(*args)
        match = re.search(r"spawned session ([^\s]+)", output)
        if not match:
            raise AdapterCommandError("AO spawn output did not include a session id")
        session_id = match.group(1)
        return self.get_session(session_id) or AgentSession(
            id=session_id,
            role="worker",
            status="idle",
            harness=harness,
            work_item_id=work_item.id,
            project_id=project_id,
        )

    def get_review(self, session_id: str) -> ReviewResult | None:
        response = self.cli.run_json("review", "ls", session_id, "--json")
        reviews = response.get("reviews", [])
        if not reviews:
            return None
        raw = reviews[-1]
        latest = raw.get("latestRun") or {}
        verdict = str(latest.get("verdict") or "unknown")
        if verdict not in {"approved", "changes_requested", "pending", "unknown"}:
            verdict = "unknown"
        change_id = raw.get("prNumber")
        return ReviewResult(
            status=str(latest.get("status") or raw.get("status") or "unknown"),
            verdict=verdict,  # type: ignore[arg-type]
            change_id=str(change_id) if change_id is not None else None,
            change_url=str(raw.get("prUrl") or ""),
            target_sha=str(raw.get("targetSha") or ""),
            run_id=str(latest.get("id") or ""),
            started_at=str(latest.get("createdAt") or ""),
        )

    def trigger_review(self, session_id: str) -> None:
        self.cli.run("review", "trigger", session_id)

    def cancel_review(self, session_id: str) -> None:
        self.cli.run("review", "cancel", session_id)

    def send(self, session_id: str, message: str) -> bool:
        self.cli.run("send", "--session", session_id, "--message", message)
        return True

    def conversation_messages(self, session_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        status = self.cli.run_json("status", "--json")
        port = int(status.get("port") or 0)
        if port <= 0:
            raise AdapterCommandError("AO status did not include a valid daemon port")
        encoded_session = urllib.parse.quote(session_id, safe="")
        url = (
            f"http://127.0.0.1:{port}/api/v1/sessions/{encoded_session}/conversation?limit={limit}"
        )
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in {404, 409}:
                return []
            raise AdapterCommandError(
                f"AO conversation request failed for {session_id}: HTTP {error.code}"
            ) from error
        except (OSError, ValueError) as error:
            raise AdapterCommandError(
                f"AO conversation request failed for {session_id}: {error}"
            ) from error
        messages = payload.get("messages", []) if isinstance(payload, dict) else []
        return [message for message in messages if isinstance(message, dict)]

    def terminate(self, project_id: str, session_id: str) -> None:
        self.cli.run("session", "kill", session_id, "--project", project_id)
