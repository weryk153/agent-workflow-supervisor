"""Read-only readiness checks for reusable model profiles."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Any

from agent_workflow_supervisor.adapters.command import AdapterCommandError, CommandAdapter
from agent_workflow_supervisor.registry import ModelProfileRecord


def _check(name: str, ready: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ready": ready, "detail": detail}


def _ollama_model_name(model: str) -> str:
    return model.removeprefix("ollama/")


def _provider_model_name(model: str, provider: str) -> str:
    return model.removeprefix(f"{provider}/")


def _openai_compatible_models(url: str) -> tuple[set[str], str | None]:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            raw = json.load(response)
    except (OSError, ValueError, urllib.error.URLError) as error:
        return set(), str(error)
    return {
        str(item.get("id"))
        for item in raw.get("data", [])
        if isinstance(item, dict) and item.get("id")
    }, None


def diagnose_model_profile(
    profile: ModelProfileRecord, *, ao_command: str = "ao"
) -> dict[str, Any]:
    """Inspect local prerequisites without downloading models or changing configuration."""

    checks: list[dict[str, Any]] = []
    try:
        catalog = CommandAdapter(ao_command, timeout_seconds=10).run_json("agent", "ls", "--json")
    except AdapterCommandError as error:
        checks.append(_check("ao", False, str(error)))
    else:
        supported = {str(item.get("id")) for item in catalog.get("supported", [])}
        installed = {str(item.get("id")) for item in catalog.get("installed", [])}
        checks.append(
            _check(
                "ao_harness_supported",
                profile.harness in supported,
                f"AO {'supports' if profile.harness in supported else 'does not support'} "
                f"{profile.harness}",
            )
        )
        checks.append(
            _check(
                "ao_harness_installed",
                profile.harness in installed,
                f"{profile.harness} {'is' if profile.harness in installed else 'is not'} installed",
            )
        )

    if profile.provider == "ollama":
        ollama = shutil.which("ollama")
        checks.append(
            _check(
                "ollama_cli",
                ollama is not None,
                ollama or "ollama was not found on PATH",
            )
        )
        if ollama is not None:
            model_name = _ollama_model_name(profile.model)
            try:
                result = subprocess.run(
                    [ollama, "show", model_name],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                checks.append(_check("ollama_model", False, str(error)))
            else:
                detail = (
                    f"{model_name} is available"
                    if result.returncode == 0
                    else (result.stderr.strip() or result.stdout.strip() or "model is unavailable")
                )
                checks.append(_check("ollama_model", result.returncode == 0, detail))

    if profile.provider == "lmstudio":
        models, error = _openai_compatible_models("http://127.0.0.1:1234/v1/models")
        checks.append(
            _check(
                "lmstudio_server",
                error is None,
                "LM Studio API is available on 127.0.0.1:1234" if error is None else error,
            )
        )
        if error is None:
            model_name = _provider_model_name(profile.model, "lmstudio")
            availability = "is" if model_name in models else "is not"
            checks.append(
                _check(
                    "lmstudio_model",
                    model_name in models,
                    f"{model_name} {availability} served by LM Studio",
                )
            )

    if profile.harness == "opencode":
        opencode = shutil.which("opencode")
        checks.append(
            _check(
                "opencode_cli",
                opencode is not None,
                opencode or "opencode was not found on PATH",
            )
        )
        if opencode is not None:
            # Filtering by provider keeps the output small. OpenCode 1.18.x can
            # truncate the unfiltered model catalog when stdout is captured,
            # which would make a configured model appear to be missing.
            provider = profile.provider or profile.model.partition("/")[0]
            command = [opencode, "models"]
            if provider:
                command.append(provider)
            try:
                result = subprocess.run(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                checks.append(_check("opencode_model", False, str(error)))
            else:
                available = {line.strip() for line in result.stdout.splitlines() if line.strip()}
                visible = result.returncode == 0 and profile.model in available
                checks.append(
                    _check(
                        "opencode_model",
                        visible,
                        f"{profile.model} "
                        f"{'is visible to OpenCode' if visible else 'is not visible to OpenCode'}",
                    )
                )

    return {
        "profile": profile.name,
        "harness": profile.harness,
        "model": profile.model,
        "provider": profile.provider,
        "ready": bool(checks) and all(check["ready"] for check in checks),
        "checks": checks,
    }
