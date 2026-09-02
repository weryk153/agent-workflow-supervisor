# Using the supervisor from AO

Agent Workflow Supervisor is designed to be paired with Agent Orchestrator.
LangGraph persists and reconciles workflow policy; AO remains the desktop user
interface, agent runner, session manager, and worktree owner. Users should not
need a second chat application or a terminal for normal daily operation.

## One-time integration

Register the repository with AO, then install its supervisor configuration from
an external terminal:

```bash
ao project add \
  --path /absolute/path/to/repository \
  --id my-project \
  --name "My Project" \
  --worker-agent claude-code

oa setup --source /absolute/path/to/my-project.toml
```

For another AO project, use:

```bash
oa project register another-project
```

Both commands install a managed block in the AO project's `orchestratorRules`.
The block is delimited by `[LANGGRAPH_SUPERVISOR]` and
`[/LANGGRAPH_SUPERVISOR]`. Re-running integration replaces only that block;
rules before or after it belong to the operator and are preserved.

The managed rules include the absolute installed `oa` path when available, so
the AO agent does not depend on a shell-specific `PATH` setup.

## Daily conversation flow

Open the project's orchestrator session in AO and use explicit requests. The
orchestrator executes the underlying command and summarizes the JSON result.

| Request in AO | Command AO runs |
|---|---|
| “Handle issue #220.” | `oa dispatch 220 --project my-project` |
| “What is the status of issue #220?” | `oa status --project my-project --work-item 220` |
| “Approve issue #220.” | `oa approve 220 --project my-project --decision approve` |
| “Reject issue #220.” | `oa approve 220 --project my-project --decision reject` |
| “Does this project merge automatically?” | `oa project merge-mode my-project` |
| “Require approval before merge.” | `oa project merge-mode my-project --set manual` |
| “Enable automatic merge.” | `oa project merge-mode my-project --set automatic` |
| “List configured Claude accounts.” | `oa account list` |
| “Switch this project to the work account.” | `oa project switch-account my-project --use work` |
| “List model profiles.” | `oa model list` |
| “Which models are assigned here?” | `oa project models my-project` |

The issue request must authorize execution. “Explain issue #220,” “plan issue
#220,” or merely mentioning the number does not allow dispatch. Account and
model names must come from registered profiles rather than being guessed.

## What happens after dispatch

`oa dispatch` idempotently adds the issue to the project's durable queue and
starts the background supervisor if necessary. LangGraph selects the configured
route, enforces capacity, asks AO to acquire a worker, reconciles review and CI,
pauses at any configured human gate, performs a guarded merge, and cleans the
worker. In the default `manual` merge mode, every merge is such a gate. In
`automatic` mode, only matching protected labels pause. AO continues to display
the actual sessions and worktrees.

When dispatch runs inside an AO conversation, it records that orchestrator's
`AO_SESSION_ID`. The background service sends that conversation a read-only
update when the workflow reaches an approval gate, completes, or encounters an
actionable blocker. The orchestrator refreshes `oa status` and summarizes the
result; a supervisor update never counts as user authorization for approval,
rejection, merge, or redispatch. Successful deliveries are checkpointed in the
job database so the normal reconciliation loop does not repeat them. If an
account switch replaced the original session, notification falls back only
when exactly one active orchestrator exists in the base AO project.

Dispatch from an external terminal has no originating AO conversation and
therefore remains headless; inspect it with `oa status` or an intentional
automation.

The separate native AO relay covers workers created through ordinary
`ao spawn`, even when their project has no supervisor workflow configuration.
It first looks for AO's structural automation messages to identify the sender.
If none exists, it binds only when that project has exactly one active
orchestrator. Chat workers contribute their latest structured provider reply;
TUI workers still report lifecycle state without fabricating unavailable
output. Relayed worker text is explicitly quoted as untrusted, read-only data,
and delivery never changes either session's interface mode.

An approval request succeeds only while the workflow is paused for the exact
change id and head SHA shown by that gate. Sending “approve” early does not queue
permission for a future pull request.

Merge mode is project policy rather than a property of the current AO session.
Changing it from the AO conversation updates the project's supervisor TOML and
restarts only that supervisor when needed. It never retroactively treats an old
approval as permission for a newer head.

The AO orchestrator should not duplicate this scheduling by directly spawning
an issue worker after dispatch. The managed rule block tells it that the
supervisor owns worker acquisition and lifecycle for supervised work items.

## Account switching inside AO

Claude Code reads `CLAUDE_CONFIG_DIR` only at process startup, so the current
session cannot change identity in place. A synchronous `bind-account --restart`
would ask the orchestrator to terminate itself while its command is still
running. The conversation-safe command is therefore asynchronous:

```bash
oa project switch-account my-project --use work
```

It performs these steps:

1. Confirms the command is running in this project's AO orchestrator through
   `AO_SESSION_ID`.
2. Refuses the change if any worker is active or any durable worker acquisition
   remains pending or temporarily unverified.
3. Repeats the worker check under the global allocation lock, validates the
   registered Claude login, saves the project binding, and records a durable
   barrier against new worker acquisition.
4. Starts a detached helper and returns control to the current AO turn.
5. Waits until AO reports that turn idle twice.
6. Terminates the old orchestrator, restores the AO project if the installed AO
   release removed it, and creates a replacement using the selected account.
7. Prompts the replacement to announce completion.

The new session does not inherit the prior conversation context. Resend any
unfinished request. If the helper fails, the binding is still saved for future
sessions, the allocation barrier is released, and its log path is present in
the command output. If the source session disappears before replacement, the
helper recreates the orchestrator instead of failing on the missing target.
The marker records scheduler/helper process ownership, so a later allocation
pass can reclaim it after an abnormal process exit.

## When a terminal is still required

Use an external terminal for:

- installing or upgrading AO and this package;
- the interactive `oa account add` OAuth login;
- first-time AO project registration and `oa setup`;
- scripts or CI that intentionally bypass conversation;
- repair when AO or its current orchestrator cannot run commands;
- direct `oa project bind-account ... --restart` recovery.

These are control-plane bootstrap or recovery operations. Dispatch, status,
approval, merge-policy selection, account selection, and model inspection are
ordinary AO conversation operations after integration.

## Refresh or remove the managed rules

Re-run `oa project register my-project` to refresh the managed block after an
upgrade. The current CLI intentionally has no removal command. To detach the
supervisor manually, remove only the text from `[LANGGRAPH_SUPERVISOR]` through
`[/LANGGRAPH_SUPERVISOR]` in AO project settings; do not delete unrelated
operator rules.
