"""Safe subprocess helper shared by CLI-backed adapters."""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


class AdapterCommandError(RuntimeError):
    pass


class CommandAdapter:
    def __init__(self, command: str, timeout_seconds: int = 60) -> None:
        if Path(command).exists():
            resolved = [command]
        else:
            resolved = shlex.split(command)
            if len(resolved) == 1 and shutil.which(resolved[0]) is None:
                bundled_ao = Path(
                    "/Applications/Agent Orchestrator.app/Contents/Resources/daemon/ao"
                )
                if sys.platform == "darwin" and resolved[0] == "ao" and bundled_ao.exists():
                    resolved = [str(bundled_ao)]
        self.command = resolved
        self.timeout_seconds = timeout_seconds

    def run(self, *args: str) -> str:
        try:
            completed = subprocess.run(
                [*self.command, *args],
                check=False,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
            )
        except OSError as error:
            raise AdapterCommandError(f"unable to run {self.command[0]}: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
            raise AdapterCommandError(
                f"{self.command[0]} exited with {completed.returncode}: {detail}"
            )
        return completed.stdout

    def run_json(self, *args: str) -> dict[str, Any]:
        output = self.run(*args)
        try:
            value = json.loads(output)
        except json.JSONDecodeError as error:
            raise AdapterCommandError(f"{self.command[0]} returned invalid JSON") from error
        if not isinstance(value, dict):
            raise AdapterCommandError(f"{self.command[0]} returned non-object JSON")
        return value
