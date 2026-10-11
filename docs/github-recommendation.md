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

## Current acceptance wording

The latest operator readback in the [receipt-based status](acceptance-status.md)
reports **1.14.64 / `f61487e2` deployment acceptance failed**: the release link
switched, installer/binding exits were 0 and RPC was active, but health timed
out (`health_exit_code=1`, `TimeoutError`, final `ok=false`). The handoff denied
nightly. Current health and the underlying cause remain unverified.
Mainline 1.14.65 bounds storage/retrieval health snapshot waits and isolates
caches by runtime; it has no reported production acceptance.

The last completed **1.14.63 / `4fbcbbc9`** deployment receipt passed, and the
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
describe the latest 1.14.63 run.

Mainline **1.14.63** fixes digest identity collisions and preserves expected
record IDs and explicit error-rate counts in diagnostic quality receipts.
An isolated native regression retains the original dated target with the same
query, expected ID and gate. The operator's subsequent readback supports
aggregate smoke recovery. It supplies no per-case comparison or historical
stage trace. Missing evidence cannot create trusted labels.

Do not describe this run as business recall accepted, all colleagues verified,
L5 complete or a fully closed autonomous evolution loop. Noise ≤0.40 and
precision@3 ≥0.60 remain unchanged formal standards; the ≤0.05 diagnostic
error-rate limit also remains unchanged. The earlier measured smoke violation
has not recurred in the latest report; formal business evaluation remains incomplete.

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
