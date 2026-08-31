from pathlib import Path

import pytest

import agent_workflow_supervisor.account_switch as switch_module
import agent_workflow_supervisor.locking as locking_module


@pytest.fixture(autouse=True)
def isolated_lock_root(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(locking_module, "LOCK_ROOT", tmp_path / "locks")


def test_wait_until_idle_requires_two_idle_observations(monkeypatch) -> None:
    states = iter(["active", "idle", "idle"])
    monkeypatch.setattr(switch_module, "_session_state", lambda *_args: next(states))
    monkeypatch.setattr(switch_module.time, "sleep", lambda _seconds: None)

    assert switch_module.wait_until_idle(object(), "demo", "demo-1") == "idle"  # type: ignore[arg-type]


def test_run_switch_spawns_replacement_with_handoff_prompt(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(switch_module, "CommandAdapter", lambda _command: object())
    monkeypatch.setattr(
        switch_module,
        "wait_until_idle",
        lambda *_args: "idle",
    )

    def fake_bind(project_id: str, account_name: str, **kwargs):
        captured.update(
            project_id=project_id,
            account_name=account_name,
            **kwargs,
        )
        return {"orchestrator_session": "demo-2"}

    monkeypatch.setattr(switch_module, "bind_ao_project_account", fake_bind)

    result = switch_module.run_switch(
        "demo",
        "work",
        "demo-1",
        ao_command="ao",
    )

    assert result["orchestrator_session"] == "demo-2"
    assert captured["restart_orchestrator"] is True
    assert captured["orchestrator_session_id"] == "demo-1"
    assert "prior conversation context was not transferred" in captured["replacement_prompt"]


def test_missing_source_still_spawns_replacement_and_clears_switch_barrier(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(switch_module, "CommandAdapter", lambda _command: object())
    monkeypatch.setattr(switch_module, "wait_until_idle", lambda *_args: "missing")

    def fake_bind(project_id: str, account_name: str, **kwargs):
        captured.update(project_id=project_id, account_name=account_name, **kwargs)
        return {"orchestrator_session": "demo-recovered"}

    monkeypatch.setattr(switch_module, "bind_ao_project_account", fake_bind)
    locking_module.mark_account_switch_pending("demo", "switch-1")

    result = switch_module.run_switch(
        "demo",
        "work",
        "vanished",
        ao_command="ao",
        switch_id="switch-1",
    )

    assert result["orchestrator_session"] == "demo-recovered"
    assert captured["allow_missing_orchestrator"] is True
    assert not locking_module.account_switch_pending("demo")


def test_wait_timeout_clears_switch_barrier(monkeypatch) -> None:
    monkeypatch.setattr(switch_module, "CommandAdapter", lambda _command: object())

    def fail_wait(*_args):
        raise TimeoutError("idle timeout")

    monkeypatch.setattr(switch_module, "wait_until_idle", fail_wait)
    locking_module.mark_account_switch_pending("demo", "switch-timeout")

    with pytest.raises(TimeoutError, match="idle timeout"):
        switch_module.run_switch(
            "demo",
            "work",
            "demo-1",
            ao_command="ao",
            switch_id="switch-timeout",
        )

    assert not locking_module.account_switch_pending("demo")


def test_dead_switch_owner_is_reclaimed(monkeypatch) -> None:
    locking_module.mark_account_switch_pending("demo", "switch-dead")
    monkeypatch.setattr(locking_module, "_process_alive", lambda _pid: False)

    assert not locking_module.account_switch_pending("demo")
    assert locking_module.account_switch_id("demo") is None
