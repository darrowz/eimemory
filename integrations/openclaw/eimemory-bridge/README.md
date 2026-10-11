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

The operator's latest 1.14.66 / `2587331f` readback reports successful deployment
and nightly execution (`ref_12f098002823`, bound deployment `rec_f7c085d6a768`).
Formal recall remains insufficient at 0/3 cases: 343 completed reviews,
zero passes and zero open reviews. Memory retrieval cases and all nine
evolution candidate counts are zero. One new sample confirms dedupe
provenance; the nightly summary cannot identify its single off-topic
review. Mainline 1.14.67 adds bounded receipt references per reason; it
has no reported production execution or business acceptance.

OpenClaw keeps its own channel authority, without implicit federation with
Codex or Hermes. Execution success does not certify natural-query quality.
See [current status](../../../docs/acceptance-status.md),
[team acceptance](../../../docs/team-recall-acceptance.md) and
[review references](../../../docs/operations.md#nightly-review-receipt-references).
