from __future__ import annotations

from subprocess import CompletedProcess

import agent_workflow_supervisor.model_health as health_module
from agent_workflow_supervisor.model_health import diagnose_model_profile
from agent_workflow_supervisor.registry import ModelProfileRecord


class FakeCommandAdapter:
    def __init__(self, _command: str, timeout_seconds: int = 10) -> None:
        self.timeout_seconds = timeout_seconds

    def run_json(self, *args: str) -> dict:
        assert args == ("agent", "ls", "--json")
        return {
            "supported": [{"id": "opencode"}],
            "installed": [{"id": "opencode"}],
        }


def test_ollama_doctor_checks_harness_and_existing_model(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "CommandAdapter", FakeCommandAdapter)
    monkeypatch.setattr(health_module.shutil, "which", lambda name: f"/bin/{name}")

    def fake_run(args, **kwargs):
        assert args == ["/bin/ollama", "show", "qwen3-coder"] or args == [
            "/bin/opencode",
            "models",
            "ollama",
        ]
        stdout = "ollama/qwen3-coder\n" if args[1] == "models" else "model info"
        return CompletedProcess(args, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(health_module.subprocess, "run", fake_run)
    profile = ModelProfileRecord(
        "local-qwen",
        harness="opencode",
        model="ollama/qwen3-coder",
        provider="ollama",
    )

    result = diagnose_model_profile(profile)

    assert result["ready"]
    assert [check["check"] for check in result["checks"]] == [
        "ao_harness_supported",
        "ao_harness_installed",
        "ollama_cli",
        "ollama_model",
        "opencode_cli",
        "opencode_model",
    ]


def test_lmstudio_doctor_checks_served_and_opencode_models(monkeypatch) -> None:
    monkeypatch.setattr(health_module, "CommandAdapter", FakeCommandAdapter)
    monkeypatch.setattr(health_module.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(
        health_module,
        "_openai_compatible_models",
        lambda _url: ({"oa-local-qwen"}, None),
    )

    def fake_run(args, **kwargs):
        assert args == ["/bin/opencode", "models", "lmstudio"]
        return CompletedProcess(args, 0, stdout="lmstudio/oa-local-qwen\n", stderr="")

    monkeypatch.setattr(health_module.subprocess, "run", fake_run)
    profile = ModelProfileRecord(
        "local-qwen",
        harness="opencode",
        model="lmstudio/oa-local-qwen",
        provider="lmstudio",
    )

    result = diagnose_model_profile(profile)

    assert result["ready"]
    assert [check["check"] for check in result["checks"]] == [
        "ao_harness_supported",
        "ao_harness_installed",
        "lmstudio_server",
        "lmstudio_model",
        "opencode_cli",
        "opencode_model",
    ]


def test_process_doctor_uses_configured_harness_command(monkeypatch) -> None:
    monkeypatch.setattr(
        health_module.shutil,
        "which",
        lambda name: name if name == "/opt/agents/custom-opencode" else None,
    )

    def fake_run(args, **kwargs):
        assert args == [
            "/opt/agents/custom-opencode",
            "--profile",
            "local",
            "models",
            "custom",
        ]
        return CompletedProcess(args, 0, stdout="custom/coder\n", stderr="")

    monkeypatch.setattr(health_module.subprocess, "run", fake_run)
    profile = ModelProfileRecord(
        "custom-coder",
        harness="opencode",
        model="custom/coder",
        provider="custom",
    )

    result = diagnose_model_profile(
        profile,
        runner_type="process",
        process_commands={"opencode": "/opt/agents/custom-opencode --profile local"},
    )

    assert result["ready"]
    assert result["checks"] == [
        {
            "check": "process_harness",
            "ready": True,
            "detail": "/opt/agents/custom-opencode --profile local",
        },
        {
            "check": "opencode_cli",
            "ready": True,
            "detail": "/opt/agents/custom-opencode --profile local",
        },
        {
            "check": "opencode_model",
            "ready": True,
            "detail": "custom/coder is visible to OpenCode",
        },
    ]
