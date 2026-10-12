# Public project descriptions

Use these descriptions consistently for the repository homepage, package
metadata, integration listings and external introductions. This file supplies
copy; it does not claim that GitHub About, topics, profile pins or social-preview
settings were changed. Historical release notes keep their original scope.

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
reported 1.14.69 deployment passed, but nightly and formal business acceptance
did not; no newer production recovery is claimed.

Version 1.14.71 repairs admission of mixed statements/questions, formatted role
boundaries, authoritative source material and source-time ordering for durable
memory extraction. Bounded extraction observations retain references and digests
without publishing conversation text or identities. These repairs do not certify
every assistant/model output as true, reconstruct historical extraction stages or
establish recovery in a participant's unbound production query.

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
smoke recovery. The latest 1.14.69 readback reports nine of ten controlled
memory checks passing and formal recall still 0/3, unaccepted. It does not
provide a new natural-production accuracy score or team acceptance.

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
