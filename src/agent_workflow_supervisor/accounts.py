"""One-time setup helpers for isolated Claude Code logins."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from tomlkit import dumps as toml_dumps

from agent_workflow_supervisor.registry import (
    REGISTRY_PATH,
    list_accounts,
    list_projects,
    register_account,
)

DATA_ROOT = (
    Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    / "agent-workflow-supervisor"
)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ValueError("account name must contain a letter or number")
    return slug


def _write_document(config_path: Path, document: Any) -> None:
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=config_path.parent,
        delete=False,
    ) as temporary:
        temporary.write(toml_dumps(document))
        temporary_path = Path(temporary.name)
    os.replace(temporary_path, config_path)


def _auth_status(config_dir: Path | None) -> dict[str, Any]:
    environment = os.environ.copy()
    if config_dir is not None:
        environment["CLAUDE_CONFIG_DIR"] = str(config_dir)
    status = subprocess.run(
        ["claude", "auth", "status", "--json"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    try:
        raw = json.loads(status.stdout)
    except json.JSONDecodeError:
        return {
            "logged_in": False,
            "auth_method": None,
            "subscription_type": None,
            "email": None,
            "organization": None,
        }
    return {
        "logged_in": bool(raw.get("loggedIn")),
        "auth_method": raw.get("authMethod"),
        "subscription_type": raw.get("subscriptionType"),
        "email": raw.get("email"),
        "organization": raw.get("orgName"),
    }


def add_claude_account(
    name: str,
    registry_path: Path = REGISTRY_PATH,
    *,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Authenticate one global Claude login without assigning it to a project."""

    slug = _slug(name)
    if slug == "default":
        raise ValueError("the default account is provided by the normal Claude login")
    credentials_dir = (
        config_dir.expanduser().resolve()
        if config_dir is not None
        else DATA_ROOT / "credentials" / f"claude-{slug}"
    )
    if config_dir is not None and not credentials_dir.is_dir():
        raise ValueError(f"Claude config directory does not exist: {credentials_dir}")
    if config_dir is None:
        credentials_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        credentials_dir.chmod(0o700)
    current = _auth_status(credentials_dir)
    if config_dir is not None and not current["logged_in"]:
        raise RuntimeError(f"Claude config directory is not logged in: {credentials_dir}")
    if not current["logged_in"]:
        environment = os.environ.copy()
        environment["CLAUDE_CONFIG_DIR"] = str(credentials_dir)
        login = subprocess.run(["claude", "auth", "login"], env=environment, check=False)
        if login.returncode != 0:
            raise RuntimeError("Claude login was cancelled or failed")
        current = _auth_status(credentials_dir)
    if not current["logged_in"]:
        raise RuntimeError("Claude login did not produce an authenticated profile")
    # A valid pre-existing credential directory can be adopted without forcing
    # the user through OAuth again merely because the registry is new.
    register_account(slug, config_dir=credentials_dir, path=registry_path)
    return {"account": slug, "claude_config_dir": str(credentials_dir), **current}


def list_claude_accounts(registry_path: Path = REGISTRY_PATH) -> list[dict[str, Any]]:
    projects = list_projects(registry_path)
    result = []
    for account in list_accounts(registry_path):
        assigned = [project.project_id for project in projects if account.name in project.accounts]
        result.append(
            {
                "account": account.name,
                "provider": account.provider,
                "assigned_projects": assigned,
                **_auth_status(account.config_dir),
            }
        )
    return result
