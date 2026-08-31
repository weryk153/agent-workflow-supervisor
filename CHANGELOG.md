# Changelog

Notable changes are documented here. This project follows semantic versioning
after its first stable release; pre-1.0 releases may still change configuration
with an explicit migration note.

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
