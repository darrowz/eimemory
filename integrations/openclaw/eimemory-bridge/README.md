# eimemory for OpenClaw

This optional external plugin connects eight OpenClaw lifecycle hooks to
scoped eimemory memory, recall and outcome evidence. OpenClaw is not a core
runtime dependency. The bridge uses the public authenticated Gateway SDK and
an explicit external plugin path; it does not impersonate an upstream bundled
plugin or patch OpenClaw core.

## Install and verify

Use the [immutable deployment guide](../../../docs/deployment.md) and
[systemd adapter instructions](../../../deploy/systemd/README.md). The installer
accepts `EIMEMORY_OPENCLAW_ADAPTER=auto|enabled|disabled`:

- `auto` enables the adapter only when an installation, configuration or service
  is detected.
- `enabled` requires a complete working integration and its deployment checks.
- `disabled` omits OpenClaw and does not certify that channel.

The package declares OpenClaw compatibility `>=2026.7.1`. Load the selected
immutable release through `plugins.load.paths` and supply runtime credentials
outside tracked files. Restart/reload the host through the documented deployment
flow and verify actual scoped write/readback and a fresh host turn.

## Hook surface

`message_received`, `before_prompt_build`, `agent_end`, `message_sent`,
`session_end`, `before_agent_finalize`, `before_tool_call`, `after_tool_call`.
The plugin also exposes `eimemory_bridge_status`.

Host availability and trusted evidence have different boundaries. A status or
health check is not a verified task outcome. Delivery probes may send a real
platform reply; configure and run them with the appropriate operator authority.
See [adapter operations](../../../docs/operations.md).

## Acceptance boundary

OpenClaw retains its own channel authority; there is no implicit federation
with Codex or Hermes. Plugin loading and known-item recall do not establish
natural-query quality. Formal recall acceptance requires accepted cases,
trustworthy labels and release-bound evidence. The latest 1.14.64 / `f61487e2`
deployment attempt switched the release link but failed health acceptance
(`TimeoutError`, `ok=false`); nightly did not start. Current health remains
unverified. The last completed 1.14.63 /
`4fbcbbc9` deployment and nightly passed; smoke passed 10/10 with error rate
0.00. The formal gate remains false because qualifying evidence is incomplete; see
[current status](../../../docs/acceptance-status.md).
Individual conversation identities and team recall remain subject to the
[team acceptance checks](../../../docs/team-recall-acceptance.md).
Since 1.14.64, policy v5 uses three cases and three trusted labels within the
existing dataset scope; label authority, release authorization and quality
checks remain required. That completed nightly's evidence remains bound to
1.14.63. Mainline 1.14.65 bounds health snapshot waits and isolates caches by
runtime; it has no reported production acceptance.

Since 1.14.66, new proactive decisions retain removed candidate IDs and exact
qualifying historical decision/item references with the observed state.
Prior injection, host feedback and in-flight reservations are distinguished;
these diagnostics do not certify a new delivery or a correct no-answer.
Current evaluation candidates and quality gates retain their existing rules.
See [session deduplication provenance](../../../docs/operations.md#session-deduplication-provenance).
