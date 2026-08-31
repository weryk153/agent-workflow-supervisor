"""Shared graph runtime construction."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

from agent_workflow_supervisor.adapters import AoRunner, GitHubTracker
from agent_workflow_supervisor.config import AppConfig
from agent_workflow_supervisor.graph import SupervisorDependencies, build_supervisor_graph
from agent_workflow_supervisor.identifiers import canonical_github_issue_id


@contextmanager
def graph_runtime(config: AppConfig) -> Iterator[Any]:
    database_path = config.supervisor.database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    runner = AoRunner(config.runner.command, repository=config.tracker.repository)
    tracker = GitHubTracker(config.tracker.repository, config.tracker.command)
    dependencies = SupervisorDependencies(config=config, runner=runner, tracker=tracker)
    with SqliteSaver.from_conn_string(str(database_path)) as checkpointer:
        yield build_supervisor_graph(dependencies, checkpointer=checkpointer)


def workflow_thread_id(config: AppConfig, work_item_id: str) -> str:
    canonical_id = canonical_github_issue_id(
        work_item_id,
        config.tracker.repository,
        strict=True,
    )
    return f"{config.project.id}:{canonical_id}"
