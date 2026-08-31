# Contributing

Thank you for improving Agent Workflow Supervisor. Keep the durable graph
provider-neutral and isolate provider-specific behavior in adapters.

## Development setup

Requirements are Python 3.12+ and `uv`.

```bash
git clone https://github.com/weryk153/agent-workflow-supervisor.git
cd agent-workflow-supervisor
uv sync --dev
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv build
```

Install an editable CLI for manual tests:

```bash
uv tool install --editable . --force
oa --help
```

## Design rules

- `graph.py` depends on `RunnerPort` and `TrackerPort`, not provider SDKs.
- Put AO, GitHub, or model-provider command translation in adapters or a
  focused integration module.
- Keep routing and capacity calculations pure where possible.
- Persist identifiers and non-secret paths, never OAuth tokens or API keys.
- Preserve shadow mode as the default.
- Bind review approval to the current change head.
- Keep dispatch explicit; do not add autonomous issue scanning by accident.
- Preserve backward compatibility for documented TOML fields or document a
  migration.
- Do not hard-code usernames, repositories, absolute developer paths, account
  names, commercial models, or local model ids in library behavior.

Examples may name generic tools and models, but must label placeholders and
must not imply that model weights share this project's Apache license.

## Tests

Run all gates before submitting:

```bash
uv run pytest -q
uv run ruff format --check .
uv run ruff check .
uv build
```

Pull requests and pushes to `main` run the same locked gates on Python 3.12 and
3.13. GitHub Actions are pinned to immutable release commits; update the commit
and its version comment together.

Changes should include proportionate tests:

- Policy changes: unit tests for ordering, skip behavior, and capacity edges.
- Graph changes: in-memory LangGraph tests proving idempotence and safety gates.
- Registry changes: round-trip, preservation, and rollback tests.
- Provider health checks: mocked tests with no network or model downloads.
- Adapter changes: assert exact command construction and error translation.

Manual AO tests must use a disposable project. Clean up the session, AO
project, worktree, and temporary repository afterward. Do not use a production
repository merely to prove spawn works.

## Documentation

Update the relevant guide whenever behavior changes. CLI examples must match
`oa <command> --help`. Configuration documentation must distinguish global
registry fields, project TOML fields, and derived AO state.

For software integrations, link to primary vendor documentation and record the
version used for a verified smoke test when behavior is version-sensitive.

## Open-source safety check

Before publishing or packaging, inspect for local or secret material:

```bash
rg -n '/Users/|/home/|API_KEY|ACCESS_TOKEN|CLIENT_SECRET' . \
  --glob '!uv.lock' \
  --glob '!dist/**' \
  --glob '!.git/**'

git status --short --untracked-files=all
```

Review every match rather than assuming all path or credential-like strings are
unsafe. Placeholder paths in documentation are acceptable; real home
directories, repository URLs, tokens, generated registry files, and provider
auth files are not.

Do not commit:

- `~/.config/agent-workflow-supervisor/`
- `~/.local/share/agent-workflow-supervisor/`
- Claude configuration directories
- OpenCode `auth.json`
- model-provider credentials
- AO runtime databases or worktrees
- project `.state/` runtime data

## Release checklist

1. Run tests, Ruff formatting and lint checks, and the package build.
2. Inspect wheel and source archive contents.
3. Run the secret/path scan above.
4. Verify the generic example in an isolated XDG config directory.
5. Smoke-test one shadow workflow.
6. When an integration changed, test it with a disposable AO project.
7. Update version, changelog or release notes, and compatibility notes.
8. Confirm `LICENSE`, `NOTICE`, `SECURITY.md`, and documentation links ship in
   the source distribution.

## Scope of contributions

Small adapters and clear safety improvements are preferred over framework-wide
rewrites. Multi-host scheduling, hosted control planes, and additional tracker
backends should begin with an issue describing persistence, locking, secret,
and failure-recovery requirements.
