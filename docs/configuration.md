# Configuration

The installed configuration has two sources with different ownership:

1. `registry.toml` owns global accounts, global model profiles, and their
   project assignments.
2. One project TOML owns runner, tracker, workflow, route, and legacy harness
   policy for that project.

At runtime the loader resolves the registered model profiles into an effective
`AppConfig`. It does not copy model definitions into the project TOML or AO.

## File locations

Default locations are:

```text
~/.config/agent-workflow-supervisor/registry.toml
~/.config/agent-workflow-supervisor/config.toml
~/.config/agent-workflow-supervisor/projects/<project>.toml
~/.local/share/agent-workflow-supervisor/
```

`XDG_CONFIG_HOME` and `XDG_DATA_HOME` replace the corresponding base
directories. `OA_CONFIG` overrides the default project configuration path.

Registry read-modify-write transactions are cross-process locked. Writes are
atomic and use mode `0600`. The registry stores paths and profile metadata, not
OAuth tokens or API keys.

## Registry schema

The CLI manages this file. A representative registry looks like:

```toml
[accounts.default]
provider = "claude-code"

[accounts.secondary]
provider = "claude-code"
config_dir = "/private/path/to/claude-secondary"

[models.claude-sonnet]
harness = "claude-code"
model = "claude-sonnet-5"
capacity = 2

[models.local-coder]
harness = "opencode"
model = "lmstudio/my-local-coder"
provider = "lmstudio"
capacity = 1

[projects.my-project]
config_path = "/path/to/project-config.toml"
accounts = ["default", "secondary"]
models = ["claude-sonnet", "local-coder"]
default_model = "claude-sonnet"
```

Prefer `oa account`, `oa model`, and `oa project` commands over manual edits.
Manual edits are not schema-migrated and can create references to missing
profiles.

## Project TOML schema

### Supervisor

```toml
[supervisor]
database_path = ".state/checkpoints.sqlite"
runtime_dir = ".state/runtime"
poll_interval_seconds = 5
shadow_mode = true
```

- Relative database and runtime paths resolve from the project TOML directory.
- Poll intervals must be between 1 and 300 seconds.
- Shadow mode blocks external mutations but still permits reads.

### Project, runner, and tracker

```toml
[project]
id = "my-project"

[runner]
type = "ao"
command = "ao"

[tracker]
type = "github"
command = "gh"
repository = "owner/repository"
```

The configured project id must match the AO project id. The built-in tracker
accepts one `owner/repository` GitHub repository.

### Legacy harness policy

Existing installations may continue using harness routing:

```toml
[policy]
default_harness = "claude-code"
skip_labels = ["agent:skip"]
approval_labels = ["approval:required"]

[policy.capacity]
claude-code = 1

# Legacy per-harness model overrides are optional. Global model profiles are
# preferred when a model definition is shared by multiple projects.
[policy.models]
claude-code = "provider-model-id"

[[policy.routes]]
harness = "codex"
labels_any = ["agent:codex"]
```

No cross-harness role assignment is enabled in the generated configuration.
The route above only illustrates explicit label routing. Operators choose all
labels, harness roles, model overrides, and capacities.

Optionally, harnesses listed in `policy.report_only_harnesses` do not enter the
PR review and merge path. Their prompt requires a report in the work-item
discussion. The workflow marks the report complete only after observed worker
activity returns to idle, or after idle remains stable across two reconciliation
observations; this avoids terminating an asynchronously starting worker. Which
harnesses, if any, have that role is an operator setting.

Each route must define `labels_any` or `labels_all`, and exactly one destination:
`harness` or `profile`.

### Model profile routes

Global model profiles are assigned with `oa project models`. The project TOML
may add friendly route aliases:

```toml
[[policy.routes]]
profile = "local-coder"
labels_any = ["agent:local", "cost:local"]
labels_all = ["ready"]
```

The profile must be assigned to that project in the registry. The effective
`policy.default_model_profile` and `policy.model_profiles` shown by
`oa validate` are injected from the registry; do not duplicate them manually.

## Routing precedence

Routing is deterministic:

```text
1. Any policy.skip_labels match                         -> skip
2. First matching [[policy.routes]] in file order      -> route destination
3. agent:<assigned-profile-name> label                 -> that profile
4. Registry projects.<id>.default_model                -> default profile
5. policy.default_harness                              -> legacy fallback
```

Explicit ordered routes therefore take priority over the automatic namespaced
profile label. Avoid applying conflicting route labels to the same issue.

## Capacity precedence

Three limits may apply:

1. `policy.capacity.<harness>` limits active workers in one supervised project.
2. A model profile's `capacity` limits that shared profile across projects.
3. A credential profile's `max_workers` limits one isolated login across
   projects that resolve to the same `CLAUDE_CONFIG_DIR` (or the same default
   Claude login).

A worker can spawn only while every applicable limit has room. AO currently
does not report the resolved model for every listed session, so workers sharing
one harness count against a model profile's capacity even when their exact
models differ. This intentionally favors safety for local inference servers.
The final decision is repeated under a user-global allocation lock. Durable
pending and temporarily unverifiable worker reservations persist their model
and login resource identities and count as occupied. If configurations disagree
about a shared resource's limit, the lowest limit visible in its reservations
wins. Concurrent work items, separate projects, and uncertain spawn responses
therefore cannot overrun the shared resource.

An AO project's current credential binding is also stored as dynamic local
runtime state. This matters when the base AO project is both the orchestrator
project and one execution target in a credential pool: switching that AO
project to another login changes the identity of future base-project workers.
The allocation check uses the live binding rather than the profile's original
`claude_config_dir`, so the base target and a derived target pointing to the
same login share one `max_workers` budget.

## Credential profiles

Credential profiles are non-secret pointers to AO execution projects:

```toml
[credentials]
strategy = "least-active"

[credentials.profiles.claude-default]
execution_project_id = "my-project"
max_workers = 1

[credentials.profiles.claude-secondary]
execution_project_id = "my-project-claude-secondary"
max_workers = 1
claude_config_dir = "/private/path/to/claude-secondary"

[policy.credential_profiles]
claude-code = ["claude-default", "claude-secondary"]
```

The `claude_config_dir` is a path, not credential content. See
[multiple Claude logins](multiple-claude-logins.md).

`policy.credential_profiles` is the allowlist for new routing. An account-pool
update may leave older entries under `credentials.profiles`; they are retained
only so active workers in retired AO execution projects remain discoverable.
Do not delete those entries manually while such workers exist.

## Settings owned by AO and the harness

The project TOML does not configure the AO orchestrator's model, reasoning
effort, or permission mode. Those settings remain in AO's project
configuration. Harness/provider login, API endpoints, context limits, and tool
permissions also remain owned by the harness or provider.

The supervisor controls worker routing and may pass a selected worker `--model`
to AO. It does not reinterpret AO's `auto`, `accept edits`, or other permission
modes, and changing a supervisor model profile does not change the
orchestrator's model or effort. A direct `oa project bind-account` preserves the
rest of the existing AO project configuration while changing only
`CLAUDE_CONFIG_DIR`.

## Validation and effective configuration

```bash
oa validate --project my-project
```

This validates local configuration without calling a model provider. The output
is the effective merged configuration, so it is the authoritative way to see
which profile, model, route, and capacity values the supervisor will use.
