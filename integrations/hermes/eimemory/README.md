# eimemory for Hermes Agent

This standalone plugin implements Hermes Agent's native `MemoryProvider`
contract. It does not patch Hermes core. Install this directory together with
`integrations/hermes/eimemory_hook`. The production installer creates
release-bound links under `$HERMES_HOME/plugins`; for a manual installation,
copy the provider to `$HERMES_HOME/plugins/eimemory`, copy the hook to
`$HERMES_HOME/plugins/eimemory_hook`, and install the matching `eimemory` Python package in
the Hermes environment. Select the provider in `config.yaml`:

```yaml
memory:
  provider: eimemory
plugins:
  enabled:
    - eimemory-hook
```

Configure the authenticated runtime in the Hermes profile environment:

```text
EIMEMORY_RPC_URL=http://<rpc-host>:8091/
EIMEMORY_RPC_TOKEN=<strong RPC bearer token>
EIMEMORY_TENANT_ID=default
EIMEMORY_AGENT_ID=<agent-id>
EIMEMORY_WORKSPACE_ID=<workspace-id>
EIMEMORY_USER_ID=<user identity>
```

Hermes memory is authoritative only inside `authority_mode=per_channel` and a
scope such as `<workspace-id>::channel::hermes`. OpenClaw and Codex cannot recall it.
The provider supports prefetch, bounded background turn sync, built-in memory
mirroring, pre-compression context, session lifecycle, session switching, and
four explicit tools for recall, remember, verified outcome, and status.

Calls are fail-open with short timeouts, a single bounded writer, a single
prefetch worker, bounded cache, and a bounded failure ledger. Service failure
never blocks Hermes. The provider deliberately ignores the full conversation history
supplied to session/compression hooks; only bounded completed turns are
sent. A session end is lifecycle evidence only. An explicit verification
string is diagnostic, not trusted L5 proof.

Trusted Hermes task evidence is fail-closed behind the operator-separated
host profile. The host plugin receives a private
`EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE` plus
`EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1`, loads the token
once, and removes producer configuration from the inherited environment before
ordinary child tools can run. `EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE` stores
only bounded receipt IDs; the protected runtime database remains authoritative
for the exact pending set and atomic terminal claim. Without this separated
profile, memory and recall keep working, `attestation_available` is false, and
Hermes tasks do not count toward L5.

The packaged session registry binds official host callbacks to the exact
MemoryManager-owned provider instance without forcing one global provider
across concurrent gateway sessions.

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
