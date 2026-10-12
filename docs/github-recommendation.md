# Public project descriptions

Use these descriptions consistently for the repository homepage, package
metadata, integration listings and external introductions. This file supplies
copy; it does not claim that GitHub About, topics, profile pins or social-preview
settings were changed. The GitHub About write returned `Forbidden`; repository
documentation can still be updated. Historical release notes keep their original scope.

## One-line description

```text
Local-first memory and evidence-gated learning for long-running AI agents.
```

中文简介：

```text
面向长期运行 AI 智能体的本地优先记忆与证据门控学习运行时，提供持久化上下文、分通道召回、评测、晋升和回滚。
```

## Short introduction

`eimemory` stores decisions, preferences, corrections, incidents, outcomes,
reviewed knowledge and capability evidence across agent sessions. It combines
scoped hybrid recall with hypotheses, trusted evaluation catalogs, replay,
promotion and rollback. Python, CLI and authenticated RPC surfaces connect
Codex, Hermes Agent, optional OpenClaw and custom agents.

Mainline 1.14.69 adds controlled recall baselines from existing authorized
records, digest-bound canonical knowledge applicability review and an official
Hermes v2 route for governed maintenance of known capability gaps. Registered
bounds, independent suites, machine policy and transaction receipts determine
whether effects can proceed. These paths do not claim natural production labels,
autonomous discovery or an already completed production evolution loop.

Version 1.14.70 makes knowledge inventory review resumable within bounded work
per run and keeps uniqueness blocked until the complete corpus is checked.
Safe provider failure categories and controlled retrieval-stage metadata improve
diagnosis without changing recall ranking or business thresholds. The latest
reported 1.14.72 deployment and nightly execution passed; formal recall remains
0/3 and unaccepted, and no successful evolution transaction is supplied.

Version 1.14.71 repairs admission of mixed statements/questions, formatted role
boundaries, authoritative source material and source-time ordering for durable
memory extraction. Bounded extraction observations retain references and digests
without publishing conversation text or identities. These repairs do not certify
every assistant/model output as true, reconstruct historical extraction stages or
establish recovery in a participant's unbound production query.

Version 1.14.72 adds a private native trace of one controlled target through
actual retrieval stages, including measured target rejection reasons and
explicit unknowns. Query-generation metadata contains only field, length and
truncation. It adds no search, record read or candidate and leaves the original
query, expectation, selector, grounding and thresholds unchanged. The latest
1.14.72 production trace confirms the failed target was absent from the complete
14-candidate source-returned window, before the selector. It does not identify
the upstream cause, establish recovery or complete formal acceptance.

Mainline 1.14.73 aligns controlled positive targets with existing recall source
policy, excluding capture-rejected/raw/default evidence while retaining
eligible L1 episodic memories. Explicit skips and existing-case revalidation
prevent an ineligible or empty dataset from passing; historical failures are not
rewritten. Independent bounded SQLite source observations preserve the actual
matching/scoring/cap windows and target branches without extra SQL, lookups or
selection changes. Window absence does not prove index absence or predicate
rejection. No 1.14.73 production result or recovered miss is claimed.

The core has no mandatory third-party Python runtime dependencies. Semantic
embeddings and model review are optional external services with separate
configuration, costs and data boundaries. Commit and production deployment
require explicit deployment-controlled machine policy and verification.

## GitHub About copy

Description: use the one-line description above.

Homepage, when a repository documentation link is desired:

```text
https://github.com/darrowz/eimemory#readme
```

Suggested topics:

```text
agent-memory
ai-agent
autonomous-agents
local-first
long-term-memory
rag
replay-evaluation
self-improving-ai
codex
hermes-agent
openclaw
```

The existing hero is `docs/assets/eimemory-github-hero.png`. It illustrates the
architecture; it is not an acceptance badge. Repository settings, profile pins
and a social-preview upload are independent GitHub actions.

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

Mainline 1.14.73 applies the existing recall source policy to controlled positive
targets: capture-rejected records and raw/default evidence-only material
stay excluded, while eligible L1 episodic memories remain supported. Skips and
old-case revalidation remain explicit; cases are not selected by retrieval failure
or grounding, and an empty eligible dataset cannot pass. Bounded native SQLite
source observations add no target lookup, search/read/model call or candidate
injection and do not change query, ordering, limits or quality thresholds.
Absence in an observed SQL window does not prove index absence or rejection by
a particular predicate. Production 1.14.73 acceptance and miss recovery are unverified.

## Current acceptance wording

An earlier operator readback in the [receipt-based status](acceptance-status.md)
reports **1.14.64 / `f61487e2` deployment acceptance failed**: the release link
switched, installer/binding exits were 0 and RPC was active, but health timed
out (`health_exit_code=1`, `TimeoutError`, final `ok=false`). The handoff denied
nightly. At that failed attempt, health and its underlying cause were unverified.
Mainline 1.14.65 bounds storage/retrieval health snapshot waits and isolates
caches by runtime; it has no reported production acceptance.

The historical **1.14.63 / `4fbcbbc9`** deployment receipt passed, and the
October 11 nightly exited 0 with no failed steps (`ref_840ade0e6fbf`).
Quality report `ref_6ae80df64e32` records smoke 10/10, error rate 0.00 and
hit@1 / hit@5 both 1.00. Formal recall remains unaccepted at 0/15;
321 automatic reviews produced zero passes and zero pending conclusions.
The memory benchmark has no retrieval cases, and all nine evolution results
have zero candidate hypotheses. The hypothesis producer lacks an applicable
knowledge link for the gap revision. The formal gate remains false with
`recall_quality_evidence_incomplete`.

Team-level recall in actual Feishu conversations remains unverified. Earlier
empty controlled results lacked complete session identity and environment
verification; they do not establish current participant faults. See
[team recall acceptance](team-recall-acceptance.md).

Mainline 1.14.64 policy v5 requires **3 cases and 3 trusted labels** in total,
as explicitly requested by the operator, with no fixed
per-client quota. A Hermes-only dataset can meet coverage prerequisites;
accepted coverage remains limited to the observed dataset hosts. Team actual-use
acceptance and cross-client product claims require separate scope and evidence.
The reported deployed 1.14.63 result retains its original v4 floor and 0/15
count; no 1.14.64 production acceptance is reported.

The earlier 1.14.62 smoke passed 9/10 and violated the ≤0.05 error-rate limit.
That release's classification repair preserved capture boundary invalid (10),
matching decision missing (186) and semantic items not delivered (**6**) directly.
The third count was **5** in 1.14.61. These historical review counts do not
describe the historical 1.14.63 run.

Mainline **1.14.63** fixes digest identity collisions and preserves expected
record IDs and explicit error-rate counts in diagnostic quality receipts.
An isolated native regression retains the original dated target with the same
query, expected ID and gate. The operator's subsequent readback supports
aggregate smoke recovery. It supplies no per-case comparison or historical
stage trace. Missing evidence cannot create trusted labels.

Do not describe this run as business recall accepted, all colleagues verified,
L5 complete or a fully closed autonomous evolution loop. Noise ≤0.40 and
precision@3 ≥0.60 remain unchanged formal standards; the ≤0.05 diagnostic
error-rate limit also remains unchanged. The historical 1.14.63 report showed
smoke recovery. The latest 1.14.72 readback reports seven of eight controlled
checks passing and formal recall still 0/3, unaccepted. The failed target was
absent from the complete source-returned window of 14 candidates, before the
selector; the upstream cause remains unknown. It supplies no natural-production
accuracy score or team acceptance and does not justify weakening the selector.

Mainline 1.14.66 adds text-free session-deduplication provenance, linking each
recorded removed candidate to a qualifying historical decision/item and its
observed state. The four earlier count-only decisions remain individually
unverified; their associated empty-case review is completed without a label.
This diagnostic change establishes no new production or team acceptance.
See [the readback](acceptance-status.md#session-deduplication-readback).

## Longer introduction

```markdown
`eimemory` is a local-first memory and evidence-gated learning runtime for
long-running AI agents. It preserves durable context, retrieves it within exact
owner/source/channel boundaries, and retains provenance for knowledge and
outcomes. Scheduled analysis can propose learning goals and candidate changes;
trusted evaluation, replay, machine policy, observation and rollback govern
whether they advance.

The runtime connects through Python, CLI, authenticated RPC and adapters for
Codex, Hermes Agent and optional OpenClaw. Deployment success, smoke checks,
memory benchmarks and business acceptance are reported separately. See the
repository's current receipt-based acceptance status before making production
quality or capability-maturity claims.

Repository: https://github.com/darrowz/eimemory
```
