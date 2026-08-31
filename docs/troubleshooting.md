# Troubleshooting

Start with local configuration and service state:

```bash
oa validate --project my-project
oa status --project my-project
oa project list
oa model list
oa account list
```

`oa start` prints the supervisor log path. The default is under the configured
`supervisor.runtime_dir`.

## AO does not run supervisor commands from conversation

The AO project may predate integration or its managed rule block may have been
removed. Refresh it from an external terminal:

```bash
oa project register my-project
```

Then inspect the AO project's orchestrator rules for the delimited
`[LANGGRAPH_SUPERVISOR]` block. Operator-owned rules outside that block should
remain unchanged. Interactive login and AO-down recovery still use an external
terminal; normal dispatch, status, approval, account selection, and model
inspection should be requested in the AO conversation.

## Agent Orchestrator is not ready

Symptom:

```text
Agent Orchestrator did not become ready
```

Check:

```bash
ao status --json
ao doctor
ao agent ls --json
```

On macOS the supervisor attempts to open Agent Orchestrator automatically. It
waits up to the service timeout but does not reinstall or repair AO.

## AO project or remote is invalid

Symptoms include `PROJECT_NOT_FOUND`, automatic config creation failure, or
worktree errors referring to `refs/remotes/origin/<branch>`.

Check:

```bash
ao project get my-project --json
git -C /absolute/path/to/repository remote -v
git -C /absolute/path/to/repository branch -vv
```

The target needs a GitHub remote and a pushed default branch. A local-only Git
repository is not enough for AO worktree creation.

For pooled Claude accounts, every managed execution checkout must also have an
`origin` resolving to the same GitHub `owner/repository` as the base AO project.
The remote parser requires the exact `github.com` host; lookalike domains or a
`github.com` string embedded in another host/path are rejected. Reassignment
fails closed on a missing, unreadable, or mismatched origin rather than
attaching an account worker to the wrong repository.

## Harness unsupported or not installed

```bash
ao agent ls --json
oa model doctor PROFILE --project my-project
```

`supported` means the AO version recognizes the harness. `installed` means its
executable is present. Some local harnesses report an unknown auth state and
still work; the spawn runtime remains the final validation.

If AO reports `SESSION_MODE_UNSUPPORTED` for `agy`, upgrade the supervisor to
a version that spawns Antigravity through AO's TUI mode. Antigravity may still
show `authStatus: unknown`; a disposable read-only spawn is the authoritative
runtime check.

## OpenCode cannot see a local model

```bash
opencode models <provider>
```

The exact profile model must appear as `provider/model`. If it does not:

- Verify the OpenCode provider id matches the prefix in the OA model profile.
- Verify the provider model id matches the server's `/v1/models` response.
- Check OpenCode config syntax and precedence.
- Restart the model server or reload the model.

OpenCode supports `OPENCODE_CONFIG` and `OPENCODE_CONFIG_CONTENT` for isolated
testing, but production supervisor use should have provider config visible to
the AO-spawned OpenCode process.

## Ollama model missing

```bash
ollama list
ollama show <model>
curl http://127.0.0.1:11434/v1/models
oa model doctor PROFILE --project my-project
```

The supervisor never runs `ollama pull`. Install the intended weights
explicitly, review their license and disk size, then retry the doctor.

## LM Studio server or model missing

```bash
lms server status
lms ls
lms ps
lms server start
lms load <model-key> --identifier my-local-coder
curl http://127.0.0.1:1234/v1/models
oa model doctor PROFILE --project my-project
```

Use the load identifier in both OpenCode and the OA model profile. The default
doctor expects LM Studio's OpenAI-compatible endpoint on `127.0.0.1:1234`.

## Config references an unknown profile

Symptoms:

```text
default model profile is not configured
route model profiles are not configured
```

Check assignment and effective configuration:

```bash
oa model list
oa project models my-project
oa validate --project my-project
```

A `profile = "name"` route is valid only if that global profile is assigned to
the project.

## Waiting for capacity

- `waiting_capacity`: the harness limit or selected model-profile limit is full.
- `waiting_profile_capacity`: every eligible credential profile is at its
  `max_workers` limit.

Inspect AO sessions and configured limits:

```bash
ao session ls --project my-project --all --json
oa validate --project my-project
```

The next service tick retries without spawning a duplicate worker.
Capacity is recomputed under a user-global allocation lock immediately before
spawn. Harness limits remain project-local; model-profile and isolated-login
limits use resource identities stored in durable reservations across projects.
Reservations also consume capacity while AO visibility is uncertain.

`waiting_account_switch` means an in-conversation account switch has saved its
binding and is waiting to replace the source orchestrator. New supervised
workers are held until the helper completes or fails; its log path was returned
by `oa project switch-account`.

## Worker route conflicts

`worker_route_conflict` means the work item already has an active worker but the
current policy selects another harness, or AO reports multiple active workers
for the same item. The supervisor stops instead of adding another worker.

Inspect the work item, current labels, policy, and AO sessions:

```bash
oa status --project my-project --work-item 123
oa session ls --project my-project --all --json
oa validate --project my-project
```

Choose which active worker should remain, intentionally terminate or finish the
other session, and then retry. Do not change capacity merely to bypass this
state.

## Worker acquisition remains pending

`worker_acquisition_pending` is a fail-closed reservation. It normally means a
spawn process stopped or returned an uncertain result before the supervisor
could record AO's session id. The canonical records are under
`~/.local/share/agent-workflow-supervisor/acquisitions/` and include the project,
work item, execution project, harness, model-profile identity, and a non-secret
credential resource key; they contain no tokens or credential contents.

Inspect the named AO execution project for a worker with that work-item id. If
one exists, run another tick; the supervisor includes the execution project
stored in the pending record even when the current config no longer contains
that credential profile, then repairs the reservation from the live session.
If no worker exists, verify that across AO before removing only that work item's
`.json` reservation and retrying. Keep the `.lock` file. Never clear a pending
record merely to force another spawn while AO state is uncertain.

Because a pending reservation represents a worker that may already exist, it
also occupies harness and credential-profile capacity for other work items
until recovery or verified manual resolution.

`worker_reservation_unverified` is similar but names a previously recorded
worker session. It means AO could not currently verify that session. The
supervisor preserves the reservation and refuses to spawn; retry after AO is
healthy. Clear it only after AO authoritatively shows that the recorded session
is terminated or absent and no worker for the item remains.

## Claude account binding did not affect the session

`CLAUDE_CONFIG_DIR` is captured when a Claude Code session starts. A session
that was already open cannot switch accounts in place. Inspect the registered
accounts first:

```bash
oa account list
oa project bind-account my-project --use work --restart
```

From a healthy AO orchestrator conversation, prefer asking it to switch the
account. It runs the asynchronous form:

```bash
oa project switch-account my-project --use work
```

Its helper log is returned in the command JSON and stored under
`$XDG_DATA_HOME/agent-workflow-supervisor/account-switches/` (or the equivalent
`~/.local/share` path). Inspect that log if no replacement appears.

The restart form refuses to run while active workers exist. Finish or terminate
those workers intentionally, then retry. The command saves the project binding,
terminates the old orchestrator, and starts a new one. If the installed AO
release removes the project registration when its last orchestrator exits, the
command restores the project from the captured path and configuration before
spawning the replacement.

If more than one orchestrator is active, external recovery must identify the
one to replace. Inspect the sessions, then pass its exact id:

```bash
ao session ls --project my-project --all --json
oa project bind-account my-project --use work --restart --session <session-id>
```

The command refuses before changing the AO project config when the selection is
ambiguous. An in-conversation `switch-account` automatically targets only the
orchestrator that requested the switch.

If replacement spawning fails after the old orchestrator has stopped, the
account binding is still saved. Confirm the project and retry the same command:

```bash
ao project get my-project --json
oa project bind-account my-project --use work --restart
```

AO's New Project dialog has no account dropdown in the tested release. Use one
AO project per fixed manual account, or use `oa project accounts --set` when a
supervised project needs a concurrent multi-account worker pool.

## Worker is unhealthy

`worker_unhealthy` means the checkpoint references a worker that AO reports as
missing or inactive. Inspect the specific work item and AO session:

```bash
oa status --project my-project --work-item 123
ao session get <session-id> --json
```

Resolve or terminate stale AO state before re-dispatching. Do not delete the
checkpoint database merely to hide an unexplained mismatch.

## Review is stale or merge waits

- `review_pending`: AO has a review that is still inside its configured
  timeout and retry budget.
- `review_stalled`: a review disappeared, failed, or exceeded
  `review_timeout_seconds` for every allowed attempt. `oa status` includes the
  exact reason. The supervisor keeps monitoring but does not create unbounded
  reviewers.
- `review_invalid`: an approved review did not identify its target head SHA;
  the supervisor cannot prove what was reviewed and will not merge it.
- `review_stale`: the approved review target SHA no longer matches the PR head.
- `waiting_change_gate`: the PR is draft, not mergeable, not clean, or has a
  non-successful check.
- `awaiting_approval`: manual merge mode or a protected label requires an
  explicit decision for the exact PR head.

Check GitHub directly:

```bash
gh pr view <number> --repo owner/repository \
  --json headRefOid,isDraft,mergeable,mergeStateStatus,statusCheckRollup
```

For a stalled review, inspect AO before intervening:

```bash
ao review ls <worker-session-id> --json
ao session get <worker-session-id> --json
```

If no reviewer is actually running, an explicit `ao review trigger
<worker-session-id>` creates a new AO run. The supervisor recognizes its new
run id and gives it a fresh bounded watchdog budget. A new pull-request head
SHA also starts a fresh budget automatically. Restarting AO or deleting the
LangGraph database is not the normal recovery path.

The supervisor will not treat an approval for an older or unidentified commit
as current. After protected human approval, it fetches the change again and
revalidates the current head, draft state, mergeability, merge state, and CI.

## Safe restart

```bash
oa stop --project my-project
oa start --project my-project
```

This stops only the LangGraph supervisor. AO and its existing workers remain
running and are reconciled on the next tick. The daemon holds an exclusive
lifetime lock and writes an atomic JSON ownership record at
`supervisor.runtime_dir/supervisor.pid`; the file is not a plain PID.

`oa stop` verifies the project id, config path, random instance token, and live
process command before sending `SIGTERM`. It refuses to signal an unrelated or
unverifiable PID, and reports an error if the verified daemon does not exit
before the timeout. Inspect the ownership record and `ps -ww -p <pid> -o
command=` before any manual cleanup. Do not delete or replace a live ownership
record merely to force configuration changes.
