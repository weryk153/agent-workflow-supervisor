from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_workflow_supervisor.adapters import process as process_adapter
from agent_workflow_supervisor.adapters.process import (
    ProcessRunner,
    ProcessRunnerError,
    _driver_argv,
    _owned_task_processes,
    _process_alive,
    _process_group_alive,
    _process_table,
    _verify_task_checkout,
)
from agent_workflow_supervisor.config import CredentialProfileConfig, RunnerConfig
from agent_workflow_supervisor.models import WorkItem


def _run(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path]:
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    repository.mkdir()
    _run("git", "init", "-b", "main", cwd=repository)
    _run("git", "config", "user.name", "Test", cwd=repository)
    _run("git", "config", "user.email", "test@example.com", cwd=repository)
    (repository / "README.md").write_text("fixture\n", encoding="utf-8")
    _run("git", "add", "README.md", cwd=repository)
    _run("git", "commit", "-m", "initial", cwd=repository)
    _run("git", "init", "--bare", str(remote))
    _run("git", "remote", "add", "origin", str(remote), cwd=repository)
    _run("git", "push", "-u", "origin", "main", cwd=repository)
    _run("git", "symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
    _run("git", "remote", "set-head", "origin", "main", cwd=repository)
    return repository, remote


def _fake_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake_cli.py"
    script.write_text(
        """
import json
import os
import signal
import sys
import time

args = sys.argv[1:]
if args[:2] == ["repo", "view"]:
    print(json.dumps({"defaultBranchRef": {"name": "main"}}))
elif args[:2] == ["pr", "list"]:
    path = os.environ["OA_FAKE_PRS"]
    print(open(path, encoding="utf-8").read())
elif args[:2] == ["pr", "review"]:
    with open(os.environ["OA_FAKE_REVIEWS"], "a", encoding="utf-8") as output:
        output.write(json.dumps(args) + "\\n")
    time.sleep(float(os.environ.get("OA_FAKE_REVIEW_SLEEP_AFTER_POST", "0")))
else:
    prompt = sys.stdin.read()
    with open(os.environ["OA_FAKE_AGENT_ENV"], "a", encoding="utf-8") as output:
        output.write((os.environ.get("CLAUDE_CONFIG_DIR") or "<default>") + "\\n")
    if started := os.environ.get("OA_FAKE_AGENT_STARTED"):
        open(started, "w", encoding="utf-8").write("started\\n")
    if os.environ.get("OA_FAKE_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(float(os.environ.get("OA_FAKE_AGENT_SLEEP", "0")))
    if "Review pull request" in prompt:
        result = json.dumps({
            "verdict": os.environ.get("OA_FAKE_VERDICT", "approved"),
            "feedback": "review fixture",
        })
    else:
        result = "worker complete"
    print(json.dumps({"session_id": "provider-session-1", "result": result}))
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return script


def _wait_for(get_value, expected: set[str], timeout: float = 30) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = get_value()
        if value in expected:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {expected}; last value was {value}")


def _runner_with_fake_cli(monkeypatch, tmp_path: Path) -> tuple[ProcessRunner, Path, Path, Path]:
    repository, _ = _repository(tmp_path)
    fake_cli = _fake_cli(tmp_path)
    prs = tmp_path / "prs.json"
    reviews = tmp_path / "reviews.jsonl"
    agent_environment = tmp_path / "agent-environment.txt"
    prs.write_text("[]\n", encoding="utf-8")
    reviews.write_text("", encoding="utf-8")
    agent_environment.write_text("", encoding="utf-8")
    monkeypatch.setenv("OA_FAKE_PRS", str(prs))
    monkeypatch.setenv("OA_FAKE_REVIEWS", str(reviews))
    monkeypatch.setenv("OA_FAKE_AGENT_ENV", str(agent_environment))
    command = f"{sys.executable} {fake_cli}"
    runner = ProcessRunner(
        RunnerConfig(
            type="process",
            repository_path=repository,
            worktree_root=tmp_path / "worktrees",
            verify_repository_remote=False,
            review_harness="claude-code",
            commands={
                "claude-code": command,
                "codex": command,
                "opencode": command,
            },
        ),
        repository="owner/repository",
        tracker_command=command,
        runtime_dir=tmp_path / "runtime",
        credentials={},
        report_only_harnesses=set(),
    )
    return runner, prs, reviews, agent_environment


def _spawn_finished_worker(runner: ProcessRunner) -> tuple[str, Path, str]:
    session = runner.spawn_worker(
        project_id="demo",
        work_item=WorkItem("42", "crash recovery fixture"),
        harness="claude-code",
        model=None,
        provider=None,
        credential_profile=None,
        prompt="Handle the recovery fixture.",
    )
    _wait_for(
        lambda: str(runner.state.get_session(session.id)["status"]),  # type: ignore[index]
        {"finished"},
    )
    row = runner.state.get_session(session.id)
    assert row is not None
    worktree = Path(str(row["worktree_path"]))
    return session.id, worktree, _run("git", "rev-parse", "HEAD", cwd=worktree)


def _publish_fake_pr(prs: Path, head_sha: str) -> None:
    prs.write_text(
        json.dumps(
            [
                {
                    "number": 12,
                    "url": "https://github.com/owner/repository/pull/12",
                    "state": "OPEN",
                    "headRefOid": head_sha,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _publish_closed_fake_pr(prs: Path, head_sha: str) -> None:
    prs.write_text(
        json.dumps(
            [
                {
                    "number": 12,
                    "url": "https://github.com/owner/repository/pull/12",
                    "state": "CLOSED",
                    "headRefOid": head_sha,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_codex_driver_selects_local_provider_without_shell() -> None:
    argv = _driver_argv(
        {
            "harness": "codex",
            "command": "codex",
            "model": "lmstudio/qwen2.5-coder-7b-instruct-mlx",
            "provider": "lmstudio",
            "worktree": "/tmp/worktree with spaces",
            "provider_session_id": None,
            "codex_sandbox": "workspace-write",
            "codex_approve_for_me": True,
        }
    )

    assert argv == [
        "codex",
        "exec",
        "--json",
        "--color",
        "never",
        "--cd",
        "/tmp/worktree with spaces",
        "--oss",
        "--local-provider",
        "lmstudio",
        "--model",
        "qwen2.5-coder-7b-instruct-mlx",
        "--approve-for-me",
        "-",
    ]


def test_review_checkout_rejects_uncommitted_changes(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    head = _run("git", "rev-parse", "HEAD", cwd=repository)
    task = {
        "expected_head_sha": head,
        "git_command": ["git"],
        "worktree": str(repository),
    }

    _verify_task_checkout(task)
    (repository / "untracked.txt").write_text("review mutation\n", encoding="utf-8")

    with pytest.raises(ProcessRunnerError, match="not clean"):
        _verify_task_checkout(task)


def test_process_runner_rejects_mismatched_github_origin(tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    _run(
        "git",
        "remote",
        "set-url",
        "origin",
        "https://github.com/different/repository.git",
        cwd=repository,
    )
    config = RunnerConfig(
        type="process",
        repository_path=repository,
        worktree_root=tmp_path / "worktrees",
    )

    with pytest.raises(ProcessRunnerError, match="origin mismatch"):
        ProcessRunner(
            config,
            repository="owner/repository",
            tracker_command="gh",
            runtime_dir=tmp_path / "runtime",
            credentials={},
            report_only_harnesses=set(),
        )


def test_process_runner_fails_closed_without_posix_ownership(monkeypatch, tmp_path: Path) -> None:
    config = RunnerConfig(
        type="process",
        repository_path=tmp_path / "repository",
        worktree_root=tmp_path / "worktrees",
    )
    monkeypatch.setattr(process_adapter.os, "name", "nt")

    with pytest.raises(ProcessRunnerError, match="requires a POSIX platform"):
        ProcessRunner(
            config,
            repository="owner/repository",
            tracker_command="gh",
            runtime_dir=tmp_path / "runtime",
            credentials={},
            report_only_harnesses=set(),
        )


def test_closed_pull_request_fails_worker_instead_of_reporting_open(
    monkeypatch, tmp_path: Path
) -> None:
    runner, prs, _, _ = _runner_with_fake_cli(monkeypatch, tmp_path)
    session_id, _, head_sha = _spawn_finished_worker(runner)
    _publish_closed_fake_pr(prs, head_sha)

    session = runner.get_session(session_id)

    assert session is not None
    assert session.status == "failed"
    row = runner.state.get_session(session_id)
    assert row is not None
    assert str(row["last_error"]) == "pull request was closed without merging"
    runner.terminate("demo", session_id)


def test_process_runner_completes_worker_and_review_without_ao(monkeypatch, tmp_path: Path) -> None:
    repository, _ = _repository(tmp_path)
    fake_cli = _fake_cli(tmp_path)
    prs = tmp_path / "prs.json"
    reviews = tmp_path / "reviews.jsonl"
    agent_environment = tmp_path / "agent-environment.txt"
    claude_config = tmp_path / "claude-secondary"
    claude_config.mkdir()
    prs.write_text("[]\n", encoding="utf-8")
    reviews.write_text("", encoding="utf-8")
    agent_environment.write_text("", encoding="utf-8")
    monkeypatch.setenv("OA_FAKE_PRS", str(prs))
    monkeypatch.setenv("OA_FAKE_REVIEWS", str(reviews))
    monkeypatch.setenv("OA_FAKE_AGENT_ENV", str(agent_environment))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "parent-claude-account"))
    command = f"{sys.executable} {fake_cli}"
    config = RunnerConfig(
        type="process",
        repository_path=repository,
        worktree_root=tmp_path / "worktrees",
        verify_repository_remote=False,
        review_harness="claude-code",
        commands={
            "claude-code": command,
            "codex": command,
            "opencode": command,
        },
    )
    runner = ProcessRunner(
        config,
        repository="owner/repository",
        tracker_command=command,
        runtime_dir=tmp_path / "runtime",
        credentials={
            "claude-secondary": CredentialProfileConfig(
                execution_project_id="demo-process-claude-secondary",
                claude_config_dir=claude_config,
            )
        },
        report_only_harnesses=set(),
    )

    session = runner.spawn_worker(
        project_id="demo",
        work_item=WorkItem("17", "process fixture"),
        harness="claude-code",
        model=None,
        provider=None,
        credential_profile="claude-secondary",
        prompt="Handle the fixture.",
    )
    _wait_for(
        lambda: str(runner.state.get_session(session.id)["status"]),  # type: ignore[index]
        {"finished"},
    )
    row = runner.state.get_session(session.id)
    assert row is not None
    head_sha = _run("git", "rev-parse", "HEAD", cwd=Path(str(row["worktree_path"])))
    prs.write_text(
        json.dumps(
            [
                {
                    "number": 8,
                    "url": "https://github.com/owner/repository/pull/8",
                    "state": "OPEN",
                    "headRefOid": head_sha,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert runner.get_session(session.id).status == "pr_open"  # type: ignore[union-attr]
    prs.write_text(
        json.dumps(
            [
                {
                    "number": 8,
                    "url": "https://github.com/owner/repository/pull/8",
                    "state": "OPEN",
                    "headRefOid": "0" * 40,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="does not match"):
        runner.trigger_review(session.id)
    assert runner.state.get_review(session.id) is None
    prs.write_text(
        json.dumps(
            [
                {
                    "number": 8,
                    "url": "https://github.com/owner/repository/pull/8",
                    "state": "OPEN",
                    "headRefOid": head_sha,
                }
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    original_launch = runner._launch
    launch_entered = threading.Event()
    release_launch = threading.Event()
    launch_count = 0
    launch_lock = threading.Lock()

    def delayed_launch(task_path: Path) -> int:
        nonlocal launch_count
        with launch_lock:
            launch_count += 1
        launch_entered.set()
        assert release_launch.wait(5)
        return original_launch(task_path)

    monkeypatch.setattr(runner, "_launch", delayed_launch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runner.trigger_review, session.id)
        assert launch_entered.wait(5)
        second = pool.submit(runner.trigger_review, session.id)
        second.result(timeout=5)
        release_launch.set()
        first.result(timeout=5)
    assert launch_count == 1
    _wait_for(
        lambda: str(runner.state.get_review(session.id)["status"]),  # type: ignore[index]
        {"completed"},
    )
    review = runner.get_review(session.id)
    assert review is not None
    assert review.verdict == "approved"
    assert review.feedback == "review fixture"
    assert review.target_sha == head_sha
    assert reviews.read_text(encoding="utf-8")
    assert agent_environment.read_text(encoding="utf-8").splitlines() == [
        str(claude_config),
        "<default>",
    ]

    run_id = str(runner.state.get_review(session.id)["run_id"])  # type: ignore[index]
    runner.state.update_review_for_run(
        session.id,
        run_id,
        status="completed",
        verdict="changes_requested",
        feedback="fix it",
    )
    stale_token = f"{session.id}-resume-stale"
    runner._write_task(stale_token, {"fixture": True})
    assert runner.state.claim_feedback(session.id, run_id, stale_token)
    stale_time = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    runner.state.update_review_for_run(session.id, run_id, updated_at=stale_time)
    runner.state.update_session(session.id, updated_at=stale_time)
    before_resume = len(agent_environment.read_text(encoding="utf-8").splitlines())
    assert runner.get_session(session.id).status == "finished"  # type: ignore[union-attr]
    assert str(runner.state.get_review(session.id)["status"]) == "completed"  # type: ignore[index]
    runner.send(session.id, "Address the feedback once.")
    _wait_for(
        lambda: str(runner.state.get_session(session.id)["status"]),  # type: ignore[index]
        {"finished"},
    )
    assert str(runner.state.get_review(session.id)["status"]) == "feedback_sent"  # type: ignore[index]
    runner.send(session.id, "Do not send this twice.")
    time.sleep(0.2)
    assert len(agent_environment.read_text(encoding="utf-8").splitlines()) == (before_resume + 1)

    unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        runner.state.update_review_for_run(
            session.id,
            run_id,
            status="running",
            pid=unrelated.pid,
            helper_token="not-the-live-task",
        )
        runner.cancel_review(session.id)
        assert unrelated.poll() is None
        runner.state.update_session(
            session.id,
            status="working",
            pid=unrelated.pid,
            helper_token="not-the-live-task",
        )
        runner.terminate("demo", session.id)
        assert unrelated.poll() is None
        assert Path(str(row["worktree_path"])).exists()
    finally:
        os.killpg(unrelated.pid, 15)
        unrelated.wait(timeout=5)

    runner.state.update_session(
        session.id,
        status="finished",
        terminated=0,
        pid=None,
        helper_token=None,
    )
    runner.terminate("demo", session.id)
    assert not Path(str(row["worktree_path"])).exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group recovery is POSIX-specific")
def test_parent_launch_ack_cannot_overwrite_promoted_driver_pid(
    monkeypatch, tmp_path: Path
) -> None:
    runner, _, _, _ = _runner_with_fake_cli(monkeypatch, tmp_path)
    started = tmp_path / "race-agent-started"
    monkeypatch.setenv("OA_FAKE_AGENT_STARTED", str(started))
    monkeypatch.setenv("OA_FAKE_AGENT_SLEEP", "30")
    original_launch = runner._launch
    launched: dict[str, int] = {}

    def return_after_driver_promotion(task_path: Path) -> int:
        outer_pid = original_launch(task_path)
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        session_id = str(payload["session_id"])

        def promotion_state() -> str:
            row = runner.state.get_session(session_id)
            if row is None or row["pid"] is None:
                return "waiting"
            return "promoted" if int(row["pid"]) != outer_pid else "waiting"

        _wait_for(promotion_state, {"promoted"})
        row = runner.state.get_session(session_id)
        assert row is not None
        launched["outer"] = outer_pid
        launched["driver"] = int(row["pid"])
        return outer_pid

    monkeypatch.setattr(runner, "_launch", return_after_driver_promotion)
    session = runner.spawn_worker(
        project_id="demo",
        work_item=WorkItem("44", "launch PID race fixture"),
        harness="claude-code",
        model=None,
        provider=None,
        credential_profile=None,
        prompt="Wait while the supervisor tests launch ownership.",
    )
    _wait_for(lambda: "started" if started.is_file() else "waiting", {"started"})
    row = runner.state.get_session(session.id)
    assert row is not None
    assert int(row["pid"]) == launched["driver"]
    assert int(row["pid"]) != launched["outer"]
    worktree = Path(str(row["worktree_path"]))
    token = str(row["helper_token"])
    process_group = os.getpgid(launched["driver"])
    table = _process_table()
    providers = [
        pid
        for pid, command in table.items()
        if "fake_cli.py" in command
        and "oas-driver-sentinel" not in command
        and os.getpgid(pid) == process_group
    ]
    assert len(providers) == 1
    try:
        for pid in _owned_task_processes(launched["driver"], session.id, token):
            os.kill(pid, 9)
        _wait_for(
            lambda: (
                "gone"
                if not _owned_task_processes(launched["driver"], session.id, token)
                else "waiting"
            ),
            {"gone"},
        )
        assert _process_alive(providers[0])
        assert _process_group_alive(process_group)
        assert runner.get_session(session.id).status == "working"  # type: ignore[union-attr]
        runner.terminate("demo", session.id)
        assert _process_alive(providers[0])
        assert worktree.is_dir()
    finally:
        try:
            os.killpg(process_group, 9)
        except ProcessLookupError:
            pass
        _wait_for(
            lambda: "gone" if not _process_group_alive(process_group) else "waiting",
            {"gone"},
        )
        runner.terminate("demo", session.id)
    assert not worktree.exists()


@pytest.mark.skipif(os.name != "posix", reason="process-group recovery is POSIX-specific")
def test_review_parent_ack_cannot_overwrite_promoted_driver_pid(
    monkeypatch, tmp_path: Path
) -> None:
    runner, prs, _, _ = _runner_with_fake_cli(monkeypatch, tmp_path)
    session_id, _, head_sha = _spawn_finished_worker(runner)
    _publish_fake_pr(prs, head_sha)
    monkeypatch.setenv("OA_FAKE_AGENT_SLEEP", "30")
    original_launch = runner._launch
    launched: dict[str, int] = {}

    def return_after_driver_promotion(task_path: Path) -> int:
        outer_pid = original_launch(task_path)
        payload = json.loads(task_path.read_text(encoding="utf-8"))
        run_id = str(payload["run_id"])

        def promotion_state() -> str:
            row = runner.state.get_review(session_id)
            if row is None or row["pid"] is None or str(row["run_id"]) != run_id:
                return "waiting"
            return "promoted" if int(row["pid"]) != outer_pid else "waiting"

        _wait_for(promotion_state, {"promoted"})
        row = runner.state.get_review(session_id)
        assert row is not None
        launched["outer"] = outer_pid
        launched["driver"] = int(row["pid"])
        return outer_pid

    monkeypatch.setattr(runner, "_launch", return_after_driver_promotion)
    runner.trigger_review(session_id)
    review = runner.state.get_review(session_id)
    assert review is not None
    assert int(review["pid"]) == launched["driver"]
    assert int(review["pid"]) != launched["outer"]

    runner.cancel_review(session_id)
    _wait_for(
        lambda: (
            "gone"
            if not _owned_task_processes(
                launched["driver"], session_id, str(review["helper_token"])
            )
            else "waiting"
        ),
        {"gone"},
    )
    runner.terminate("demo", session_id)


@pytest.mark.skipif(os.name != "posix", reason="process-group recovery is POSIX-specific")
def test_terminate_stops_driver_after_process_worker_is_killed(monkeypatch, tmp_path: Path) -> None:
    runner, _, _, _ = _runner_with_fake_cli(monkeypatch, tmp_path)
    started = tmp_path / "agent-started"
    monkeypatch.setenv("OA_FAKE_AGENT_STARTED", str(started))
    monkeypatch.setenv("OA_FAKE_AGENT_SLEEP", "30")
    monkeypatch.setenv("OA_FAKE_IGNORE_TERM", "1")
    session = runner.spawn_worker(
        project_id="demo",
        work_item=WorkItem("43", "orphan worker fixture"),
        harness="claude-code",
        model=None,
        provider=None,
        credential_profile=None,
        prompt="Wait while the supervisor tests recovery.",
    )
    row = runner.state.get_session(session.id)
    assert row is not None
    worktree = Path(str(row["worktree_path"]))
    try:
        _wait_for(lambda: "started" if started.is_file() else "waiting", {"started"})
        row = runner.state.get_session(session.id)
        assert row is not None
        token = str(row["helper_token"])
        owned = _owned_task_processes(int(row["pid"]), session.id, token)
        table = _process_table()
        helpers = [
            pid for pid in owned if "agent_workflow_supervisor.process_worker" in table.get(pid, "")
        ]
        assert len(helpers) == 1
        os.kill(helpers[0], 9)

        assert runner.get_session(session.id).status == "working"  # type: ignore[union-attr]
        assert worktree.is_dir()
        runner.terminate("demo", session.id)
        _wait_for(
            lambda: "removed" if not worktree.exists() else "retained",
            {"removed"},
            timeout=10,
        )
        assert not _owned_task_processes(int(row["pid"]), session.id, token)
    finally:
        runner.terminate("demo", session.id)


@pytest.mark.skipif(os.name != "posix", reason="hard-crash recovery is POSIX-specific")
def test_review_comment_crash_retries_persisted_verdict_without_rereview(
    monkeypatch, tmp_path: Path
) -> None:
    runner, prs, reviews, agent_environment = _runner_with_fake_cli(monkeypatch, tmp_path)
    session_id, _, head_sha = _spawn_finished_worker(runner)
    _publish_fake_pr(prs, head_sha)
    monkeypatch.setenv("OA_FAKE_VERDICT", "changes_requested")
    monkeypatch.setenv("OA_FAKE_REVIEW_SLEEP_AFTER_POST", "2")

    runner.trigger_review(session_id)
    _wait_for(
        lambda: str(runner.state.get_review(session_id)["status"]),  # type: ignore[index]
        {"posting"},
    )
    _wait_for(
        lambda: "posted" if reviews.read_text(encoding="utf-8") else "waiting",
        {"posted"},
    )
    review = runner.state.get_review(session_id)
    assert review is not None
    token = str(review["helper_token"])
    table = _process_table()
    helpers = [
        pid
        for pid in _owned_task_processes(int(review["pid"]), session_id, token)
        if "agent_workflow_supervisor.process_worker" in table.get(pid, "")
    ]
    assert len(helpers) == 1
    os.kill(helpers[0], 9)

    _wait_for(
        lambda: runner.get_review(session_id).status,  # type: ignore[union-attr]
        {"failed"},
        timeout=10,
    )
    failed = runner.state.get_review(session_id)
    assert failed is not None
    assert str(failed["verdict"]) == "changes_requested"
    assert str(failed["feedback"]) == "review fixture"
    invocation_count = len(agent_environment.read_text(encoding="utf-8").splitlines())

    monkeypatch.setenv("OA_FAKE_VERDICT", "approved")
    monkeypatch.setenv("OA_FAKE_REVIEW_SLEEP_AFTER_POST", "0")
    runner.trigger_review(session_id)
    _wait_for(
        lambda: str(runner.state.get_review(session_id)["status"]),  # type: ignore[index]
        {"completed"},
    )
    completed = runner.get_review(session_id)
    assert completed is not None
    assert completed.verdict == "changes_requested"
    assert len(agent_environment.read_text(encoding="utf-8").splitlines()) == invocation_count
    posted_bodies = []
    for line in reviews.read_text(encoding="utf-8").splitlines():
        arguments = json.loads(line)
        posted_bodies.append(arguments[arguments.index("--body") + 1])
    assert len(posted_bodies) == 2
    assert all("changes_requested" in body for body in posted_bodies)

    runner.terminate("demo", session_id)
