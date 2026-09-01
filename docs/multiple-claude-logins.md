# Multiple Claude logins

The supervisor supports multiple Claude Code logins without placing credentials
in TOML or LangGraph checkpoints.

The existing AO project's ordinary Claude login remains the default profile and
does not receive an explicit `CLAUDE_CONFIG_DIR`. Only additional logins get an
isolated configuration directory.

With `runner.type = "process"`, the same account commands inject the selected
`CLAUDE_CONFIG_DIR` into each Claude subprocess. No extra clone or AO project is
created. The default profile explicitly clears any `CLAUDE_CONFIG_DIR`
inherited from the supervisor service, so it cannot silently use a different
account. See [Running without AO](process-runner.md#multiple-claude-logins).

## Switch from the AO conversation

After the project has been integrated, tell its AO orchestrator:

```text
List the configured Claude accounts.
Switch this project to the work Claude account.
```

The orchestrator runs `oa account list`, resolves only a registered account
name, and then runs:

```bash
oa project switch-account my-project --use work
```

The command saves the new project binding and returns immediately. A detached
helper watches the current `AO_SESSION_ID`; after the orchestrator finishes its
answer and becomes idle, the helper terminates it and creates a replacement
orchestrator under the selected account. The replacement starts with a short
handoff notice, but prior conversation context is not transferred. Resend any
unfinished request.

The switch is rejected while an AO worker is active. This prevents an account
change from stranding work owned by the previous login. The helper replaces
only the orchestrator that requested the switch, even if another orchestrator
is active in the same AO project, and uses a fresh name for the replacement so
AO cannot satisfy the spawn with a surviving old session. Binding and final
worker discovery share the supervisor's global allocation lock. A durable
switch marker also prevents a new supervised worker from starting after the
check but before replacement. If the source orchestrator disappears first, the
helper still creates a replacement and then releases the marker. Pending or
unverified durable worker reservations reject the switch even when AO's session
list is temporarily empty. A wait timeout releases the marker, and a later
allocation pass reclaims it if both its scheduler and helper have died.

## External-terminal binding and recovery

AO 0.12.9 has no account selector in its New Project dialog. Bind the AO
project from an external terminal when initially adopting a login, scripting,
or recovering a failed in-conversation switch:

```bash
oa account add work --config-dir /absolute/private/path/claude-work
oa project bind-account my-project --use work --restart
```

The first command adopts the already authenticated directory without another
OAuth login. The second saves `CLAUDE_CONFIG_DIR` in AO's project environment,
terminates the existing orchestrator, restores the AO project registration if
that AO release removed it with the last session, and then starts a replacement
orchestrator. It refuses the restart while active workers exist. This order is
required because spawning first may cause AO to reuse the old session. This
path does not require GitHub and does not create another clone.

When several orchestrators are active, inspect their ids and select the exact
one to replace:

```bash
ao session ls --project my-project --all --json
oa project bind-account my-project --use work --restart --session <session-id>
```

Without this selection the command refuses before saving a different binding.
Any orchestrator not selected remains on its original startup account and is
reported under `active_sessions_still_using_previous_account`.

Without `--restart`, only sessions opened after the command use the new account;
all current sessions retain their startup environment. To return a project to
the normal Claude login, bind the built-in `default` account and restart:

```bash
oa project bind-account my-project --use default --restart
```

Direct bindings live in AO's project configuration. They are deliberately
separate from the supervisor account pool shown by `oa project accounts` and
the `assigned_projects` field in `oa account list`.

Use the pooled setup below only when one supervised project must run multiple
Claude workers under different accounts concurrently.

## Why pooled accounts require separate AO projects

Claude Code reads login state from `CLAUDE_CONFIG_DIR`. AO forwards environment
variables from project configuration into sessions, but AO 0.12.9 has no
per-spawn environment flag. It also rejects registering the same filesystem path
twice. Each isolated login therefore needs a separate local clone and AO project.

This is an AO adapter constraint, not a LangGraph constraint. The process
runner injects a profile environment independently for each process and does
not use the clone strategy described in this section.

## One-time setup

First add each login globally. This command does not clone or select a project:

```bash
oa account add secondary
```

Then explicitly assign one or more accounts to each AO project:

```bash
oa project accounts game-project --set default,secondary
oa project accounts website-project --set secondary
oa project accounts service-project --set default
```

One account name means every Claude worker for that project uses that login.
Multiple names form an ordered least-active pool. Check the result without
exposing tokens:

```bash
oa account list
oa project accounts game-project
```

The project-assignment step creates a dedicated execution checkout only when AO
needs one for a non-default login. Process mode creates only logical profile
identities and uses its normal per-worker worktrees. AO checkout paths are
intentionally structured as `execution-projects/<project>/<account>`.

Assignment is also reconciliation. On every run, the supervisor verifies that
an existing derived AO project points to that managed checkout and reapplies
the current account directory, Claude worker harness, selected model, and the
base project's permission policy. It verifies that the checkout's `origin`
resolves to the same GitHub repository as the base project and fails closed if
the path, AO project id, or remote points somewhere else. Selecting the
built-in `default` account removes any stale direct `CLAUDE_CONFIG_DIR`
override from the base AO project so it cannot keep using a previously bound
isolated login.

Remote validation parses URL and SCP-style Git syntax and requires the exact
`github.com` host. A lookalike domain cannot satisfy repository identity.

Removing an account from the allowed pool removes it from future routing but
retains its credential profile as a discovery record. Existing workers in that
account's AO execution project therefore remain visible and are reused or
reported as a route conflict instead of being silently duplicated.

The manual steps below document what the commands automate and remain useful for
custom filesystem layouts.

Use absolute paths throughout. The following placeholders are deliberately not
ready-to-run credentials or repository values.

```bash
mkdir -p /absolute/private/path/claude-primary
CLAUDE_CONFIG_DIR=/absolute/private/path/claude-primary claude auth login

mkdir -p /absolute/private/path/claude-secondary
CLAUDE_CONFIG_DIR=/absolute/private/path/claude-secondary claude auth login
```

Verify each profile in isolation:

```bash
CLAUDE_CONFIG_DIR=/absolute/private/path/claude-primary claude auth status
CLAUDE_CONFIG_DIR=/absolute/private/path/claude-secondary claude auth status
```

Create two independent clones of the same remote and register each with AO:

```bash
git clone https://github.com/OWNER/REPOSITORY.git /absolute/path/project-claude-primary
git clone https://github.com/OWNER/REPOSITORY.git /absolute/path/project-claude-secondary

ao project add \
  --path /absolute/path/project-claude-primary \
  --id project-claude-primary \
  --name "Project — Claude primary" \
  --worker-agent claude-code

ao project add \
  --path /absolute/path/project-claude-secondary \
  --id project-claude-secondary \
  --name "Project — Claude secondary" \
  --worker-agent claude-code

ao project set-config project-claude-primary \
  --env CLAUDE_CONFIG_DIR=/absolute/private/path/claude-primary \
  --worker-agent claude-code

ao project set-config project-claude-secondary \
  --env CLAUDE_CONFIG_DIR=/absolute/private/path/claude-secondary \
  --worker-agent claude-code
```

Do not commit either Claude configuration directory. It contains authentication
state. Prefer a private directory outside every source repository.

## Supervisor configuration

```toml
[credentials]
strategy = "least-active"

[credentials.profiles.claude-primary]
execution_project_id = "project-claude-primary"
max_workers = 1
claude_config_dir = "/absolute/private/path/claude-primary"

[credentials.profiles.claude-secondary]
execution_project_id = "project-claude-secondary"
max_workers = 1
claude_config_dir = "/absolute/private/path/claude-secondary"

[policy]
default_harness = "claude-code"

[policy.capacity]
claude-code = 2

[policy.credential_profiles]
claude-code = ["claude-primary", "claude-secondary"]
```

`policy.capacity.claude-code = 2` is the total Claude worker limit across both
accounts. `max_workers = 1` is the limit for one login. Profile order breaks ties,
so the first profile is selected when both are equally idle.

Login capacity is global to the resolved `claude_config_dir`, not multiplied by
the number of supervised projects. Two projects that point to the same login
therefore share its `max_workers` budget even though their AO execution-project
ids differ. The default Claude login is treated as one shared identity too.
Direct or in-conversation binding of a base AO project updates its dynamic
credential identity immediately. If that binding now aliases a login already
used by a derived pool project, both routes consume the same login budget; the
profile names cannot create duplicate capacity.

## Durable and secret-safe state

Checkpoints contain only values such as `claude-secondary` and
`project-claude-secondary`. They never contain Claude OAuth data. Authentication
continues to be owned by the isolated Claude configuration directories and AO's
project-scoped environment.

If every configured login is at its profile limit, the workflow stops at
`waiting_profile_capacity`. A later tick retries selection without spawning a
duplicate worker.
