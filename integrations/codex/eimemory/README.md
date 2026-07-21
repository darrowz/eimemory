# eimemory for Codex

This plugin gives Codex an independent, authoritative long-term memory channel.
Accepted Codex records use `authority_mode=per_channel` and a deterministic
scope such as `embodied::channel::codex`; they are never recalled by OpenClaw
or Hermes unless a future explicit federation feature is enabled.

Install the `eimemory` Python package, install or enable this Codex plugin, and
provide these environment variables to Codex:

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

A successful task is not L5 evidence until `eimemory_verify_outcome` supplies
an explicit verification string. The `Stop` hook records lifecycle truth but
does not invent verification and always returns `continue: true`.
