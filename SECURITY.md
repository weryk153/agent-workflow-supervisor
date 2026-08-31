# Security policy

## Supported versions

Until the first stable release, security fixes target the latest release and
the default development branch. Older pre-1.0 versions may require upgrading.

## Reporting a vulnerability

Use the repository host's private vulnerability-reporting feature when it is
available. Do not post tokens, provider credentials, private repository URLs,
local configuration files, or exploit details in a public issue.

Include:

- affected version or commit
- operating system and adapter versions
- minimal reproduction with secrets removed
- impact and whether external mutation was possible
- suggested mitigation, if known

If no private reporting channel is configured, contact the maintainer through
a private channel before opening a public issue. A public issue may describe
that a security report exists without including sensitive details.

## Security boundaries

The supervisor coordinates tools that may modify repositories and merge pull
requests. Operators are responsible for:

- using least-privilege GitHub and model-provider credentials
- reviewing `approval_labels` and shadow mode before apply operation
- protecting local AO, OpenCode, Claude, and provider configuration directories
- restricting unauthenticated local inference endpoints to loopback
- securing the host account and filesystem containing checkpoints and worktrees
- reviewing third-party harness and model licenses and updates

The project does not store provider tokens intentionally. It does store
non-secret account names, model identifiers, project ids, filesystem paths, AO
session ids, issue content, and workflow state. Treat checkpoint and runtime
directories as private operational data even when they contain no API key.

## Out of scope

- Vulnerabilities in AO, GitHub CLI, model harnesses, local model servers, or
  model weights should also be reported to their respective maintainers.
- Model quality, hallucination, prompt injection, and unsafe code generation
  remain operational risks unless they demonstrate a defect in this project's
  documented safety gates.
