# Model profiles

Model profiles keep reusable runner choices outside project repositories. A
profile contains only a name, AO harness, model id, optional provider hint, and
a conservative capacity limit. It never contains API keys, OAuth tokens, or
provider credentials.

A model profile does not configure the harness or provider. It names a model
that the selected harness must already be able to use.

## Ownership

```text
registry.toml
  models.<name>                 global model definition
  projects.<id>.models          allowed profiles for one project
  projects.<id>.default_model   fallback profile for that project

project TOML
  policy.routes                 optional label rules referencing profiles

AO
  receives --harness and --model for each spawned worker
```

The registry is normally stored at
`~/.config/agent-workflow-supervisor/registry.toml`. Project repositories do
not need an OA file, an AO file, or local-provider credentials.

## Register and assign

```bash
oa model add claude-sonnet \
  --harness claude-code \
  --model claude-sonnet-5 \
  --capacity 2

oa model add local-qwen \
  --harness opencode \
  --model lmstudio/my-local-coder \
  --provider lmstudio \
  --capacity 1

oa model list

oa project models my-project \
  --set claude-sonnet,local-qwen \
  --default claude-sonnet
```

Changing a project assignment restarts that project's supervisor only when it
was already running. AO itself remains the execution plane and does not need a
second manually maintained model mapping. If the project uses pooled Claude
accounts, the command also reapplies the effective default Claude model to each
derived AO execution project. Replacing an assigned global profile performs the
same reconciliation for every affected project. A failure restores the prior
registry state before a stopped supervisor is restarted.

## Route work

Every assigned profile automatically accepts its namespaced issue label. For
the example above, `agent:local-qwen` selects the local model. Unmatched work
uses `claude-sonnet`.

Additional labels can be declared in the project supervisor TOML:

```toml
[[policy.routes]]
profile = "local-qwen"
labels_any = ["agent:local", "cost:local"]
```

Legacy `harness = "codex"` routes and `[policy.models]` remain supported, so
existing installations do not need an immediate migration.

Complete precedence is documented in [configuration](configuration.md).

## OpenCode provider setup

OpenCode accepts local OpenAI-compatible providers. Provider configuration is
owned by OpenCode and is normally stored in
`~/.config/opencode/opencode.json`. The profile's `model` value must exactly
match a model header printed by `opencode models <provider>`.

The examples below follow the current OpenCode `provider` schema. Consult the
[OpenCode provider documentation](https://opencode.ai/docs/providers/) and its
installed JSON schema if a later major version changes the format.

### LM Studio

List downloaded models, start the API server, and load one model under a stable
API identifier:

```bash
lms ls
lms server start
lms load <model-key> \
  --identifier my-local-coder \
  --context-length 16384 \
  --gpu max

curl http://127.0.0.1:1234/v1/models
```

The identifier, not the display name or filesystem folder, is the model id sent
to the API. LM Studio documents `lms load` and the OpenAI-compatible
[`/v1/models`](https://lmstudio.ai/docs/developer/openai-compat/models)
endpoint in its official documentation.

Add the provider to OpenCode without embedding a credential:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "lmstudio": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LM Studio (local)",
      "options": {
        "baseURL": "http://127.0.0.1:1234/v1"
      },
      "models": {
        "my-local-coder": {
          "name": "My local coder",
          "limit": {
            "context": 16384,
            "output": 2048
          }
        }
      }
    }
  }
}
```

Verify OpenCode before involving AO:

```bash
opencode models lmstudio
opencode run \
  --model lmstudio/my-local-coder \
  "Reply with exactly LOCAL_MODEL_OK"
```

Register and assign the same provider/model string:

```bash
oa model add local-coder \
  --harness opencode \
  --model lmstudio/my-local-coder \
  --provider lmstudio \
  --capacity 1

oa project models my-project \
  --set local-coder \
  --default local-coder

oa model doctor local-coder --project my-project
```

The LM Studio doctor checks the server at `127.0.0.1:1234`, the served model
identifier, the OpenCode executable, and OpenCode model visibility.

### Ollama

Install a model explicitly and confirm its exact id:

```bash
ollama pull <model>
ollama list
ollama show <model>
```

Configure OpenCode:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (local)",
      "options": {
        "baseURL": "http://127.0.0.1:11434/v1"
      },
      "models": {
        "<model>": {
          "name": "Local Ollama model"
        }
      }
    }
  }
}
```

Then register `--model ollama/<model> --provider ollama`. The Ollama doctor
checks the CLI, `ollama show`, OpenCode, and OpenCode model visibility. The
supervisor never pulls weights automatically.

## End-to-end smoke test

Use a disposable issue or AO project before making a local model the default
for production work. A minimum test should prove:

1. The provider's `/v1/models` endpoint exposes the expected id.
2. `opencode models <provider>` prints the exact `provider/model` string.
3. `opencode run --model provider/model` returns a response.
4. `oa model doctor` reports every check ready.
5. An AO `opencode` worker can receive and answer a prompt.
6. A coding test exercises at least one read and one edit tool before relying
   on the model for unattended work.

The LM Studio → OpenCode → AO path was smoke-tested with an MLX Qwen2.5 Coder
7B model on 2026-08-31. A simple exact-response prompt succeeded through both
OpenCode directly and an AO chat-mode worker. This proves the integration path,
not the model's suitability for complex autonomous changes.

## Readiness and licenses

```bash
oa model doctor local-coder --project my-project
```

The doctor is read-only. It always checks AO harness support and installation.
For OpenCode profiles it also checks the executable and model visibility.
Recognized `ollama` and `lmstudio` provider hints add provider-specific server
or model checks. It does not download weights or edit OpenCode, model servers,
AO, or project configuration.

The supervisor is Apache-2.0, but model weights and inference providers have
independent licenses. Operators are responsible for selecting models whose
licenses fit their use and redistribution requirements.

Profile capacity is intentionally conservative. AO's session catalog exposes
the harness but not every harness's resolved model, so active workers sharing
the same harness count against the selected profile limit. This prevents a
local inference backend from being overcommitted. The profile name and limit
are stored in each durable acquisition reservation and enforced under a
user-global lock, so assigning the same profile to several projects does not
multiply its capacity.

## Security, privacy, and model quality

- Local weights are not automatically equivalent to open-source software;
  inspect the model card and weight license.
- A local inference endpoint may be unauthenticated. Bind it to loopback unless
  remote access is intentional and secured.
- OpenCode can still use configured MCP servers, web tools, or remote services.
  Local inference alone does not guarantee a fully offline workflow.
- Small local models may answer prompts but fail structured tool calling,
  long-context reasoning, or safe edits. Validate on representative tasks.
- Keep profile capacity low until memory use and concurrent inference have been
  measured on the deployment machine.
