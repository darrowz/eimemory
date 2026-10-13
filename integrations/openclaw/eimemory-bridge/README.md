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

The operator reports accepted deployment on **1.14.72 / `612dc458`**.
Deployment receipt `rec_fba7350aa0b4` records successful installation, binding
refresh and health checks, with active RPC and matching current/target releases.
Nightly `ref_d6eb0f6e9c6f` ran 02:15:05–02:23:40 on October 13, 2026
(Asia/Shanghai) and exited 0 with no failed steps.

Controlled memory checks passed **7 of 8** cases. The failed known-item case
returned no final records. Its completed native trace shows an available SQLite
source without fallback: the expected target was absent from the complete,
untruncated source-returned window of 14 candidates and subsequent candidate
stages. This excludes rejection of that target by the selector in this run.
It does not establish an absent index entry; query generation, source matching
and source-internal filtering remain distinct possible causes.
Collection created five cases and found 164 existing cases, with no collection
skips; all five new cases had empty candidates. Reviews total 365, with zero
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

Version 1.14.72 adds a private, single-target native trace, now observed in the
supplied controlled readback. It records actual stage presence and target-specific
drops; missing observations remain unknown. The original query, case ID, expected
record, selector, grounding and thresholds stay unchanged, with no extra search,
record read or candidate injection. Query-generation metadata contains only
field, length and truncation. The new trace does not backfill 1.14.71 membership,
determine a historical production query type or certify recovery or formal quality.

Mainline 1.14.74 repairs the October 13 business-closure audit: trusted host
identity, recoverable Hermes capture and delivery evidence, formal acceptance
summaries, negative capability evidence and protected patch feedback, complete
archived-payload backups, and idempotent knowledge/persona processing. Review
coverage is resumable, Chinese query features retain privacy checks, and ordinary
memory evaluation follows the configured runtime. Long-query term selection now
covers the full question within existing bounds; ambiguous identity windows do
not certify uniqueness. Grounding, label trust, release authority and the v5
three-case floor remain required. These source repairs do not certify a production
deployment, recovery of the older controlled miss, or team-wide recall quality.

OpenClaw keeps its own channel authority, without implicit federation with
Codex or Hermes. Execution success does not certify natural-query quality.
See [current status](../../../docs/acceptance-status.md),
[team acceptance](../../../docs/team-recall-acceptance.md) and
[review references](../../../docs/operations.md#nightly-review-receipt-references).
