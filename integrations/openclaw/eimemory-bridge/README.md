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

The operator reports accepted deployment on **1.14.71 / `e598397b`**.
Deployment receipt `rec_e56a7b8df209` records successful installation, binding
refresh and health checks. Nightly `ref_b23ca097a83f` ran 01:06:17–01:15:06
on October 13, 2026 (Asia/Shanghai) and exited 0 with no failed steps. The earlier
research-review execution failure did not recur in this run; its absence does
not establish the earlier provider failure's cause.

Controlled memory checks passed **8 of 9** cases. The failed known-item case
returned no records: its source was available, no fallback occurred, and the
selector rejected all 14 inputs with `grounding` reasons. Whether the expected
record was among those inputs is unknown, so this is not proof of its loss stage.
Collection created four cases and found 160 existing cases, with no collection
skips; all four new cases had empty candidates. Reviews total 360, with zero
pending/open reviews and **0/3 formally accepted cases**. Formal recall remains
unaccepted. Nine evolution results each have zero candidate hypotheses; an
applicable knowledge link for the current gap revision is still missing.

Version 1.14.71 repairs durable-memory input filtering, role parsing,
authoritative source binding and ordering of already related states, with
reference-only extraction diagnostics. A participant's small 1.14.69 Hermes
readback reported retained sources but two empty memory queries. Their exact
scope/source/stage binding and recovery remain unverified. Successful deployment
and nightly execution do not certify these queries, model factual truth or team
recall quality.

Mainline 1.14.72 adds a private, single-target native trace for subsequent
controlled checks. It records actual stage presence and target-specific drops;
missing observations remain unknown. The original query, case ID, expected
record, selector, grounding and thresholds stay unchanged, with no extra search,
record read or candidate injection. Query-generation metadata contains only
field, length and truncation. This does not backfill the 1.14.71 target's unknown
membership or establish a new deployment, production query type or recovery.

OpenClaw keeps its own channel authority, without implicit federation with
Codex or Hermes. Execution success does not certify natural-query quality.
See [current status](../../../docs/acceptance-status.md),
[team acceptance](../../../docs/team-recall-acceptance.md) and
[review references](../../../docs/operations.md#nightly-review-receipt-references).
