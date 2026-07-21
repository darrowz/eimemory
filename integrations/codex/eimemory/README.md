# eimemory for Codex

This plugin gives Codex an independent, authoritative long-term memory channel.
Accepted Codex records use `authority_mode=per_channel` and a deterministic
scope such as `embodied::channel::codex`; they are never recalled by OpenClaw
or Hermes unless a future explicit federation feature is enabled.

Install the `eimemory` Python package, register this repository's marketplace
with `codex plugin marketplace add <repo>/integrations/codex`, install or enable
the eimemory plugin in the desktop app, and provide these environment variables
to Codex:

```text
EIMEMORY_RPC_URL=http://honxin:8091/
EIMEMORY_RPC_TOKEN=<strong RPC bearer token>
EIMEMORY_TENANT_ID=default
EIMEMORY_AGENT_ID=hongtu
EIMEMORY_WORKSPACE_ID=embodied
EIMEMORY_USER_ID=<user identity>
```

The plugin registers `SessionStart`, `UserPromptSubmit`, `PostToolUse`, and
`Stop` hooks plus four MCP tools: recall, remember, verify outcome, and status.
Hook calls use a short timeout and are fail-open: an unavailable eimemory
service never blocks Codex. Inputs and outputs are bounded, likely secrets are
redacted, and tool payloads carry a SHA-256 digest. The unstable Codex
`transcript_path` is deliberately ignored and never read.

A successful task is not L5 evidence from prose alone. Trusted Codex task
evidence requires the explicit operator-separated attestation profile: the
host-only `PostToolUse` integration receives
`EIMEMORY_CODEX_ATTESTATION_TOKEN_FILE` and
`EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1`, while ordinary
model-launched commands receive neither. Configure Codex
`shell_environment_policy` to strip `KEY`, `SECRET`, and `TOKEN` variables
from model-launched commands. A global same-UID environment is not a secure
producer boundary and must not be represented as one.

`EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE` is a private, bounded SQLite spool
containing receipt IDs only. It is an untrusted process-boundary hint; the
runtime database independently requires the submitted set to equal the exact
pending set before atomically consuming it with terminal evidence. Without the
separated profile, memory and recall remain available but
`eimemory_status.attestation_available` is false and Codex tasks cannot count
toward L5. The `Stop` hook always returns `continue: true`.
