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
UserPromptSubmit uses a 13-second hook ceiling to cover the optional 10-second
server recall budget plus transport/startup margin and a separately bounded
one-second acknowledgement. Other hooks retain their short timeout.
Explicit MCP recall also uses the 3.5-second transport default.
An explicit `EIMEMORY_ADAPTER_TIMEOUT_SECONDS` overrides the transport default.
Set it to `11.5` in the Codex host environment when assisted recall is enabled;
the server still enforces 3-second ordinary and 10-second assisted retrieval.
The host-provided `session_id` and `turn_id` are required; missing identifiers
are never invented to manufacture natural samples. See the
[official hook contract](https://learn.chatgpt.com/docs/hooks).

For a deliberate maintenance verification, set `EIMEMORY_ACCEPTANCE_GENERATED=1`
in that Codex process environment. The hook carries this provenance into the
raw decision; default natural collection skips it and natural labelling rejects
it. Do not set this flag globally for ordinary user sessions. Selected evidence
fragments are rendered verbatim and revalidated on retries; raw stage diagnostics
retain safe failure codes without copying memory or prompt text.

Proactive recall and feedback default to the `codex` source, matching retained
Codex memories. `EIMEMORY_SOURCE_IDS` remains an explicit operator override; it
does not bypass the server's source allowlist or channel scope.

Hook calls are bounded and are fail-open: an unavailable eimemory
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
