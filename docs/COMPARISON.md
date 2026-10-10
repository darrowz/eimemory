# Choosing a memory approach

`eimemory` combines durable agent records, scoped hybrid recall and
evidence-gated learning. Choose it when you need decisions, corrections,
outcomes and replay evidence to survive across sessions, with an auditable
path from a proposed improvement to promotion or rollback.

This comparison describes architectural roles, not measured performance or a
ranking of products. Specific frameworks and database features vary by version
and configuration.

## Roles and tradeoffs

| Approach | Typical role | What eimemory adds or changes |
| --- | --- | --- |
| Conversation history | Retain messages and supply recent context | Structured operational records, scoped retrieval, outcomes and replay |
| Vector database | Index and serve similarity candidates | Record provenance, lexical/graph/quality fusion and governed learning; an optional vector backend can complement it |
| LangChain or other orchestration frameworks | Compose models, tools and memory backends | A separate memory/evidence runtime accessed through Python or RPC |
| LlamaIndex or document retrieval frameworks | Build document indexing and retrieval workflows | Agent decisions, corrections and outcomes alongside reviewed knowledge |
| In-context examples | Shape a model response through the prompt | Persistent records can select relevant examples across sessions |
| Fine-tuning | Change model weights using a training pipeline | Record- and policy-based adaptation with replay and rollback; eimemory does not train model weights |

## What the runtime provides

- Local JSONL records and SQLite projections with recovery and provenance.
- Exact tenant/agent/workspace/user scopes, source policies and separate host
  channel authority.
- Lexical, graph, quality and recency retrieval, with optional external semantic
  embeddings and optional PostgreSQL/vector maintenance.
- CLI, public Python `Runtime`, authenticated RPC, Codex, Hermes and optional
  OpenClaw integrations.
- Trusted capability catalogs, isolated evaluation, evidence-bound hypotheses,
  promotion, observation and rollback under deployment-controlled policy.

## Costs and limits

The default core has no mandatory third-party Python runtime dependencies.
Optional PDF parsing, PostgreSQL, embedding APIs, model reviews and host
integrations add installation work and may add service costs. Local-first
storage does not mean every optional feature keeps all data local: inspect the
configured external model and embedding routes before enabling them.

No universal latency, cost per session or production-quality guarantee is
claimed. Measure recall against your own accepted queries, memory size,
channel/source policy, payload budget and service configuration. A vector
service or retrieval framework may be a better fit for a workload dominated by
large-scale document search; components can also be combined.

## Acceptance before rollout

Systemd templates, healthy services and passing known-item smoke demonstrate
operational capabilities. Business recall requires trustworthy labels,
release-bound evidence, qualifying natural cases and the formal ranking/noise
gates. The latest reported 1.14.57 run has not passed that formal gate.
See [acceptance status](acceptance-status.md) and [evaluation](evaluation.md).

Start with the [Quick Start](QUICKSTART.md), review the
[architecture](architecture.md), and benchmark the actual deployment before
selecting a production rollout policy.
