# Changelog

Notable changes are documented here. This project follows semantic versioning
after its first stable release; pre-1.0 releases may still change configuration
with an explicit migration note.

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
