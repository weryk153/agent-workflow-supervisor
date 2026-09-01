# Architecture

The supervisor separates durable workflow policy from agent execution.

```text
explicit user request or AO conversation
    │
    ▼
LangGraph supervisor ───── registry.toml + project TOML ── GitHub issue / PR
    │                           │
    │ spawn/review/kill         │ accounts, models, routes, limits
    ▼                           │
RunnerPort                      │
    ├── AO ── Claude / Codex / OpenCode
    └── ProcessRunner ── own worktree + Claude / Codex / OpenCode
```

## Responsibilities

### LangGraph supervisor

- Persists one workflow thread per project and work item.
- Loads the work item and applies deterministic routing.
- Enforces harness, model-profile, and credential-profile capacity.
- Reuses an active matching worker instead of spawning duplicates. If policy
  now routes the same work item to another harness, it stops with
  `worker_route_conflict` for operator resolution.
- Serializes final discovery, reservation, and spawn with nested canonical
  cross-process locks: one user-global allocation lock protects shared
  capacity, and one work-item lock protects worker identity. Under the global
  lock it rescans the configured runner, counts durable pending or unverified
  reservations as occupied, and reruns project harness limits plus global
  model-profile and login limits before spawning. Reservations persist the
  resolved model profile and login identity, so separate project
  configurations still contend for the same resource. A reservation also
  exposes the selected worker by session id even when two config versions
  contain different credential-project sets. Pending recovery scans the
  execution project recorded by the reservation rather than relying on the
  current config alone. The credential key first consults the AO execution
  project's dynamic binding, then falls back to the profile configuration.
  Consequently, rebinding a base AO project cannot make that project and a
  derived project count the same Claude login under two names.
- Reconciles runner review state. Review run identity, start time, target SHA, and
  attempt count are checkpointed. A watchdog restarts a missing, failed, or
  timed-out run within a bounded budget and then exposes `review_stalled`
  instead of waiting silently forever.
- Verifies that review approval matches the current pull-request head SHA.
- Checks draft, mergeability, merge state, and status checks.
- Interrupts for manual-merge policy or protected-label human approval. The
  queue accepts a decision only while that exact change and head SHA are
  paused, and the graph revalidates every change gate after resume.
- Merges with a head-commit guard and cleans up the worker.

### Agent Orchestrator

- Provides the primary user conversation and invokes mapped `oa` commands.
- Owns agent processes, chat/TUI sessions, worktrees, and session lifecycle.
- Applies project-scoped environment and permission settings.
- Runs the selected harness and model override.
- Owns review sessions and their result metadata.

AO is optional. These responsibilities apply only when `runner.type = "ao"`.

### Process runner

- Creates one unique git branch and worktree per worker.
- Starts Claude Code, Codex, or OpenCode through noninteractive CLI contracts.
- Persists process, provider-session, worktree, and review metadata in SQLite.
- Resumes the original provider session when review changes are requested.
- Discovers pull requests and posts review comments through `gh`.
- Atomically claims each review attempt and feedback delivery in SQLite.
- Validates the configured checkout's GitHub origin, the exact reviewed head,
  and a clean worktree before and after a read-only reviewer turn.
- Runs each provider CLI in a token-bearing process group, signals it only when
  the live command matches the unique launch token stored in SQLite, and keeps
  the worktree whenever a surviving group cannot yet be ruled out.
- Persists reviewer verdicts before GitHub comment delivery and uses
  comment-only recovery, preventing a crash from rerunning the model and
  changing an already-decided verdict.
- Never opens, checks, or depends on AO.

### GitHub

- Owns issue content, labels, pull requests, checks, and merges.
- Is accessed through the authenticated `gh` CLI.

### Model providers

- Own model weights, serving, authentication, context limits, and inference.
- Are configured in the selected harness, not in this supervisor.
- Are never downloaded or started automatically by profile assignment.

## Workflow sequence

```text
explicit dispatch
  → load issue
  → skip / select route
  → reuse worker or check capacity
  → select least-active credential profile
  → spawn through the configured runner
  → inspect worker and review
  → verify current PR head and checks
  → optional policy/label approval interrupt
  → revalidate current PR head and checks
  → guarded squash merge
  → terminate and clean worker
  → report the durable outcome to the dispatching AO orchestrator
```

The background service repeatedly reconciles durable state. It does not infer
new work from repository labels and does not scan the issue tracker.

## Persistence

- `jobs.sqlite` stores the explicit dispatch queue, its originating AO session
  when present, and durable notification/deduplication state.
- The configured LangGraph SQLite database stores workflow checkpoints.
- AO stores its execution sessions and worktrees independently. Process mode
  stores equivalent metadata in `process-runner/state.sqlite` and owns its git
  worktrees directly.
- `registry.toml` stores global non-secret assignments.
- `supervisor.pid` is an atomic JSON ownership record containing the daemon
  PID, project id, resolved config path, and a random instance token. A
  project-scoped lifetime lock prevents two daemons from owning the same
  runtime directory.
- The canonical per-user acquisition directory stores non-secret pending/worker
  reservations, dynamic AO execution-project credential bindings, deferred
  switch ownership, and project operation locks independently of configurable
  runtime directories.

A supervisor crash does not erase runner sessions or the workflow checkpoint.
On restart, reconciliation resumes from the stored thread and attempts to reuse
an active matching worker.

Review watchdog fields are additive checkpoint state. Workflows created by
older releases adopt AO's current review run and its `createdAt` timestamp on
their next reconciliation, so upgrading does not require deleting checkpoints.

Account-pool updates retain inactive credential profile records for discovery
but remove them from future routing. This lets an existing worker in a retired
AO execution project remain visible until it finishes and is cleaned up.

## Safety boundaries

- Shadow mode blocks spawn, review trigger, merge, and termination mutations.
- Review and human approval are bound to a non-empty change id and reviewed head
  SHA. A decision cannot be queued before that exact interrupt exists.
- The service atomically removes a matching decision before graph resume and
  clears every stale, migrated, or newly published-gate decision. A crash can
  lose an approval but cannot replay it.
- Draft, mergeability, merge-state, and CI gates are revalidated after an
  approval interrupt; approval alone cannot bypass a changed gate.
- A route/account-policy change and concurrent reconciliation cannot create a
  second active worker for the same work item through the supported runtime.
- An uncertain spawn remains `worker_acquisition_pending` and fails closed
  until runner state or an operator resolves it. A transient lookup cannot
  erase a known worker reservation; it stops as
  `worker_reservation_unverified`.
- Profile and account assignment are explicit per project.
- Equivalent GitHub issue references are canonicalized before queue,
  checkpoint, AO-session comparison, and acquisition locking; another
  repository's qualified issue never aliases the configured repository.
- Harness roles, label meanings, models, and capacities are operator policy;
  the package ships no Claude/Codex/Antigravity division of labor.
- Tokens are not copied into project TOML, registry, or LangGraph state.
- Registry read-modify-write transactions are cross-process locked; writes are
  atomic and mode `0600`.
- Service stop verifies the live process command against the complete ownership
  record before sending a signal. An unverified PID or stop timeout fails
  closed, so a configuration mutation cannot continue under an old daemon.
- Model provider setup remains outside the supervisor.

## Adapter boundaries

`RunnerPort` and `TrackerPort` define the graph-facing interfaces. The built-in
implementations are `AoRunner`, `ProcessRunner`, and `GitHubTracker`. A new
runner or tracker should translate its native state into the domain values in
`models.py`; the graph should not import provider SDKs directly.

The AO adapter does not select a session interface. It omits `--mode` when it
spawns workers or replacement orchestrators, leaving AO to choose its native
default for the project and harness. The durable workflow observes session and
review state through AO's CLI, and supervisor updates use AO's mode-aware
`send` command, so neither TUI nor Chat is required by the supervisor.

`oa setup`, `oa project register`, and account-pool assignment manage one
delimited block of AO orchestrator rules. Operator rules outside the block are
preserved. The managed block requires explicit execution intent and maps AO
conversation requests to the CLI rather than duplicating scheduling policy in
the orchestrator prompt.

An orchestrator cannot safely synchronously kill itself during an account
change. `oa project switch-account` therefore records the binding and launches
a process outside the AO session. That helper waits for the source turn to
become idle before using the normal guarded replacement path. The source
session id is carried through the replacement, so another active orchestrator
in the same AO project is not terminated accidentally. External recovery must
select a session explicitly when more than one orchestrator is active. The new
orchestrator receives a fresh session name so a surviving old session cannot be
mistaken for the replacement. A project-scoped cross-process lock covers the
binding write, targeted termination, possible re-registration, and replacement
spawn, so concurrent switches cannot exchange credential identities. The
binding path also holds the user-global allocation lock and performs its final
worker check there. A durable pending-switch marker blocks new supervised
workers until the detached helper completes or fails. If the source session
has already disappeared, the helper creates the replacement without trying to
terminate a nonexistent session. Pending or unverified worker reservations are
also treated as active ownership and reject the account mutation. The marker
tracks scheduler/helper process ownership so it can be reclaimed after an
abnormal exit instead of blocking the project indefinitely.

## Current limitations

- One local supervisor process per project; SQLite is not a multi-host queue.
- Built-in tracker support is GitHub only.
- The process runner supports the Claude Code, Codex, and OpenCode CLI
  contracts; arbitrary command templates are not yet a stable public API.
- AO's session list does not expose every resolved model, so profile capacity
  counts all active workers sharing the harness.
- Multiple isolated account support is currently specialized for Claude Code.
- Model profiles do not configure OpenCode, Ollama, LM Studio, or credentials.
- Account/model profile deletion and project unregistration commands are not
  yet implemented.
- AO's New Project dialog has no account selector in the tested release; direct
  bindings use `oa project bind-account` after the AO project exists.

For multi-process deployment, use a shared durable queue and PostgreSQL-backed
LangGraph checkpointer rather than the local SQLite runtime.
