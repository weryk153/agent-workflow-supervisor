# Running without AO

The process runner is a complete execution path, not an AO wrapper. It creates
isolated git worktrees, starts agent CLIs as detached processes, persists their
session metadata in SQLite, discovers their pull requests through `gh`, runs an
independent reviewer, and returns requested changes to the original session.
The LangGraph workflow, capacity limits, CI gates, approval policy, and guarded
merge behavior are identical to AO mode.

Process mode currently requires a POSIX platform such as macOS, Linux, or WSL.
It fails closed on native Windows because Windows does not provide the same
token-verifiable process-group ownership used for safe detached shutdown. Use
AO mode on native Windows.

## Configure a project

Start from [`examples/process.toml`](../examples/process.toml). Set the GitHub
repository and local checkout, then install it:

```bash
oa setup --source /path/to/process.toml --apply
oa dispatch 123
```

`oa setup` resolves repository and worktree paths before copying the config, so
the background service does not depend on its working directory. Process mode
never launches or checks AO.

```toml
[runner]
type = "process"
repository_path = "/absolute/path/to/repository"
worktree_root = "/absolute/path/outside/the/repository"
review_harness = "claude-code"
review_model = "claude-sonnet-5"

[runner.commands]
claude-code = "claude"
codex = "codex"
opencode = "opencode"
```

The checkout must be the root of a git worktree with an `origin` remote. Each
worker receives a unique branch and a worktree under `worktree_root`. Keep that
root outside the source checkout. Completed clean worktrees are removed after
merge; a dirty worktree is retained for recovery.

## Agent drivers

The built-in drivers use each CLI's noninteractive, resumable interface:

| Harness | Initial turn | Follow-up |
| --- | --- | --- |
| `claude-code` | `claude -p --output-format json` | `--resume <session>` |
| `codex` | `codex exec --json` | `codex exec resume <session>` |
| `opencode` | `opencode run --format json` | `--session <session>` |

Prompts are passed through stdin. Configured commands are parsed into argument
arrays and are never interpolated into shell source. On POSIX systems, a fixed
`/bin/sh` ownership wrapper forwards those arguments through `"$@"`. Driver
output and terminal state are written below the project's configured
`supervisor.runtime_dir/process-runner`. Temporary task files use mode `0600`
and are removed after the detached turn records its result. Each driver runs in
its own token-bearing process group. A small guard keeps that ownership token
observable even if the state helper is force-killed, so cleanup never removes a
worktree while its CLI may still be using it.

Process-mode reviewers are limited to Claude Code or Codex. The supervisor
forces Claude reviewers into `plan` mode and Codex reviewers into the
`read-only` sandbox, regardless of the worker permissions. It verifies both
the exact pull-request head and a clean git worktree before and after the
review, and records a verdict only after the GitHub review comment succeeds.
OpenCode remains available for workers, but is not accepted as the independent
review harness because its CLI does not expose an equivalent read-only sandbox.

Claude defaults to `acceptEdits`. Additional noninteractive tool grants can be
listed with `claude_allowed_tools`. At minimum, a Claude implementation worker
needs scoped `git` and `gh` grants to commit, push, and open its pull request;
add only the project-specific build and test commands it needs. Without those
grants, a noninteractive Claude turn stops when it reaches an unapproved shell
command. Selecting `bypassPermissions` also passes Claude's dangerous bypass
flag and should only be used inside an external sandbox. Codex defaults to
`--approve-for-me`, which implies the
`workspace-write` sandbox; it does not use the unrestricted bypass flag. Set
`codex_approve_for_me = false` to pass `codex_sandbox` explicitly instead.

```toml
[runner]
type = "process"
claude_permission_mode = "acceptEdits"
claude_allowed_tools = ["Bash(git *)", "Bash(gh *)"]
codex_sandbox = "workspace-write"
codex_approve_for_me = true
```

## Multiple Claude logins

Account registration and project assignment are shared by both runners:

```bash
oa account add work --config-dir ~/.claude-work
oa project accounts my-project --set default,work
```

In process mode this does not clone the repository or create AO projects. Each
credential profile is only a logical capacity namespace; the selected worker
receives that profile's `CLAUDE_CONFIG_DIR` in its own environment. Other
workers and the supervisor environment are unchanged. The default profile
explicitly clears an inherited `CLAUDE_CONFIG_DIR`. A separate
`review_credential_profile` can select the account used by the reviewer.

## Local and open models

Use OpenCode for any provider/model pair it supports, including Ollama and LM
Studio. Model profiles and label routing are runner-independent. For example:

```bash
oa model add local-coder \
  --harness opencode \
  --model ollama/qwen3-coder \
  --provider ollama \
  --capacity 1
oa project models my-project --set local-coder
oa model doctor local-coder --project my-project
```

The supervisor selects and limits the profile; OpenCode owns provider
configuration, model availability, and inference.

Codex can also use its native local-provider path. A profile with
`harness = "codex"` and `provider = "lmstudio"` or `provider = "ollama"`
automatically adds `--oss --local-provider <provider>` and removes that prefix
from the model id passed to Codex:

```bash
oa model add local-codex \
  --harness codex \
  --model lmstudio/qwen2.5-coder-7b-instruct-mlx \
  --provider lmstudio \
  --capacity 1
```

## Recovery

`oa status --work-item <id>` exposes workflow and adapter failures. Process
logs are under `supervisor.runtime_dir/process-runner/logs`. A worker that exits
successfully has a bounded PR-discovery window (120 seconds by default); after
that it becomes `worker_unhealthy` instead of waiting forever. Reviewer
failures use the same bounded retry policy as AO reviews.

Stopping the supervisor does not kill an active detached worker. Restarting it
reopens the process-runner database, reconciles live PIDs and pull requests,
and resumes the durable graph.

Review verdict and feedback are committed to SQLite before the GitHub comment
is attempted. If the helper stops after GitHub accepts the comment but before
the local completion write, recovery retries only that same comment; it does
not rerun the reviewer or replace the persisted verdict. The comment contains
a stable review-run marker, so duplicate comments from this crash window are
identifiable.

Feedback delivery is claimed before launch and acknowledged only after the
provider process has accepted the prompt on stdin. A claim that never reaches
the provider is recovered after the launch grace period. Like other external
side effects, a machine crash immediately after delivery can produce an
at-least-once retry; provider CLIs do not expose a portable transactional
message API. Prompts should therefore remain safe to repeat.

`oa stop` signals only processes whose command contains the expected launch
token. If ownership cannot be proven, it retains the worktree. A later stop or
status reconciliation can clean it after the process group has exited; the
runner prefers a recoverable worktree leak over deleting files beneath an
unverified live process.
