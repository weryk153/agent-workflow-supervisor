# Changelog

Notable changes are documented here. This project follows semantic versioning
after its first stable release; pre-1.0 releases may still change configuration
with an explicit migration note.

## Unreleased

### Added

- AO-originated dispatches now remember the live orchestrator session and
  report approval gates, completion, and actionable blockers back to that
  conversation. Notification state is persisted for duplicate suppression,
  failures remain visible without changing workflow status, and a replaced
  origin falls back only to a single unambiguous active orchestrator.

### Fixed

- AO sessions created by the supervisor no longer force Chat or TUI mode.
  Worker spawns and account-switch replacement orchestrators now preserve AO's
  native mode selection, while durable reports continue through AO's
  mode-aware send channel.

### Compatibility

- Existing `jobs.sqlite` databases add nullable notification columns
  automatically. Existing jobs and dispatches from external terminals remain
  headless because no origin session can be proven.

## 0.3.0 - 2026-09-01

### Added

- AO-independent `process` runner with owned git worktrees, detached agent
  processes, SQLite session metadata, pull-request discovery, review execution,
  review comments, resumable feedback turns, and safe cleanup.
- Built-in noninteractive drivers for Claude Code, Codex, and OpenCode.
- Native Codex local-provider routing for LM Studio and Ollama model profiles.
- Per-process `CLAUDE_CONFIG_DIR` injection, allowing multiple Claude accounts
  without AO execution projects or duplicate repository clones.
- Process-runner setup example, configuration reference, recovery guide, and
  unit/integration fixtures that run without AO.

### Fixed

- The background service now checks AO readiness only for AO configurations;
  process mode never opens or queries AO.
- Feedback delivery is durably claimed before a resume helper starts and is
  acknowledged only after the provider process accepts the prompt. Recovery
  preserves at-least-once semantics across the unavoidable post-delivery crash
  window exposed by provider CLIs.
- Reviewers are atomically claimed, tied to the exact local and remote head,
  forced into read-only settings, and accepted only if the worktree remains
  clean and the GitHub review comment succeeds. A verdict is persisted before
  comment delivery, so comment-only recovery cannot rerun or flip it.
- Detached-process shutdown verifies the session and per-launch task token
  before signaling a token-bearing process group. Orphaned provider processes
  retain their worktree until ownership is reconciled, preventing stale or
  reused PIDs from targeting an unrelated process or triggering unsafe cleanup.
- Parent launch acknowledgement can no longer overwrite a child-promoted
  driver process-group PID; helper-to-driver promotion uses a compare-and-swap
  on the expected helper PID for workers, feedback turns, and reviewers.
- Process mode fails closed on native Windows until equivalent token-verifiable
  process ownership is available; macOS, Linux, and WSL remain supported.
- Closed, unmerged pull requests now fail their process-runner session instead
  of being reported as open and entering a futile review loop.
- `oa model doctor` now resolves the process runner's configured harness
  command, including wrapper arguments, instead of assuming a default binary
  name on `PATH`.
- Claude's default credential profile now explicitly clears an inherited
  `CLAUDE_CONFIG_DIR`, preventing the service account from leaking into a
  worker or reviewer.
- Codex `--approve-for-me` no longer conflicts with an explicit `--sandbox`
  flag; configuration rejects incompatible sandbox choices.
- Successful agent exits receive a bounded pull-request discovery window
  instead of failing on transient GitHub visibility latency or waiting forever.

### Compatibility

- `runner.type = "ao"` remains the default and existing project TOML files do
  not require migration.
- Process mode is opt-in with `runner.type = "process"`. It uses the same graph,
  policy, registry, account commands, approval gates, and GitHub tracker.

## 0.2.1 - 2026-09-01

### Fixed

- Trigger the initial AO review when a worker reports `pr_open` before AO has
  created any review record. Previously that race left the workflow in
  `worker_running` forever despite an open pull request.

## 0.2.0 - 2026-09-01

### Added

- Per-project `policy.merge_mode` with `manual` and `automatic` choices.
- `oa project merge-mode` for inspecting or changing the policy from AO or a
  terminal, including safe supervisor restart when needed.
- AO managed rules that map explicit merge-policy and merge requests to the
  head-bound supervisor approval flow.

### Compatibility

- Missing `policy.merge_mode` defaults to `manual`, so upgrading an existing
  project fails safe by requiring confirmation. Set it to `automatic` to retain
  the previous non-protected auto-merge behavior.

## 0.1.1 - 2026-09-01

### Fixed

- Added a durable AO review watchdog so a missing, failed, or timed-out
  reviewer cannot leave a workflow silently stuck in `review_pending`.
- Limited automatic review attempts and exposed exhausted workflows as
  `review_stalled` with the cause visible through `oa status`.
- Recognized AO's latest review-run status instead of allowing a stale
  aggregate `needs_review` value to hide a failed run.

### Added

- Configurable `review_timeout_seconds` and `review_max_attempts` supervisor
  settings, defaulting to 30 minutes and two total attempts.
- Recovery for an explicitly triggered AO review or a new pull-request head,
  both of which receive a fresh bounded attempt budget.

### Compatibility

- Existing project TOML files and LangGraph checkpoints require no migration.
  Missing settings use the new defaults, and old review state adopts AO's
  current run metadata on the next reconciliation.

## 0.1.0 - 2026-09-01

### Added

- Durable LangGraph workflow with SQLite checkpoints and explicit dispatch.
- AO runner and GitHub tracker adapters.
- Shadow mode, current-head review validation, protected approval interrupts,
  guarded merges, and worker cleanup.
- Global Claude account registry with explicit per-project account pools.
- Reusable model profiles with per-project assignment, label routing, and
  conservative capacity.
- Read-only model doctor checks for AO, OpenCode, Ollama, and LM Studio.
- TUI-mode AO spawning for the Antigravity (`agy`) harness.
- Report-only harness workflows that complete without requiring a pull request.
- Existing Claude config-directory adoption and direct AO project account
  binding for manual sessions, including safe orchestrator replacement.
- AO-first managed orchestrator rules for routine conversation-driven dispatch,
  status, approval, account selection, and model inspection.
- Deferred in-conversation Claude account switching that waits for the source
  AO turn to become idle before replacing only that orchestrator.
- Multi-project CLI registration and listing.
- Atomic, head-bound approval consumption with complete merge-gate
  revalidation after resume.
- Cross-process worker acquisition locks and durable fail-closed reservations
  that prevent duplicate workers across config versions.
- User-global allocation locking and resource-tagged reservations that enforce
  shared model-profile and Claude-login limits across supervised projects.
- Dynamic AO execution-project credential identities, preventing a directly
  rebound base project from double-counting a pooled Claude login.
- Race-free deferred account switching with a durable worker barrier and
  replacement recovery when the source orchestrator disappears, plus timeout
  cleanup, dead-helper reclamation, and uncertain-worker rejection.
- Verified service ownership records, exclusive daemon lifetime locks, and
  stop-timeout failures that prevent stale daemons or unrelated PIDs from
  being mutated.
- Managed execution-checkout origin validation and automatic effective-model
  reconciliation for pooled Claude account projects.
- Exact GitHub remote-host validation and canonical issue identities shared by
  the queue, checkpoints, AO discovery, and worker acquisition.
- Project-wide capacity and account-switch locks, durable capacity occupancy,
  and cross-process registry transactions.
- Guarded report-only completion that requires observed activity or stable idle
  observations before cleanup.
- Locked GitHub Actions gates for formatting, lint, tests, and package builds on
  Python 3.12 and 3.13.
- Complete installation, configuration, local-model, architecture,
  troubleshooting, contributing, and security documentation.

### Compatibility

- Legacy `[policy.models]`, harness routes, and capacity tables remain valid.
- Model-profile use is opt-in; existing projects do not require migration.
