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
covers **1.14.60 / `40285e36`**: deployment receipt acceptance passed, and one
additional nightly completed with exit 0 (`ref_3287829a4ae2`). Formal recall
remains unaccepted at 0/15, with five pending reviews; automatic review is enabled
but reports unavailable semantic transport and insufficient independent signals.
Memory benchmarking did not run, and nine capability-evolution results await
unambiguous hypotheses. The earlier 1.14.57 known-item smoke passed 10/10;
the intervening 1.14.58 research-review execution failure is historical.

Mainline **1.14.61** records completed pass/fail automatic review conclusions,
shares Hermes discovery with the tool-free semantic judge, and preserves bounded
business-evaluation diagnostics. Missing evidence cannot create trusted labels.
No production deployment or formal business acceptance is reported for this patch.

Do not describe this run as business recall accepted, all colleagues verified,
L5 complete or a fully closed autonomous evolution loop. Noise ≤0.40 and
precision@3 ≥0.60 remain unchanged formal standards; the smoke observations are
not a completed business evaluation.

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
