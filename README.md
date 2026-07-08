# eimemory

![eimemory memory infrastructure hero](docs/assets/eimemory-github-hero.png)

`eimemory` is a local-first memory and learning runtime for long-running AI
agents.

It gives an agent a durable way to remember what happened, recall the right
context, evaluate whether memory helped, and turn repeated failures into gated
improvements. The project is built for OpenClaw and the EI stack, but the core
runtime is a standalone Python package with CLI, Python APIs, HTTP/RPC surfaces,
and local storage.

## Why It Exists

Most agent systems keep conversation history, but lose operational experience:

- what the operator corrected
- which task failed and why
- which rule helped in a real run
- which memory should be recalled for a specific scope
- whether a proposed improvement passed replay, health, and rollback checks

`eimemory` treats those signals as records instead of chat residue. It stores
them locally, indexes them for retrieval, evaluates their usefulness, and feeds
bounded learning loops without giving memory itself unlimited execution
authority.

## What It Provides

- Local record store backed by JSONL and SQLite.
- Hybrid recall over lexical, semantic, graph, quality, and recency signals.
- Scoped memory for users, workspaces, projects, channels, and agents.
- Knowledge intake for papers, URLs, claims, entities, relations, and compiled
  recall views.
- Event memory for task intent, execution paths, outcomes, corrections, and
  incidents.
- Replay and evaluation tools for recall quality, living memory, regression
  cases, and task outcomes.
- Governance loops for learning goals, candidate improvements, capability
  scores, rollout gates, canaries, and rollback evidence.
- OpenClaw hooks and eibrain RPC integration for production agent runtimes.

## Core Model

`eimemory` separates memory from authority.

```text
events -> records -> indexes -> recall -> evaluation -> learning goals
                                      -> candidates -> gates -> ledger
```

The runtime can observe, store, retrieve, score, propose, and audit. High-risk
actions such as external sends, spending, credential changes, irreversible
deletion, private data export, or production mutation stay outside automatic
memory authority unless a deployment adapter and policy gate explicitly allow
them.

## Main Surfaces

### Memory

The memory layer stores structured records and returns task-relevant context
with metadata. It is designed to avoid dumping the whole memory file into an
agent prompt.

```bash
eimemory ingest "Remember concise replies" --title "Concise reply style"
eimemory recall "how should this agent reply?"
eimemory quality stats
```

### Knowledge

The knowledge layer turns external sources into claims, entities, relations,
pages, and recall views. This is useful for research agents that need evidence
instead of loose summaries.

```bash
eimemory paper ingest --url https://example.com/paper --title "Example paper"
eimemory intake run --source-kind paper --limit 10
eimemory source scan
```

### Experience

The experience layer records outcomes, corrections, and incidents so repeated
operator feedback can become regression data instead of disappearing into chat
history.

```bash
eimemory reflect log reply-style "Missed concise style" "Use one direct sentence"
eimemory reflect check
```

### Governance

The governance layer runs bounded self-improvement loops. It can identify weak
capabilities, build replay datasets, create candidate improvements, score them,
and record rollout state in a capability ledger.

```bash
eimemory learn cycle --dry-run
eimemory learn ledger --limit 50
eimemory learn dashboard --persist
```

### Production Health

The service exposes compact health output for repeatable deployment checks.

```bash
curl http://127.0.0.1:8091/health
eimemory doctor --json
```

## Safety Posture

`eimemory` uses tiered authority:

| Tier | Scope |
| --- | --- |
| `L0` | Records, reports, replay cases, scores, dashboards. |
| `L1` | Local low-risk assets such as memory rules, route drafts, playbooks, and eval fixtures. |
| `L2` | Gated rollout through explicit adapters after evidence, eval, health, canary, timeout, audit, and rollback checks. |
| `L3` | External sends, spending, auth changes, private data export, device actions, irreversible deletion, or privilege expansion. Blocked by default. |

This lets the system learn from real work while keeping dangerous authority in
the surrounding runtime or release process.

## Quick Start

```bash
python -m pip install -e .
eimemory init
eimemory ingest "Remember concise replies" --title "Concise"
eimemory recall "concise replies"
eimemory learn cycle --dry-run
```

For a local RPC service:

```bash
eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
curl http://127.0.0.1:8091/health
```

## Deployment Shape

The production deployment uses immutable releases and a user systemd service:

```text
/opt/eimemory/releases/<commit>
/opt/eimemory/current -> /opt/eimemory/releases/<commit>
eimemory-rpc.service
eimemory-nightly.timer
```

`eimemory-nightly.timer` is the single default production scheduler. Legacy
learning timers remain available for manual diagnostics, but are not part of
the default production schedule.

## EI Stack

`eimemory` is one layer of the EI series:

- `eimemory`: memory, knowledge, recall, evaluation, and learning ledger
- `eibrain`: cognition and runtime decision layer
- `eiskills`: skill registry and capability packaging
- `eitraining`: replay, evaluation, and training feedback loop
- `eiprotocol`: shared contracts
- `eihead`: embodied/audio/vision runtime integration

## Documentation

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [Evaluation](docs/evaluation.md)
- [L5 roadmap](docs/l5-roadmap-spec.md)
- [Memory scoring contract](docs/scoring/memory-scoring-contract-v1.md)

## Project Status

The project is under active development. The current focus is production-grade
memory governance: scoped recall, evidence-first learning, replay gates,
capability ledgers, and conservative rollout paths for agent self-improvement.
