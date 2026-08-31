# CLI reference

Run `oa <command> --help` for the installed version's exact flags. Commands
print JSON so they can be inspected or scripted.

These commands are the integration surface between AO and the durable
supervisor. After `oa setup` or `oa project register`, routine commands are
normally selected and executed by the AO orchestrator in response to explicit
conversation requests. Running them directly remains supported for bootstrap,
scripts, diagnostics, and recovery.

## Installation and validation

| Command | Effect |
|---|---|
| `oa setup --source FILE` | Installs one default project config; preserves shadow mode. |
| `oa setup --source FILE --apply` | Installs the config and forces shadow mode off. |
| `oa validate [--project ID]` | Validates and prints effective config; no provider calls. |

## Service and work queue

| Command | Effect |
|---|---|
| `oa start [--project ID]` | Ensures AO is ready and starts the project supervisor. |
| `oa stop [--project ID]` | Stops only the supervisor process. |
| `oa status [--project ID]` | Shows service and queue status. |
| `oa status --work-item N [--project ID]` | Also shows the LangGraph checkpoint. |
| `oa dispatch N [--project ID]` | Idempotently queues one issue and starts the service. |
| `oa tick --work-item N [--project ID]` | Runs one reconciliation pass. |
| `oa tick --work-item N --apply` | Allows mutations for that one tick. |
| `oa approve N --decision approve` | Approves the exact change/head currently paused for merge. |
| `oa approve N --decision reject` | Rejects the exact change/head currently paused for merge. |

Dispatch does not scan for other issues. Re-dispatching an active work item does
not create a duplicate queue row.

For the built-in GitHub tracker, `194`, `#194`,
`github:owner/repository#194`, and that repository's full GitHub issue URL are
one canonical work item. A qualified reference to another repository is
rejected instead of sharing a queue, checkpoint, or worker lock.

Approval is not a future instruction. The command fails unless the LangGraph
thread and durable job row are both currently waiting at the same approval gate.
The queued decision records that gate's change id and head SHA; a later or
different change cannot consume it. The service atomically consumes the
decision before resuming LangGraph. If the process crashes in that window, the
workflow asks for approval again instead of reusing the decision.

## Accounts

| Command | Effect |
|---|---|
| `oa account add NAME` | Interactively creates one isolated Claude login. |
| `oa account add NAME --config-dir DIR` | Adopts an existing authenticated `CLAUDE_CONFIG_DIR`. |
| `oa account list` | Shows login readiness and assigned projects without tokens. |
| `oa project bind-account ID --use NAME` | Binds future manual AO sessions to one account; no clone or tracker config required. |
| `oa project bind-account ID --use NAME --restart` | Saves the binding, terminates the sole old orchestrator, recovers the AO project registration if needed, then creates a replacement. |
| `oa project bind-account ID --use NAME --restart --session SESSION` | Selects the exact orchestrator to replace when several are active. |
| `oa project switch-account ID --use NAME` | AO-conversation form: schedules replacement after the current orchestrator turn becomes idle. |
| `oa project accounts ID` | Shows the project's allowed Claude accounts. |
| `oa project accounts ID --set A,B` | Replaces the allowed pool and creates required AO execution projects. |

Changing account assignment restarts the project supervisor only if it was
already running. It does not terminate existing AO workers.

Profiles removed from the allowed pool are retained internally for session
discovery but are no longer eligible for new work. This allows an already
running worker in its AO execution project to finish without becoming invisible
to duplicate-worker checks.

`bind-account` is for AO's ordinary project/session UI and selects one account
for all future sessions in that AO project. `project accounts --set` is the
separate pooled-supervisor feature: it may create execution checkouts so two
Claude accounts can work concurrently. AO does not expose an account selector
in its New Project dialog, and a running session cannot change
`CLAUDE_CONFIG_DIR`. Without `--restart`, every active session continues using
its original account. With `--restart`, the command refuses to proceed while
any worker is active; it must terminate the old orchestrator before spawning
the replacement because AO may otherwise reuse the existing session. If
several orchestrators are active, omitting `--session` is ambiguous and fails
before the project binding is changed. A targeted result lists untouched
sessions under `active_sessions_still_using_previous_account`; their startup
environment does not change retroactively.

`oa account list` reports `assigned_projects` for supervisor account pools
created by `project accounts --set`. A direct manual `bind-account` is stored in
AO's project configuration instead and is not represented as a supervisor pool
assignment.

Use `switch-account` from inside the AO orchestrator conversation. It requires
`AO_SESSION_ID`, refuses to run from a worker or while workers are active, saves
the binding under the global allocation lock, blocks new supervised workers,
and starts a detached helper. Once the current turn is idle, the helper replaces
the old orchestrator and sends the new session a short handoff prompt. A source
session that disappears is recreated. Use `bind-account --restart` from an
external terminal for recovery; do not make an orchestrator synchronously
terminate itself.

## Model profiles

| Command | Effect |
|---|---|
| `oa model add NAME --harness H --model M` | Creates or replaces a global profile. |
| `oa model list` | Shows profiles and assigned projects. |
| `oa model doctor NAME [--project ID]` | Performs read-only readiness checks. |
| `oa project models ID` | Shows allowed and default profiles. |
| `oa project models ID --set A,B --default A` | Replaces the project model pool. |

If `--default` is omitted while setting a model pool, the first name becomes
the default. At least one profile is required; duplicate or unknown names are
rejected. The CLI currently has no global profile deletion command.

Replacing an assigned global profile restarts each affected running supervisor.
Changing either a global profile or a project's default profile also reconciles
derived Claude-account execution projects to the effective worker model. A
failed reconciliation restores the prior registry assignment. These commands
do not download weights, start a model server, or edit provider config.

## Projects

| Command | Effect |
|---|---|
| `oa project register ID` | Registers an existing AO project and creates supervisor config if needed. |
| `oa project list` | Lists supervisor projects, config paths, accounts, and models. |
| `oa project merge-mode ID` | Shows whether the project uses manual or automatic merge. |
| `oa project merge-mode ID --set manual` | Requires an explicit user decision for every merge. |
| `oa project merge-mode ID --set automatic` | Merges after review and all change gates pass. |

Automatic project config creation requires the AO project to have a GitHub
remote from which `owner/repository` can be derived.

Changing merge mode safely restarts that project's supervisor only when it was
already running. `automatic` does not bypass review, current-head matching,
draft, mergeability, merge-state, CI, or label-based approval gates.

`setup`, `project register`, and `project accounts --set` install or refresh the
managed `[LANGGRAPH_SUPERVISOR]` block in AO's orchestrator rules. Text outside
the managed start/end markers remains operator-owned.

## Configuration selection

Most commands accept `--config FILE` or `--project ID`:

1. An explicit `--config` wins.
2. An explicit `--project` resolves through `registry.toml`.
3. Otherwise `OA_CONFIG` or the default installed config is used.

Use `--project` in automation to avoid accidentally operating on the default
project.
