# eimemory Hermes hook bridge

This plugin registers official Hermes host callbacks (`pre_gateway_dispatch`,
`pre_llm_call`, `post_llm_call`) and synchronous `tool_execution` middleware.
Older hosts without middleware use `post_tool_call` instead. It resolves the exact
MemoryManager-owned `eimemory` provider through the packaged, session-scoped
registry.

Execution middleware observes the actual result before returning it unchanged;
it avoids Hermes' suppression of overlapping `post_tool_call` callbacks. No
background queue or model-supplied receipt is used. When another execution
middleware can rewrite the invocation, or the execution chain cannot be inspected,
attestation fails closed. The legacy hook path still inherits the old host's
observer concurrency limitations.

`eimemory_status`, recall, and remember are not verification evidence. To close a
verified turn, run an actual supported verification command (for example
`python -B -m pytest -p no:cacheprovider tests/test_hermes_plugin_package.py -q`)
through the host terminal tool, then invoke `eimemory_verify_outcome` in the same
session. No eligible receipt or multiple unfinalized turns must remain a refusal.
The deployment verifier is a real subprocess/official-loader replay, not proof
that an already-running gateway has reloaded the plugin. Verify memory write/readback
and a fresh gateway turn separately after deployment and gateway restart.

`pre_gateway_dispatch` binds a genuine external inbound message to Hermes'
durable delivery obligation. Evidence is emitted only after Hermes records the
response as successfully delivered; local, replay, webhook, bot, and unbound
events are ignored.

Install this folder under `$HERMES_HOME/plugins` together with the existing
`eimemory` provider plugin so closed-loop evidence can be produced on real host
tool calls:

```text
$HERMES_HOME/plugins/eimemory
$HERMES_HOME/plugins/eimemory_hook
```

Enable only the general hook plugin; the provider is selected independently:

```bash
hermes config set memory.provider eimemory
hermes plugins enable eimemory-hook
```

Requirements for attested tool receipts:

1. `EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE` points to a non-empty private token file.
2. `EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1`.
3. Runtime and producer credentials are different.
4. `EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE` is shared by the hook and provider.

When these are not set, memory reads/writes keep working and attestation is
reported as unavailable.

## Validation boundary

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

Hook registration and execution success do not certify natural-query quality
or capability maturity. See [receipt-based status](../../../docs/acceptance-status.md),
[evaluation](../../../docs/evaluation.md) and [team acceptance](../../../docs/team-recall-acceptance.md).
For repeated questions, new 1.14.66 decisions retain removed candidate IDs and
qualifying historical references; existing count-only decisions cannot be backfilled.
