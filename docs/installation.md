# Installation

This guide installs the supervisor for one local GitHub repository. Start in
shadow mode, validate reads, and only then enable external mutations.

## Prerequisites

- macOS or another environment where the selected runner adapter works.
- Python 3.12 or newer.
- [uv](https://docs.astral.sh/uv/).
- Git.
- [GitHub CLI](https://cli.github.com/) authenticated with `gh auth login`.
- Agent Orchestrator (`ao`) running and able to manage the target repository.
- At least one AO worker harness, such as Claude Code, Codex, or OpenCode.

The built-in runner and tracker adapters currently support AO and GitHub. The
LangGraph layer is provider-neutral, but other runners and trackers require
new `RunnerPort` or `TrackerPort` adapters.

The local-model integration was verified on 2026-08-31 with AO 0.12.9,
OpenCode 1.18.8, LM Studio's `lms` CLI, and an MLX Qwen2.5 Coder 7B model. These
are a tested baseline, not hard dependency pins. Use `ao agent ls --json` and
the installed harness documentation when upgrading their CLI surfaces.

Verify the external tools before installing:

```bash
python3 --version
uv --version
git --version
gh auth status
ao status --json
ao agent ls --json
```

## Install

Install version 0.2.1 directly from GitHub:

```bash
uv tool install git+https://github.com/weryk153/agent-workflow-supervisor.git@v0.2.1
oa --help
```

For local development, clone the repository and install it in editable mode:

```bash
git clone https://github.com/weryk153/agent-workflow-supervisor.git
cd agent-workflow-supervisor
uv sync --dev
uv tool install --editable . --force
oa --help
```

A packaged local installation can use a built wheel instead:

```bash
uv build
uv tool install dist/agent_workflow_supervisor-*.whl --force
```

## Register the repository with AO

The target must be a Git repository with a usable default branch and remote.
AO creates worktrees from the remote branch, so a local-only commit without an
`origin/<default-branch>` is insufficient.

```bash
ao project add \
  --path /absolute/path/to/repository \
  --id my-project \
  --name "My Project" \
  --worker-agent claude-code
```

Confirm AO can resolve it:

```bash
ao project get my-project --json
```

## Create the first supervisor configuration

Copy `examples/generic.toml` outside the source checkout and replace at least:

- `project.id`
- `tracker.repository`
- runner command if `ao` is not on `PATH`
- `policy.merge_mode` (`manual` or `automatic`)
- labels, harness limits, and model names for the target project

Install it in shadow mode first:

```bash
oa setup --source /path/to/my-project.toml
oa validate --project my-project
```

`oa setup` copies the configuration into the supervisor's configuration
directory, registers the project, and installs a managed command block in the
AO project's orchestrator rules. It preserves orchestrator rules outside that
managed block and does not modify the application repository. Without
`--apply`, `supervisor.shadow_mode` remains whatever the source file declares;
the example declares `true`.

From this point, AO is the normal interface. In the project's orchestrator
conversation, ask “Show the supervisor status” or “Handle issue #123.” The
orchestrator runs the mapped `oa` command itself. See [AO integration](ao-integration.md)
for the supported requests and the smaller set of tasks that still require a
terminal.

For an installation smoke test, run a read-only workflow tick against a real
issue from a terminal:

```bash
oa tick --project my-project --work-item 123
```

In shadow mode the supervisor may read AO and GitHub state, but it does not
spawn or terminate sessions, trigger reviews, merge changes, or mutate pull
requests.

## Enable real mutations

After validating routes and paths, either reinstall the configuration with
`--apply` or set `shadow_mode = false` in the installed project TOML:

```bash
oa setup --source /path/to/my-project.toml --apply
oa validate --project my-project
oa start --project my-project
```

`--apply` changes only the installed copy. Keep the source template under your
own configuration management if you want a reproducible setup.

## Dispatch work

The service does not scan GitHub autonomously. Normally, tell the AO
orchestrator:

```text
Handle issue #123.
```

AO runs this command on your behalf:

```bash
oa dispatch 123 --project my-project
oa status --project my-project --work-item 123
```

The example defaults to manual merge, so every approved change pauses for an
exact decision. Protected labels also pause when automatic mode is selected:

```bash
oa approve 123 --project my-project --decision approve
```

The same choice is available from the AO conversation or CLI:

```bash
oa project merge-mode my-project
oa project merge-mode my-project --set automatic
```

Stopping the supervisor does not stop AO or existing AO workers:

```bash
oa stop --project my-project
```

Continue with [configuration](configuration.md),
[AO integration](ao-integration.md),
[multi-project operation](multi-project.md), or
[local model profiles](model-profiles.md).
