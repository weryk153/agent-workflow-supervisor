"""Durable LangGraph workflow for agent-backed change delivery."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from operator import add
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent_workflow_supervisor.config import AppConfig
from agent_workflow_supervisor.identifiers import canonical_github_issue_id
from agent_workflow_supervisor.locking import (
    account_switch_pending,
    clear_acquisition_record,
    execution_credential_identity,
    list_acquisition_records,
    list_all_acquisition_records,
    read_acquisition_record,
    record_acquired_worker,
    record_pending_acquisition,
    work_item_acquisition_lock,
    worker_acquisition_locks,
)
from agent_workflow_supervisor.models import AgentSession, WorkItem
from agent_workflow_supervisor.policy import (
    ModelSelection,
    has_capacity,
    has_model_capacity,
    requires_approval,
    select_credential_profile,
    select_model,
)
from agent_workflow_supervisor.ports import RunnerPort, TrackerPort


def _utc_now() -> datetime:
    return datetime.now(UTC)


class SupervisorState(TypedDict, total=False):
    project_id: str
    work_item_id: str
    title: str
    body: str
    labels: list[str]
    work_item_url: str
    harness: str
    model: str
    model_profile: str
    model_capacity: int
    credential_profile: str
    execution_project_id: str
    worker_id: str
    worker_status: str
    review_status: str
    review_verdict: str
    review_target_sha: str
    review_triggered_for_sha: str
    review_worker_id: str
    review_run_id: str
    review_started_at: str
    review_attempts: int
    change_id: str
    change_url: str
    change_head_sha: str
    requires_approval: bool
    report_only: bool
    report_started: bool
    report_idle_observations: int
    approval: str
    approval_change_id: str
    approval_target_sha: str
    status: str
    last_error: str
    events: Annotated[list[str], add]


@dataclass(frozen=True)
class SupervisorDependencies:
    config: AppConfig
    runner: RunnerPort
    tracker: TrackerPort
    now: Callable[[], datetime] = _utc_now


def _item_from_state(state: SupervisorState) -> WorkItem:
    return WorkItem(
        id=state["work_item_id"],
        title=state.get("title", ""),
        body=state.get("body", ""),
        labels=frozenset(state.get("labels", [])),
        url=state.get("work_item_url", ""),
    )


def build_supervisor_graph(deps: SupervisorDependencies, checkpointer: Any = None) -> Any:
    """Compile a supervisor graph around interchangeable provider adapters."""

    config = deps.config
    runner = deps.runner
    tracker = deps.tracker

    def now_utc() -> datetime:
        value = deps.now()
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def iso_now() -> str:
        return now_utc().isoformat()

    def review_timed_out(started_at: str) -> bool:
        if not started_at:
            return False
        try:
            parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        age = (now_utc() - parsed.astimezone(UTC)).total_seconds()
        return age >= config.supervisor.review_timeout_seconds

    def execution_project_ids() -> list[str]:
        ids = [config.project.id]
        # Keep inactive/retired credential profiles visible until their AO
        # workers have finished. Project account changes retain these profile
        # records even after removing them from future routing.
        ids.extend(profile.execution_project_id for profile in config.credentials.profiles.values())
        return list(dict.fromkeys(ids))

    def all_sessions(extra_project_ids: list[str] | None = None) -> list[AgentSession]:
        sessions: list[AgentSession] = []
        project_ids = execution_project_ids()
        if extra_project_ids:
            project_ids.extend(extra_project_ids)
        for project_id in dict.fromkeys(project_ids):
            sessions.extend(runner.list_sessions(project_id))
        return sessions

    def profile_for_project(project_id: str | None, harness: str) -> str | None:
        for name in config.policy.credential_profiles.get(harness, []):
            if config.credentials.profiles[name].execution_project_id == project_id:
                return name
        return None

    def credential_resource_key(profile_name: str, harness: str) -> str:
        profile = config.credentials.profiles[profile_name]
        bound, bound_config_dir = execution_credential_identity(profile.execution_project_id)
        config_dir = bound_config_dir if bound else profile.claude_config_dir
        if config_dir is None:
            identity = "default"
        else:
            identity = str(Path(config_dir).expanduser().resolve())
        return f"{harness}:claude-config:{identity}"

    def global_resource_count(
        records: list[tuple[str, str, Any]],
        sessions: list[AgentSession],
        *,
        record_matches: Any,
        session_matches: Any,
    ) -> int:
        """Count reservations plus visible active workers without duplication."""

        matching_records = [entry for entry in records if record_matches(entry[2])]
        represented_ids = {
            record.worker_id for _, _, record in matching_records if record.worker_id is not None
        }
        for _, work_item_id, record in matching_records:
            if record.state != "pending":
                continue
            represented_ids.update(
                session.id
                for session in sessions
                if session.active
                and session.role == "worker"
                and session.work_item_id == work_item_id
                and session.harness == record.harness
                and session.project_id in {None, record.execution_project_id}
            )
        unreserved_visible = sum(
            1
            for session in sessions
            if session.active
            and session.role == "worker"
            and session.id not in represented_ids
            and session_matches(session)
        )
        return len(matching_records) + unreserved_visible

    def has_global_model_capacity(
        sessions: list[AgentSession],
        records: list[tuple[str, str, Any]],
        selection: ModelSelection,
    ) -> bool:
        if selection.capacity is None or selection.profile is None:
            return True
        matching_records = [
            record
            for _, _, record in records
            if record.model_profile == selection.profile
            or (record.model_profile is None and record.harness == selection.harness)
        ]
        effective_capacity = min(
            [selection.capacity]
            + [
                record.model_capacity
                for record in matching_records
                if record.model_capacity is not None
            ]
        )
        count = global_resource_count(
            records,
            sessions,
            record_matches=lambda record: (
                record.model_profile == selection.profile
                or (record.model_profile is None and record.harness == selection.harness)
            ),
            session_matches=lambda session: session.harness == selection.harness,
        )
        return count < effective_capacity

    def select_global_credential_profile(
        sessions: list[AgentSession],
        records: list[tuple[str, str, Any]],
        harness: str,
    ) -> tuple[str, Any, str] | None:
        names = config.policy.credential_profiles.get(harness, [])
        candidates: list[tuple[int, int, str, Any, str]] = []
        for order, name in enumerate(names):
            profile = config.credentials.profiles[name]
            resource_key = credential_resource_key(name, harness)
            execution_project_id = profile.execution_project_id

            def matches_credential(
                record: Any,
                key: str = resource_key,
                project_id: str = execution_project_id,
            ) -> bool:
                return record.credential_key == key or (
                    record.credential_key is None
                    and record.execution_project_id == project_id
                    and record.harness == harness
                )

            matching_records = [record for _, _, record in records if matches_credential(record)]
            effective_capacity = min(
                [profile.max_workers]
                + [
                    record.credential_capacity
                    for record in matching_records
                    if record.credential_capacity is not None
                ]
            )
            count = global_resource_count(
                records,
                sessions,
                record_matches=matches_credential,
                session_matches=lambda session, project_id=(profile.execution_project_id): (
                    session.harness == harness and session.project_id == project_id
                ),
            )
            if count < effective_capacity:
                candidates.append((count, order, name, profile, resource_key))
        if not candidates:
            return None
        _, _, name, profile, resource_key = min(candidates)
        return name, profile, resource_key

    def active_workers_for_item(
        sessions: list[AgentSession], work_item_id: str
    ) -> list[AgentSession]:
        canonical_id = canonical_github_issue_id(
            work_item_id, config.tracker.repository, strict=True
        )
        return [
            session
            for session in sessions
            if session.role == "worker"
            and session.work_item_id is not None
            and canonical_github_issue_id(session.work_item_id, config.tracker.repository)
            == canonical_id
            and session.active
        ]

    def sessions_with_capacity_reservations(
        sessions: list[AgentSession], current_work_item_id: str
    ) -> list[AgentSession]:
        """Count durable acquisitions that AO may not expose yet."""

        capacity_sessions = list(sessions)
        active_ids = {session.id for session in sessions if session.active}
        for work_item_id, reservation in list_acquisition_records(config.project.id):
            if work_item_id == current_work_item_id:
                continue
            if reservation.worker_id is not None and reservation.worker_id in active_ids:
                continue
            if reservation.state == "pending" and any(
                session.active
                and session.role == "worker"
                and session.work_item_id == work_item_id
                and session.harness == reservation.harness
                and session.project_id in {None, reservation.execution_project_id}
                for session in sessions
            ):
                continue
            capacity_sessions.append(
                AgentSession(
                    id=f"reservation:{work_item_id}",
                    role="worker",
                    status="reserved",
                    harness=reservation.harness,
                    work_item_id=work_item_id,
                    project_id=reservation.execution_project_id,
                )
            )
        return capacity_sessions

    def worker_conflict(
        workers: list[AgentSession], expected_harness: str
    ) -> dict[str, Any] | None:
        if not workers:
            return None
        matching = [worker for worker in workers if worker.harness == expected_harness]
        if len(workers) == 1 and matching:
            return None
        details = ", ".join(f"{worker.id} ({worker.harness})" for worker in workers)
        return {
            "status": "worker_route_conflict",
            "last_error": (
                f"active worker conflict for this work item: {details}; "
                f"current route requires {expected_harness}"
            ),
            "events": ["refused to create a duplicate worker for the work item"],
        }

    def guard_existing_state(state: SupervisorState) -> dict[str, Any]:
        return {"events": [f"resuming from {state.get('status', 'new')}"]}

    def load_work_item(state: SupervisorState) -> dict[str, Any]:
        requested_id = canonical_github_issue_id(
            state["work_item_id"], config.tracker.repository, strict=True
        )
        item = tracker.get_work_item(requested_id)
        resolved_id = canonical_github_issue_id(item.id, config.tracker.repository, strict=True)
        if resolved_id != requested_id:
            raise RuntimeError(
                f"tracker returned work item {resolved_id!r} for request {requested_id!r}"
            )
        return {
            "project_id": state.get("project_id", config.project.id),
            "work_item_id": resolved_id,
            "title": item.title,
            "body": item.body,
            "labels": sorted(item.labels),
            "work_item_url": item.url,
            "last_error": "",
            "status": "loaded",
            "events": [f"loaded work item {item.id}"],
        }

    def select_route(state: SupervisorState) -> dict[str, Any]:
        item = _item_from_state(state)
        selection = select_model(item, config.policy)
        if selection is None:
            return {"status": "skipped", "events": ["policy skipped work item"]}
        profile_note = f" via profile {selection.profile}" if selection.profile else ""
        return {
            "harness": selection.harness,
            "model": selection.model or "",
            "model_profile": selection.profile or "",
            "model_capacity": selection.capacity or 0,
            "requires_approval": requires_approval(item, config.policy),
            "report_only": selection.harness in config.policy.report_only_harnesses,
            "status": "routed",
            "events": [f"routed to {selection.harness}{profile_note}"],
        }

    def check_capacity(state: SupervisorState) -> dict[str, Any]:
        sessions = all_sessions()
        harness = state["harness"]
        selection = ModelSelection(
            state.get("model_profile") or None,
            harness,
            state.get("model") or None,
            state.get("model_capacity") or None,
        )
        active_workers = active_workers_for_item(sessions, state["work_item_id"])
        matching = active_workers[0] if active_workers else None
        if matching:
            return {
                "worker_id": matching.id,
                "worker_status": matching.status,
                "execution_project_id": matching.project_id or state["project_id"],
                "credential_profile": profile_for_project(matching.project_id, harness) or "",
                "status": "worker_candidate",
                "events": [f"found worker candidate {matching.id}"],
            }
        if not has_model_capacity(sessions, selection, config.policy):
            return {
                "status": "waiting_capacity",
                "events": [f"waiting for {state.get('model_profile') or harness} capacity"],
            }
        profile_names = config.policy.credential_profiles.get(harness, [])
        selected = select_credential_profile(sessions, harness, config)
        if profile_names and selected is None:
            return {
                "status": "waiting_profile_capacity",
                "events": [f"waiting for {harness} credential profile capacity"],
            }
        if selected is None:
            return {
                "execution_project_id": state["project_id"],
                "credential_profile": "",
                "status": "capacity_available",
                "events": ["capacity available"],
            }
        profile_name, profile = selected
        return {
            "execution_project_id": profile.execution_project_id,
            "credential_profile": profile_name,
            "status": "capacity_available",
            "events": [f"selected credential profile {profile_name}"],
        }

    def ensure_worker(state: SupervisorState) -> dict[str, Any]:
        # Capacity checks are advisory until acquisition. Serialize the final
        # session scan and spawn so concurrent service/manual ticks cannot both
        # observe an empty work item and create two workers.
        with worker_acquisition_locks(state["project_id"], state["work_item_id"]):
            reservation = read_acquisition_record(state["project_id"], state["work_item_id"])
            reservation_projects = [reservation.execution_project_id] if reservation else []
            sessions = all_sessions(reservation_projects)
            active_workers = active_workers_for_item(sessions, state["work_item_id"])
            if reservation is not None and reservation.state == "worker":
                assert reservation.worker_id is not None
                reserved_worker = next(
                    (worker for worker in active_workers if worker.id == reservation.worker_id),
                    None,
                )
                if reserved_worker is None:
                    reserved_worker = runner.get_session(reservation.worker_id)
                if reserved_worker is None:
                    return {
                        "status": "worker_reservation_unverified",
                        "last_error": (
                            f"AO could not verify reserved worker {reservation.worker_id}; "
                            "the reservation was retained to prevent a duplicate"
                        ),
                        "events": ["refused to discard an unverified worker reservation"],
                    }
                if not reserved_worker.active:
                    clear_acquisition_record(
                        state["project_id"],
                        state["work_item_id"],
                        worker_id=reservation.worker_id,
                    )
                    reservation = None
                elif reserved_worker.work_item_id not in {None, state["work_item_id"]}:
                    return {
                        "status": "worker_reservation_conflict",
                        "last_error": (
                            f"reserved worker {reserved_worker.id} now identifies work item "
                            f"{reserved_worker.work_item_id}"
                        ),
                        "events": ["refused to replace a conflicting worker reservation"],
                    }
                elif all(worker.id != reserved_worker.id for worker in active_workers):
                    active_workers.append(reserved_worker)

            if reservation is not None and reservation.state == "pending":
                pending_matches = [
                    worker
                    for worker in active_workers
                    if worker.project_id in {None, reservation.execution_project_id}
                    and worker.harness == reservation.harness
                ]
                if len(pending_matches) == 1:
                    recovered_worker = pending_matches[0]
                    execution_project_id = (
                        recovered_worker.project_id or reservation.execution_project_id
                    )
                    record_acquired_worker(
                        state["project_id"],
                        state["work_item_id"],
                        execution_project_id=execution_project_id,
                        harness=recovered_worker.harness,
                        worker_id=recovered_worker.id,
                        model_profile=reservation.model_profile,
                        model_capacity=reservation.model_capacity,
                        credential_key=reservation.credential_key,
                        credential_capacity=reservation.credential_capacity,
                    )
                    reservation = read_acquisition_record(
                        state["project_id"], state["work_item_id"]
                    )
            if conflict := worker_conflict(active_workers, state["harness"]):
                return conflict
            matching = active_workers[0] if active_workers else None
            if matching:
                execution_project_id = (
                    matching.project_id
                    or (reservation.execution_project_id if reservation else None)
                    or state.get("execution_project_id", state["project_id"])
                )
                matching_profile = profile_for_project(matching.project_id, state["harness"])
                matching_credential_key = (
                    credential_resource_key(matching_profile, state["harness"])
                    if matching_profile is not None
                    else None
                )
                record_acquired_worker(
                    state["project_id"],
                    state["work_item_id"],
                    execution_project_id=execution_project_id,
                    harness=matching.harness,
                    worker_id=matching.id,
                    model_profile=(
                        reservation.model_profile
                        if reservation and reservation.model_profile is not None
                        else state.get("model_profile") or None
                    ),
                    model_capacity=(
                        reservation.model_capacity
                        if reservation and reservation.model_capacity is not None
                        else state.get("model_capacity") or None
                    ),
                    credential_key=(
                        reservation.credential_key
                        if reservation and reservation.credential_key is not None
                        else matching_credential_key
                    ),
                    credential_capacity=(
                        reservation.credential_capacity
                        if reservation and reservation.credential_capacity is not None
                        else (
                            config.credentials.profiles[matching_profile].max_workers
                            if matching_profile is not None
                            else None
                        )
                    ),
                )
                return {
                    "worker_id": matching.id,
                    "worker_status": matching.status,
                    "execution_project_id": execution_project_id,
                    "credential_profile": matching_profile or state.get("credential_profile", ""),
                    "status": "worker_running",
                    "events": [f"reused worker {matching.id}"],
                }

            if reservation is not None and reservation.state == "pending":
                return {
                    "status": "worker_acquisition_pending",
                    "last_error": (
                        "a previous worker acquisition may still be completing; "
                        "manual resolution is required if no AO worker appears"
                    ),
                    "events": ["refused to spawn while an acquisition reservation is pending"],
                }

            if account_switch_pending(state["project_id"]):
                return {
                    "status": "waiting_account_switch",
                    "events": ["waiting for the scheduled account switch to finish"],
                }

            # The earlier graph node is only an advisory fast path. A lock
            # shared by every configured project closes the race between that
            # check and spawn, while durable reservations count uncertain or
            # temporarily invisible workers as occupied capacity.
            capacity_sessions = sessions_with_capacity_reservations(sessions, state["work_item_id"])
            selection = ModelSelection(
                state.get("model_profile") or None,
                state["harness"],
                state.get("model") or None,
                state.get("model_capacity") or None,
            )
            if not has_capacity(capacity_sessions, selection.harness, config.policy):
                return {
                    "status": "waiting_capacity",
                    "events": [
                        f"waiting for {state.get('model_profile') or state['harness']} capacity"
                    ],
                }
            global_records = list_all_acquisition_records()
            if not has_global_model_capacity(sessions, global_records, selection):
                return {
                    "status": "waiting_capacity",
                    "events": [f"waiting for {state['model_profile']} global capacity"],
                }
            profile_names = config.policy.credential_profiles.get(state["harness"], [])
            selected_profile = select_global_credential_profile(
                sessions, global_records, state["harness"]
            )
            if profile_names and selected_profile is None:
                return {
                    "status": "waiting_profile_capacity",
                    "events": [f"waiting for {state['harness']} credential profile capacity"],
                }
            if selected_profile is None:
                execution_project_id = state["project_id"]
                credential_profile = ""
                credential_key = None
                credential_capacity = None
            else:
                credential_profile, credential, credential_key = selected_profile
                execution_project_id = credential.execution_project_id
                credential_capacity = credential.max_workers

            if config.supervisor.shadow_mode:
                profile = credential_profile
                profile_note = f" with profile {profile}" if profile else ""
                model_note = (
                    f" using model profile {state['model_profile']}"
                    if state.get("model_profile")
                    else ""
                )
                return {
                    "execution_project_id": execution_project_id,
                    "credential_profile": credential_profile,
                    "status": "planned_worker",
                    "events": [
                        f"shadow: would spawn {state['harness']} worker{profile_note}{model_note}"
                    ],
                }

            item = _item_from_state(state)
            prompt = (
                f"Research work item {item.id}: {item.title}. Read its complete description "
                "and discussion. Do not modify repository files, implement code, create a PR, "
                "make art, or review a PR. Produce an evidence-backed report with source links "
                "and actionable recommendations. Post the final report to the work item's "
                "tracker discussion before finishing; if tracker posting is unavailable, leave "
                "the complete report in the session output."
                if state.get("report_only", False)
                else (
                    f"Handle work item {item.id}: {item.title}. Read its complete description "
                    "and discussion before acting. Follow the target project's own instructions."
                )
            )
            record_pending_acquisition(
                state["project_id"],
                state["work_item_id"],
                execution_project_id=execution_project_id,
                harness=state["harness"],
                model_profile=state.get("model_profile") or None,
                model_capacity=state.get("model_capacity") or None,
                credential_key=credential_key,
                credential_capacity=credential_capacity,
            )
            try:
                worker = runner.spawn_worker(
                    project_id=execution_project_id,
                    work_item=item,
                    harness=state["harness"],
                    model=state.get("model") or None,
                    credential_profile=credential_profile or None,
                    prompt=prompt,
                )
            except Exception:
                # A timeout or malformed adapter response may occur after AO
                # actually created the session. Keep the pending reservation
                # so a retry fails closed instead of risking a duplicate.
                raise
            record_acquired_worker(
                state["project_id"],
                state["work_item_id"],
                execution_project_id=worker.project_id or execution_project_id,
                harness=worker.harness,
                worker_id=worker.id,
                model_profile=state.get("model_profile") or None,
                model_capacity=state.get("model_capacity") or None,
                credential_key=credential_key,
                credential_capacity=credential_capacity,
            )
            return {
                "worker_id": worker.id,
                "worker_status": worker.status,
                "execution_project_id": worker.project_id or execution_project_id,
                "credential_profile": credential_profile,
                "report_started": False,
                "report_idle_observations": 0,
                "status": "worker_running",
                "events": [f"spawned worker {worker.id}"],
            }

    def inspect_worker(state: SupervisorState) -> dict[str, Any]:
        worker = runner.get_session(state["worker_id"])
        if worker is None or not worker.active:
            return {
                "status": "worker_unhealthy",
                "last_error": "worker session is missing or inactive",
                "events": ["worker health check failed"],
            }

        base: dict[str, Any] = {
            "worker_status": worker.status,
            "events": [f"inspected worker {worker.id}: {worker.status}"],
        }
        if state.get("report_only", False):
            if worker.status == "idle":
                idle_observations = state.get("report_idle_observations", 0) + 1
                if not state.get("report_started", False) and idle_observations < 2:
                    return {
                        **base,
                        "report_idle_observations": idle_observations,
                        "status": "worker_running",
                        "events": [
                            f"waiting for report-only worker {worker.id} to begin or remain idle"
                        ],
                    }
                return {
                    **base,
                    "report_idle_observations": idle_observations,
                    "status": "report_completed",
                    "events": [f"report-only worker {worker.id} completed"],
                }
            observed_activity = worker.status.casefold() not in {
                "idle",
                "pending",
                "queued",
                "starting",
                "unknown",
            }
            return {
                **base,
                "report_started": state.get("report_started", False) or observed_activity,
                "report_idle_observations": 0,
                "status": "worker_running",
            }

        review = runner.get_review(worker.id)
        if review is None:
            same_worker = state.get("review_worker_id") == worker.id
            started_at = state.get("review_started_at", "")
            attempts = state.get("review_attempts", 0)
            review_key = state.get("review_triggered_for_sha", "")
            # A newly opened PR can briefly exist before AO creates its first
            # review record. The worker status is the only durable signal in
            # that window; without adopting it, the workflow waits forever in
            # worker_running and never asks AO to start a review.
            if worker.status.casefold() == "pr_open" and not (
                same_worker and review_key and attempts
            ):
                missing_review_key = f"{worker.id}:unknown"
                if not config.supervisor.shadow_mode:
                    runner.trigger_review(worker.id)
                action = "shadow: would trigger" if config.supervisor.shadow_mode else "triggered"
                return {
                    **base,
                    "review_worker_id": worker.id,
                    "review_run_id": "",
                    "review_triggered_for_sha": missing_review_key,
                    "review_started_at": iso_now(),
                    "review_attempts": 1,
                    "status": "review_pending",
                    "events": [f"{action} initial review for PR-open worker {worker.id}"],
                }
            if same_worker and review_key and attempts and review_timed_out(started_at):
                if config.supervisor.shadow_mode:
                    return {
                        **base,
                        "review_started_at": iso_now(),
                        "status": "review_pending",
                        "events": [f"shadow: would restart missing review for {worker.id}"],
                    }
                if attempts >= config.supervisor.review_max_attempts:
                    return {
                        **base,
                        "status": "review_stalled",
                        "last_error": (
                            f"review for {worker.id} at {review_key} disappeared or did not "
                            f"complete after {attempts} attempt(s)"
                        ),
                        "events": ["review watchdog exhausted its retry budget"],
                    }
                runner.trigger_review(worker.id)
                return {
                    **base,
                    "review_worker_id": worker.id,
                    "review_run_id": "",
                    "review_started_at": iso_now(),
                    "review_attempts": attempts + 1,
                    "status": "review_pending",
                    "events": [
                        f"restarted missing review for {worker.id} (attempt {attempts + 1})"
                    ],
                }
            if same_worker and review_key and attempts:
                return {**base, "status": "review_pending"}
            return {**base, "status": "worker_running"}

        base.update(
            review_status=review.status,
            review_verdict=review.verdict,
            review_target_sha=review.target_sha,
        )
        if review.verdict == "changes_requested":
            return {
                **base,
                "review_worker_id": worker.id,
                "review_run_id": "",
                "review_triggered_for_sha": "",
                "review_started_at": "",
                "review_attempts": 0,
                "status": "changes_requested",
            }

        if review.verdict == "approved" and review.change_id and not review.target_sha:
            return {
                **base,
                "change_id": review.change_id,
                "change_url": review.change_url,
                "status": "review_invalid",
                "last_error": "approved review did not identify its target head SHA",
                "events": ["rejected approval without a target head SHA"],
            }

        if review.verdict == "approved" and review.change_id:
            return {
                **base,
                "change_id": review.change_id,
                "change_url": review.change_url,
                "status": "review_approved",
            }

        review_status = review.status.casefold()
        review_key = review.target_sha or f"{worker.id}:unknown"
        same_review = (
            state.get("review_worker_id") == worker.id
            and state.get("review_triggered_for_sha") == review_key
        )
        attempts = state.get("review_attempts", 0) if same_review else 0
        started_at = state.get("review_started_at", "") if same_review else ""
        run_id = state.get("review_run_id", "") if same_review else ""

        # A run created outside the supervisor after a stalled/known run is an
        # explicit recovery action. Give that new run a fresh bounded budget.
        if same_review and run_id and review.run_id and run_id != review.run_id:
            attempts = 1
            started_at = review.started_at or iso_now()
            run_id = review.run_id

        failure_statuses = {"cancelled", "canceled", "error", "failed", "terminated"}
        if review_status in failure_statuses:
            attempts = max(attempts, 1)
            if config.supervisor.shadow_mode:
                return {
                    **base,
                    "review_worker_id": worker.id,
                    "review_run_id": review.run_id,
                    "review_triggered_for_sha": review_key,
                    "review_started_at": review.started_at or iso_now(),
                    "review_attempts": attempts,
                    "status": "review_pending",
                    "events": [f"shadow: would restart failed review for {worker.id}"],
                }
            if attempts >= config.supervisor.review_max_attempts:
                return {
                    **base,
                    "review_worker_id": worker.id,
                    "review_run_id": review.run_id,
                    "review_triggered_for_sha": review_key,
                    "review_started_at": started_at or review.started_at or iso_now(),
                    "review_attempts": attempts,
                    "status": "review_stalled",
                    "last_error": (
                        f"review for {worker.id} at {review_key} ended as {review_status} "
                        f"after {attempts} attempt(s)"
                    ),
                    "events": ["review watchdog exhausted its retry budget"],
                }
            runner.trigger_review(worker.id)
            return {
                **base,
                "review_worker_id": worker.id,
                "review_run_id": "",
                "review_triggered_for_sha": review_key,
                "review_started_at": iso_now(),
                "review_attempts": attempts + 1,
                "status": "review_pending",
                "events": [f"restarted failed review for {worker.id} (attempt {attempts + 1})"],
            }

        if review_status == "needs_review" and attempts == 0:
            if not config.supervisor.shadow_mode:
                runner.trigger_review(worker.id)
            return {
                **base,
                "review_worker_id": worker.id,
                "review_run_id": "",
                "review_triggered_for_sha": review_key,
                "review_started_at": iso_now(),
                "review_attempts": 1,
                "status": "review_pending",
                "events": [
                    f"{'shadow: would trigger' if config.supervisor.shadow_mode else 'triggered'} "
                    f"review for {worker.id} (attempt 1)"
                ],
            }

        if attempts == 0:
            # AO may already have started the review before the supervisor saw
            # it. Adopt that run so it receives the same timeout protection.
            attempts = 1
            started_at = review.started_at or iso_now()
            run_id = review.run_id

        if not config.supervisor.shadow_mode and review_timed_out(started_at):
            if attempts >= config.supervisor.review_max_attempts:
                return {
                    **base,
                    "review_worker_id": worker.id,
                    "review_run_id": run_id or review.run_id,
                    "review_triggered_for_sha": review_key,
                    "review_started_at": started_at,
                    "review_attempts": attempts,
                    "status": "review_stalled",
                    "last_error": (
                        f"review for {worker.id} at {review_key} timed out after "
                        f"{attempts} attempt(s)"
                    ),
                    "events": ["review watchdog exhausted its retry budget"],
                }
            if review_status != "needs_review":
                runner.cancel_review(worker.id)
            runner.trigger_review(worker.id)
            return {
                **base,
                "review_worker_id": worker.id,
                "review_run_id": "",
                "review_triggered_for_sha": review_key,
                "review_started_at": iso_now(),
                "review_attempts": attempts + 1,
                "status": "review_pending",
                "events": [f"restarted timed-out review for {worker.id} (attempt {attempts + 1})"],
            }

        return {
            **base,
            "review_worker_id": worker.id,
            "review_run_id": run_id or review.run_id,
            "review_triggered_for_sha": review_key,
            "review_started_at": started_at,
            "review_attempts": attempts,
            "status": "review_pending",
        }

    def verify_change(state: SupervisorState) -> dict[str, Any]:
        expected_sha = state.get("review_target_sha", "")
        if not expected_sha:
            return {
                "status": "review_invalid",
                "last_error": "approved review did not identify its target head SHA",
                "events": ["rejected approval without a target head SHA"],
            }
        change = tracker.get_change(state["change_id"])
        if change.head_sha != expected_sha:
            return {
                "status": "review_stale",
                "change_head_sha": change.head_sha,
                "last_error": "review target no longer matches change head",
                "events": ["review is stale for current change head"],
            }
        if not change.ready:
            return {
                "status": "waiting_change_gate",
                "change_head_sha": change.head_sha,
                "events": ["change is not ready to merge"],
            }
        approval_matches = (
            state.get("approval") == "approved"
            and state.get("approval_change_id") == change.id
            and state.get("approval_target_sha") == change.head_sha
        )
        return {
            "change_head_sha": change.head_sha,
            "change_url": change.url,
            "status": "awaiting_approval"
            if state.get("requires_approval", False) and not approval_matches
            else "ready_to_merge",
            "events": ["change gates passed"],
        }

    def approval_gate(state: SupervisorState) -> dict[str, Any]:
        response = interrupt(
            {
                "kind": "change_approval",
                "work_item_id": state["work_item_id"],
                "change_id": state["change_id"],
                "change_url": state.get("change_url", ""),
                "change_head_sha": state.get("change_head_sha", ""),
                "message": "Approve this exact change and head for merge?",
            }
        )
        action = response.get("action") if isinstance(response, dict) else str(response)
        if action == "approve":
            return {
                "approval": "approved",
                "approval_change_id": state["change_id"],
                "approval_target_sha": state["change_head_sha"],
                "status": "approval_recorded",
                "events": ["human approved change for merge"],
            }
        return {
            "approval": "rejected",
            "status": "approval_rejected",
            "events": ["human rejected protected change"],
        }

    def merge_change(state: SupervisorState) -> dict[str, Any]:
        if config.supervisor.shadow_mode:
            return {
                "status": "planned_merge",
                "events": [f"shadow: would merge change {state['change_id']}"],
            }
        tracker.merge_change(state["change_id"], state["change_head_sha"])
        return {
            "status": "merged",
            "events": [f"merged change {state['change_id']}"],
        }

    def cleanup_worker(state: SupervisorState) -> dict[str, Any]:
        with work_item_acquisition_lock(state["project_id"], state["work_item_id"]):
            if not config.supervisor.shadow_mode:
                runner.terminate(
                    state.get("execution_project_id", state["project_id"]),
                    state["worker_id"],
                )
            clear_acquisition_record(
                state["project_id"],
                state["work_item_id"],
                worker_id=state["worker_id"],
            )
        return {
            "status": "completed",
            "events": [f"cleaned worker {state['worker_id']}"],
        }

    graph = StateGraph(SupervisorState)
    graph.add_node("guard_existing_state", guard_existing_state)
    graph.add_node("load_work_item", load_work_item)
    graph.add_node("select_route", select_route)
    graph.add_node("check_capacity", check_capacity)
    graph.add_node("ensure_worker", ensure_worker)
    graph.add_node("inspect_worker", inspect_worker)
    graph.add_node("verify_change", verify_change)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("merge_change", merge_change)
    graph.add_node("cleanup_worker", cleanup_worker)

    graph.add_edge(START, "guard_existing_state")
    graph.add_conditional_edges(
        "guard_existing_state",
        lambda state: (
            "cleanup"
            if state.get("status") == "merged"
            else "stop"
            if state.get("status") in {"completed", "approval_rejected"}
            else "continue"
        ),
        {"cleanup": "cleanup_worker", "stop": END, "continue": "load_work_item"},
    )
    graph.add_edge("load_work_item", "select_route")
    graph.add_conditional_edges(
        "select_route",
        lambda state: "stop" if state["status"] == "skipped" else "continue",
        {"stop": END, "continue": "check_capacity"},
    )
    graph.add_conditional_edges(
        "check_capacity",
        lambda state: (
            "acquire" if state["status"] in {"worker_candidate", "capacity_available"} else "stop"
        ),
        {"stop": END, "acquire": "ensure_worker"},
    )
    graph.add_conditional_edges(
        "ensure_worker",
        lambda state: "continue" if state["status"] == "worker_running" else "stop",
        {"stop": END, "continue": "inspect_worker"},
    )
    graph.add_conditional_edges(
        "inspect_worker",
        lambda state: (
            "cleanup"
            if state["status"] == "report_completed"
            else "verify"
            if state["status"] == "review_approved"
            else "stop"
        ),
        {"stop": END, "verify": "verify_change", "cleanup": "cleanup_worker"},
    )
    graph.add_conditional_edges(
        "verify_change",
        lambda state: (
            "approve"
            if state["status"] == "awaiting_approval"
            else "merge"
            if state["status"] == "ready_to_merge"
            else "stop"
        ),
        {"stop": END, "approve": "approval_gate", "merge": "merge_change"},
    )
    graph.add_conditional_edges(
        "approval_gate",
        lambda state: "verify" if state["status"] == "approval_recorded" else "stop",
        {"stop": END, "verify": "verify_change"},
    )
    graph.add_conditional_edges(
        "merge_change",
        lambda state: "cleanup" if state["status"] == "merged" else "stop",
        {"stop": END, "cleanup": "cleanup_worker"},
    )
    graph.add_edge("cleanup_worker", END)
    return graph.compile(checkpointer=checkpointer)
