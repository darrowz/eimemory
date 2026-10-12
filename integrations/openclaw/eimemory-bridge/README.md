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

The operator reports accepted deployment on 1.14.69 / `1729dfd2`.
Nightly `ref_0b76c0408ad1` ran 21:23:41–21:32:59 and exited 1; its only failed
step was `research_closure_review`, with
`command_completion_failed:provider_request_failed`. The underlying provider
failure is not identified as network, configuration or quota trouble.

Controlled memory checks ran 10 cases: nine passed and one known item was
missing with zero returned records. Collection created four cases and found
156 existing cases; all four new cases had empty candidates and completed
non-certifying reviews. Formal recall remains unaccepted at 0/3.
Evolution has no candidates; `knowledge_inventory_truncated` blocks knowledge
applicability, so waiting for more data alone cannot explain or resolve it.

Mainline 1.14.71 repairs durable-memory input filtering, role parsing,
authoritative source binding and event-time ordering, with reference-only
extraction diagnostics. A participant's small 1.14.69 Hermes readback reported
retained conversation sources but two empty memory queries; missing exact
scope/source/stage evidence prevents assigning that result to a particular bug.
These source repairs do not certify production recovery or team recall.

OpenClaw keeps its own channel authority, without implicit federation with
Codex or Hermes. Execution success does not certify natural-query quality.
See [current status](../../../docs/acceptance-status.md),
[team acceptance](../../../docs/team-recall-acceptance.md) and
[review references](../../../docs/operations.md#nightly-review-receipt-references).
