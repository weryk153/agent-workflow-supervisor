from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import agent_workflow_supervisor.accounts as account_module
from agent_workflow_supervisor.accounts import add_claude_account
from agent_workflow_supervisor.registry import (
    get_account,
    get_model_profile,
    get_project,
    list_accounts,
    list_model_profiles,
    list_projects,
    register_account,
    register_model_profile,
    register_project,
    set_project_model_profiles,
)


def test_registry_keeps_global_accounts_separate_from_project_assignment(
    tmp_path: Path,
) -> None:
    registry = tmp_path / "registry.toml"
    secondary_dir = tmp_path / "credentials" / "secondary"

    register_account("secondary", config_dir=secondary_dir, path=registry)
    register_project(
        "game",
        config_path=tmp_path / "game.toml",
        accounts=["default", "secondary"],
        path=registry,
    )
    register_project(
        "website",
        config_path=tmp_path / "website.toml",
        accounts=["secondary"],
        path=registry,
    )

    assert [account.name for account in list_accounts(registry)] == [
        "default",
        "secondary",
    ]
    assert get_account("secondary", registry).config_dir == secondary_dir
    assert get_project("game", registry).accounts == ("default", "secondary")
    assert get_project("website", registry).accounts == ("secondary",)


def test_registry_keeps_global_models_separate_from_project_assignment(tmp_path: Path) -> None:
    registry = tmp_path / "registry.toml"
    game_config = tmp_path / "game.toml"
    site_config = tmp_path / "site.toml"
    register_model_profile(
        "local-qwen",
        harness="opencode",
        model="ollama/qwen3-coder",
        provider="ollama",
        capacity=1,
        path=registry,
    )
    register_model_profile(
        "reviewer",
        harness="codex",
        model="gpt-reviewer",
        capacity=2,
        path=registry,
    )
    register_project("game", config_path=game_config, path=registry)
    register_project("website", config_path=site_config, path=registry)

    set_project_model_profiles(
        "game", ["reviewer", "local-qwen"], default_profile="reviewer", path=registry
    )
    set_project_model_profiles(
        "website", ["local-qwen"], default_profile="local-qwen", path=registry
    )

    assert [profile.name for profile in list_model_profiles(registry)] == [
        "local-qwen",
        "reviewer",
    ]
    assert get_model_profile("local-qwen", registry).provider == "ollama"
    assert get_project("game", registry).model_profiles == ("reviewer", "local-qwen")
    assert get_project("game", registry).default_model_profile == "reviewer"
    assert get_project("website", registry).model_profiles == ("local-qwen",)

    register_project("game", config_path=game_config, accounts=["default"], path=registry)
    assert get_project("game", registry).model_profiles == ("reviewer", "local-qwen")

    register_project(
        "game",
        config_path=game_config,
        model_profiles=(),
        default_model_profile=None,
        path=registry,
    )
    assert get_project("game", registry).model_profiles == ()
    assert get_project("game", registry).default_model_profile is None


def test_existing_authenticated_directory_is_adopted_without_login(
    tmp_path: Path, monkeypatch
) -> None:
    registry = tmp_path / "registry.toml"
    monkeypatch.setattr(account_module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(
        account_module,
        "_auth_status",
        lambda _path: {
            "logged_in": True,
            "auth_method": "claude.ai",
            "subscription_type": "team",
        },
    )

    result = add_claude_account("secondary", registry)

    assert result["logged_in"]
    assert get_account("secondary", registry).config_dir == (
        tmp_path / "data" / "credentials" / "claude-secondary"
    )


def test_explicit_authenticated_directory_is_adopted(tmp_path: Path, monkeypatch) -> None:
    registry = tmp_path / "registry.toml"
    work_dir = tmp_path / ".claude-work"
    work_dir.mkdir()
    monkeypatch.setattr(
        account_module,
        "_auth_status",
        lambda path: {
            "logged_in": path == work_dir,
            "auth_method": "claude.ai",
            "subscription_type": "team",
            "email": "developer@example.com",
            "organization": "Example",
        },
    )

    result = add_claude_account("work", registry, config_dir=work_dir)

    assert result["email"] == "developer@example.com"
    assert get_account("work", registry).config_dir == work_dir


def test_concurrent_registry_mutations_do_not_lose_updates(tmp_path: Path) -> None:
    registry = tmp_path / "registry.toml"

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(
            executor.map(
                lambda index: register_account(
                    f"account-{index}",
                    config_dir=tmp_path / f"account-{index}",
                    path=registry,
                ),
                range(12),
            )
        )
        list(
            executor.map(
                lambda index: register_model_profile(
                    f"model-{index}",
                    harness="opencode",
                    model=f"provider/model-{index}",
                    path=registry,
                ),
                range(12),
            )
        )
        list(
            executor.map(
                lambda index: register_project(
                    f"project-{index}",
                    config_path=tmp_path / f"project-{index}.toml",
                    path=registry,
                ),
                range(12),
            )
        )

    assert {account.name for account in list_accounts(registry)} >= {
        *(f"account-{index}" for index in range(12)),
        "default",
    }
    assert {profile.name for profile in list_model_profiles(registry)} == {
        f"model-{index}" for index in range(12)
    }
    assert {project.project_id for project in list_projects(registry)} == {
        f"project-{index}" for index in range(12)
    }
