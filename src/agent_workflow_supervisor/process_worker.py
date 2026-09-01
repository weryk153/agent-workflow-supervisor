"""Detached task entry point for the AO-independent process runner."""

from __future__ import annotations

import argparse
from pathlib import Path

from agent_workflow_supervisor.adapters.process import run_process_task


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", required=True, type=Path)
    arguments = parser.parse_args()
    run_process_task(arguments.task.expanduser().resolve())


if __name__ == "__main__":
    main()
