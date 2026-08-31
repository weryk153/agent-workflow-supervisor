"""Detached AO account-switch helper used by an active orchestrator conversation."""

from __future__ import annotations

import argparse
import json
import time

from agent_workflow_supervisor.adapters.command import AdapterCommandError, CommandAdapter
from agent_workflow_supervisor.locking import (
    clear_account_switch_pending,
    global_capacity_lock,
)
from agent_workflow_supervisor.project_accounts import bind_ao_project_account


def _session_state(ao: CommandAdapter, project_id: str, session_id: str) -> str:
    try:
        response = ao.run_json("session", "get", session_id, "--project", project_id, "--json")
    except AdapterCommandError:
        return "missing"
    session = response.get("session")
    if not isinstance(session, dict) or bool(session.get("isTerminated", False)):
        return "terminated"
    activity = session.get("activity")
    if isinstance(activity, dict) and activity.get("state"):
        return str(activity["state"])
    return str(session.get("status") or "unknown")


def wait_until_idle(
    ao: CommandAdapter,
    project_id: str,
    session_id: str,
    *,
    timeout_seconds: float = 300,
) -> str:
    """Wait for the current AO turn to finish before replacing its session."""

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        state = _session_state(ao, project_id, session_id)
        if state in {"missing", "terminated"}:
            return state
        if state == "idle":
            # Require a second idle observation to avoid racing the final AO
            # state transition immediately after the command tool returns.
            time.sleep(1)
            if _session_state(ao, project_id, session_id) == "idle":
                return "idle"
        time.sleep(0.5)
    raise TimeoutError(f"AO session {session_id!r} did not become idle before timeout")


def run_switch(
    project_id: str,
    account_name: str,
    source_session_id: str,
    *,
    ao_command: str,
    switch_id: str | None = None,
) -> dict[str, object]:
    ao = CommandAdapter(ao_command)
    try:
        settled_as = wait_until_idle(ao, project_id, source_session_id)
        result = bind_ao_project_account(
            project_id,
            account_name,
            ao_command=ao_command,
            restart_orchestrator=True,
            orchestrator_session_id=source_session_id,
            allow_missing_orchestrator=settled_as in {"missing", "terminated"},
            authorized_switch_id=switch_id,
            replacement_prompt=(
                f"The Claude account switch to registered profile {account_name!r} completed. "
                "Briefly tell the user that this is the replacement AO orchestrator session and "
                "ask them to resend any unfinished request because prior conversation context was "
                "not transferred."
            ),
        )
    finally:
        if switch_id is not None:
            with global_capacity_lock():
                clear_account_switch_pending(project_id, switch_id)
    return {
        "completed": True,
        "project_id": project_id,
        "account": account_name,
        "source_session": source_session_id,
        "source_settled_as": settled_as,
        "orchestrator_session": result["orchestrator_session"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--source-session", required=True)
    parser.add_argument("--ao-command", default="ao")
    parser.add_argument("--switch-id")
    arguments = parser.parse_args()
    result = run_switch(
        arguments.project,
        arguments.account,
        arguments.source_session,
        ao_command=arguments.ao_command,
        switch_id=arguments.switch_id,
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
