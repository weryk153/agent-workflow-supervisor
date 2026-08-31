from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_workflow_supervisor.config import load_config
from agent_workflow_supervisor.registry import (
    register_model_profile,
    register_project,
    set_project_model_profiles,
)


def test_load_config_resolves_database_relative_to_config(tmp_path: Path) -> None:
    config_path = tmp_path / "supervisor.toml"
    config_path.write_text(
        """
[supervisor]
database_path = ".state/test.sqlite"

[project]
id = "demo"

[tracker]
repository = "owner/repo"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.supervisor.database_path == tmp_path / ".state/test.sqlite"
    assert config.runner.type == "ao"
    assert config.policy.default_harness == "claude-code"
    assert config.policy.report_only_harnesses == set()


def test_load_config_accepts_isolated_credential_profiles(tmp_path: Path) -> None:
    config_path = tmp_path / "supervisor.toml"
    config_path.write_text(
        """
[project]
id = "demo"

[tracker]
repository = "owner/repo"

[credentials.profiles.claude-primary]
execution_project_id = "demo-claude-primary"

[credentials.profiles.claude-secondary]
execution_project_id = "demo-claude-secondary"

[policy.credential_profiles]
claude-code = ["claude-primary", "claude-secondary"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.policy.credential_profiles["claude-code"] == [
        "claude-primary",
        "claude-secondary",
    ]


def test_config_rejects_two_logins_pointing_at_same_ao_project(tmp_path: Path) -> None:
    config_path = tmp_path / "supervisor.toml"
    config_path.write_text(
        """
[project]
id = "demo"

[tracker]
repository = "owner/repo"

[credentials.profiles.one]
execution_project_id = "shared"

[credentials.profiles.two]
execution_project_id = "shared"

[policy.credential_profiles]
claude-code = ["one", "two"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="distinct execution_project_id"):
        load_config(config_path)


def test_load_config_resolves_project_models_from_global_registry(tmp_path: Path) -> None:
    config_path = tmp_path / "supervisor.toml"
    registry_path = tmp_path / "registry.toml"
    config_path.write_text(
        """
[project]
id = "demo"

[tracker]
repository = "owner/repo"

[[policy.routes]]
profile = "local-qwen"
labels_any = ["agent:local"]
""".strip(),
        encoding="utf-8",
    )
    register_model_profile(
        "local-qwen",
        harness="opencode",
        model="ollama/qwen3-coder",
        provider="ollama",
        path=registry_path,
    )
    register_project("demo", config_path=config_path, path=registry_path)
    set_project_model_profiles(
        "demo", ["local-qwen"], default_profile="local-qwen", path=registry_path
    )

    config = load_config(config_path, registry_path=registry_path)

    assert config.policy.default_model_profile == "local-qwen"
    assert config.policy.model_profiles["local-qwen"].harness == "opencode"
    assert config.policy.model_profiles["local-qwen"].model == "ollama/qwen3-coder"
