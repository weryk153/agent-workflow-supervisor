# Multi-project operation

Accounts and model profiles are global resources. Each project explicitly
selects which ones it may use. A login or local model is never assigned to a
repository merely because it exists on the machine.

## Register AO projects

Register every repository with AO first:

```bash
ao project add \
  --path /absolute/path/game \
  --id game-project \
  --name "Game Project" \
  --worker-agent claude-code

ao project add \
  --path /absolute/path/website \
  --id website-project \
  --name "Website Project" \
  --worker-agent opencode
```

Both repositories need a GitHub remote and a pushed default branch.

## Register supervisor projects

Install one project from a prepared TOML with `oa setup`. Other existing AO
projects can be registered directly:

```bash
oa project register game-project
oa project register website-project
oa project list
```

For a non-default project, the supervisor creates
`$XDG_CONFIG_HOME/agent-workflow-supervisor/projects/<id>.toml` when needed.
Without `XDG_CONFIG_HOME`, the base is `~/.config`.

Automatic creation copies safe workflow defaults and derives the GitHub
repository from the AO project's remote. Review the generated config before
using apply mode:

```bash
oa validate --project website-project
```

## Assign accounts by project

Create a global secondary login once:

```bash
oa account add secondary
```

Then select per-project pools:

```bash
oa project accounts game-project --set default,secondary
oa project accounts website-project --set secondary
```

The default login uses the base AO project. A non-default Claude login needs an
isolated configuration directory, checkout, and AO execution project because
AO environment is project-scoped. The supervisor creates those derived
resources under:

```text
$XDG_DATA_HOME/agent-workflow-supervisor/execution-projects/<project>/<account>
```

These execution projects are implementation details. Keep
`registry.toml` as the assignment source of truth. Reconciliation verifies that
each managed checkout's `origin` identifies the same GitHub repository as its
base AO project.

## Assign models by project

Register models globally:

```bash
oa model add claude-sonnet \
  --harness claude-code \
  --model claude-sonnet-5 \
  --capacity 2

oa model add local-coder \
  --harness opencode \
  --model lmstudio/my-local-coder \
  --provider lmstudio \
  --capacity 1
```

Assign different pools:

```bash
oa project models game-project \
  --set claude-sonnet,local-coder \
  --default claude-sonnet

oa project models website-project \
  --set local-coder \
  --default local-coder
```

The issue label `agent:local-coder` selects that profile only in projects where
it is assigned. An unassigned profile is rejected during config validation.

## Operate explicitly

Always name the project in scripts:

```bash
oa start --project game-project
oa start --project website-project

oa dispatch 123 --project game-project
oa dispatch 45 --project website-project

oa status --project game-project
oa status --project website-project
```

Each project has its own queue, runtime directory, PID, and checkpoint database
as defined by its project TOML. Do not point multiple simultaneously running
projects at the same runtime directory or SQLite checkpoint file.

## Changes and restarts

- Changing a project's account or model assignment restarts that supervisor if
  it was running.
- Replacing a global model profile restarts running projects assigned to it.
- Account-derived AO execution projects are reconciled to the effective default
  Claude model when either the profile definition or project assignment
  changes.
- Restarting the supervisor does not stop AO or terminate existing workers.
- Existing active workers are reconciled by work item and harness on the next
  tick.
