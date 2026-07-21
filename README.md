# eimemory

![eimemory memory infrastructure hero](docs/assets/eimemory-github-hero.png)

**Long-term memory and autonomous evolution platform for OpenClaw agents, with governed recall, eval gates, rollback, and safe self-deploy loops.**

`eimemory` is a local-first memory and learning runtime for long-running AI agents. It gives an agent a durable way to remember what happened, recall the right context, evaluate whether memory helped, and turn repeated failures into gated improvements.

The project is built for OpenClaw and the EI stack, but the core runtime is a standalone Python package with CLI, Python APIs, HTTP/RPC surfaces, and local storage.

## Why eimemory?

### The Problem

Most agent systems keep conversation history, but lose operational experience:

- What the operator corrected
- Which task failed and why
- Which rule helped in a real run
- Which memory should be recalled for a specific scope
- Whether a proposed improvement passed replay, health, and rollback checks

Chat history disappears. Lessons aren't learned. Agents repeat mistakes.

### The Solution

`eimemory` treats operational signals as **records**, not residue. It stores them locally, indexes them for retrieval, evaluates their usefulness, and feeds bounded learning loops without giving memory itself unlimited execution authority.

## Core Features

### 🧠 Intelligent Recall
- **Hybrid indexing**: Lexical, semantic, graph, quality, and recency signals combined
- **Scoped memory**: Separate contexts for users, workspaces, projects, and agents
- **Smart ranking**: Returns the right memory for the right moment

### 📝 Complete Event Capture
- **Task intents**: What was the agent trying to do?
- **Execution paths**: How did it get there?
- **Outcomes**: What actually happened?
- **Corrections**: What did humans fix?
- **Incidents**: What went wrong?

### 🎯 Knowledge Management
- **Papers & URLs**: Ingest external knowledge
- **Claims & entities**: Extract structured information
- **Relationships**: Understand connections between ideas
- **Compiled views**: Optimized recall for specific tasks

### 🛡️ Safety-First Governance
```
L0: Records, reports, dashboards (read-only)
L1: Local safe changes (memory rules, playbooks)
L2: Gated rollout (evidence + health + canary + rollback)
L3: Dangerous actions (blocked by default)
```

### 📊 Learning & Evaluation
- **Replay testing**: Run past scenarios with new rules
- **Regression detection**: Catch when "improvements" break things
- **Quality metrics**: Measure if memory actually helps
- **Learning ledger**: Complete audit trail of what was learned

### 🚀 Production Ready
- **Immutable releases**: Reproducible deployments
- **systemd integration**: Easy service management
- **Health monitoring**: Built-in diagnostics
- **Rollback support**: Revert to prior state if issues occur

## What It Provides

- Local record store backed by JSONL and SQLite
- Hybrid recall over lexical, semantic, graph, quality, and recency signals
- Scoped memory for users, workspaces, projects, channels, and agents
- Knowledge intake for papers, URLs, claims, entities, relations, and compiled recall views
- Event memory for task intent, execution paths, outcomes, corrections, and incidents
- Replay and evaluation tools for recall quality, living memory, regression cases, and task outcomes
- Governance loops for learning goals, candidate improvements, capability scores, rollout gates, canaries, and rollback evidence
- OpenClaw hooks and eibrain RPC integration for production agent runtimes
- CLI, Python APIs, and HTTP/RPC interfaces
- Zero external dependencies

## Quick Start

### Installation

```bash
pip install eimemory
```

### 5-Minute Demo

```bash
# Initialize
eimemory init

# Add your first memory
eimemory ingest "Be concise and direct" --title "Communication style"

# Recall information
eimemory recall "how should I write?"

# Log experience
eimemory reflect log communication "too wordy" "Use 1-2 sentences"

# See what could be learned
eimemory learn cycle --dry-run

# Apply learning with safety gates
eimemory learn cycle

# Check what was learned
eimemory learn ledger
```

For more details, see [Quick Start Guide](docs/QUICKSTART.md).

## Core Model

`eimemory` separates memory from authority.

```
events -> records -> indexes -> recall -> evaluation -> learning goals
                                       -> candidates -> gates -> ledger
```

The runtime can observe, store, retrieve, score, propose, and audit. High-risk actions such as external sends, spending, credential changes, irreversible deletion, private data export, or production mutation stay outside automatic memory authority unless a deployment adapter and policy gate explicitly allow them.

## Main Surfaces

### Memory Layer

The memory layer stores structured records and returns task-relevant context with metadata.

```bash
eimemory ingest "Remember concise replies" --title "Concise reply style"
eimemory recall "how should this agent reply?"
eimemory quality stats
```

### Knowledge Layer

The knowledge layer turns external sources into claims, entities, relations, pages, and recall views.

```bash
eimemory paper ingest --url https://example.com/paper --title "Example paper"
eimemory intake run --source-kind paper --limit 10
eimemory source scan
```

### Experience Layer

The experience layer records outcomes, corrections, and incidents.

```bash
eimemory reflect log reply-style "Missed concise style" "Use one direct sentence"
eimemory reflect check
```

### Governance Layer

The governance layer runs bounded self-improvement loops.

```bash
eimemory learn cycle --dry-run
eimemory learn ledger --limit 50
eimemory learn dashboard --persist
```

### Health & Diagnostics

```bash
curl http://127.0.0.1:8091/health
eimemory doctor --json
```

## Deployment

### Local Development

```bash
eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
curl http://127.0.0.1:8091/health
```

### Production Deployment

```bash
# Install and enable services
deploy/install_immutable_release.sh

# Start RPC service
systemctl --user enable --now eimemory-rpc.service

# Enable nightly governance
systemctl --user enable --now eimemory-nightly.timer
```

See [Deployment Guide](docs/deployment.md) for production setup.

## Documentation

| Document | Purpose |
|----------|---------|
| [Quick Start](docs/QUICKSTART.md) | Get running in 5 minutes |
| [Architecture](docs/architecture.md) | System design and components |
| [Deployment](docs/deployment.md) | Production setup and operations |
| [Evaluation](docs/evaluation.md) | How memory quality is measured |
| [Comparison](docs/COMPARISON.md) | vs other memory systems |
| [Changelog](CHANGELOG.md) | Version history and features |
| [FAQ](FAQ.md) | Common questions and troubleshooting |

## Project Status

The project is under active development. Current focus:

- Production-grade memory governance
- Scoped recall with zero boundary crossing
- Evidence-first learning with conservative gates
- Replay testing and regression detection
- Capability ledgers for tracking improvement
- Safe rollout paths for agent self-improvement

## EI Stack

`eimemory` is one layer of the EI series:

- **eimemory**: Memory, knowledge, recall, evaluation, and learning ledger
- **eibrain**: Cognition and runtime decision layer
- **eiskills**: Skill registry and capability packaging
- **eitraining**: Replay, evaluation, and training feedback loop
- **eiprotocol**: Shared contracts
- **eihead**: Embodied/audio/vision runtime integration

## Use Cases

### Long-Running Assistants

Remember user preferences, past interactions, and effective approaches. Improve response quality over time.

### Autonomous Agents

Learn from execution outcomes and operator corrections. Self-improve safely with governance gates.

### Multi-Agent Systems

Shared memory across agents. Agent-specific scoped views. Coordinated learning.

### Research Agents

Persistent knowledge from papers and URLs. Evidence-based reasoning. Audit trails for reproducibility.

## Integration

### Python API

```python
from eimemory.memory import MemoryRuntime

runtime = MemoryRuntime()
runtime.ingest("Be helpful", title="Behavior")
context = runtime.recall("How should I behave?", k=5)
runtime.log_outcome(task_id="task-1", outcome="success")
```

### HTTP/RPC

```bash
eimemory serve-eibrain-rpc --port 8091

# Ingest
curl -X POST http://127.0.0.1:8091/api/ingest \
  -d '{"content": "Memory", "title": "Label"}'

# Recall
curl http://127.0.0.1:8091/api/recall?query=test
```

### Agent Runtime Adapters

The additive `agent.runtime.v1` contract supports OpenClaw, Codex, and Hermes
without changing OpenClaw's existing L5 evidence rules. Authority is
`per_channel`: the base OpenClaw scope is unchanged, while Codex and Hermes use
deterministic scopes such as `embodied::channel::codex` and
`embodied::channel::hermes`. Recall never crosses those scopes.

Codex installs through the marketplace under `integrations/codex`; Hermes uses
the standalone native `MemoryProvider` under `integrations/hermes/eimemory`.
Both clients require `EIMEMORY_RPC_URL` and `EIMEMORY_RPC_TOKEN`, use bounded
inputs/caches, and are fail-open when the authenticated RPC is unavailable.
Successful terminal events count toward L5 only when they carry explicit,
release-bound verification; session lifecycle events never count.

See [Adapter Operations](docs/operations.md) for install, status, disable,
bypass, channel isolation, and live-verification procedures.

### OpenClaw Integration

eimemory provides hooks for OpenClaw agent runtimes. See `docs/architecture.md` for integration details.

## Comparison with Alternatives

| Feature | eimemory | Chat History | Vector DBs | LangChain | LlamaIndex |
|---------|----------|--------------|------------|-----------|-----------|
| Persistent storage | ✅ | ❌ | ✅ | ⚠️ | ✅ |
| Hybrid recall | ✅ | ❌ | ⚠️ | ⚠️ | ⚠️ |
| Safety governance | ✅ | ❌ | ❌ | ❌ | ❌ |
| Learning loops | ✅ | ❌ | ❌ | ❌ | ❌ |
| Zero dependencies | ✅ | ✅ | ❌ | ❌ | ❌ |
| Local-first | ✅ | ✅ | ⚠️ | ✅ | ✅ |

[Full comparison →](docs/COMPARISON.md)

## Contributors

`eimemory` is built in the open. Special thanks to external contributors:

- [Garry Tan](https://github.com/garrytan)

External contributions, review, and real-world use are part of the project's signal that local-first agent memory is worth building carefully.

## Getting Involved

### Report Issues

Found a bug? [Open an issue](https://github.com/darrowz/eimemory/issues) with:
- Steps to reproduce
- Expected vs actual behavior
- Python version and OS

### Request Features

Have an idea? [Open a discussion](https://github.com/darrowz/eimemory/discussions) to share use cases and feedback.

### Contribute Code

See [Contributing Guide](CONTRIBUTING.md) for development setup and guidelines.

### Share Your Use Case

Using eimemory? Let us know! Open an issue or discussion to share your experience.

## License

[License details TBD]

## Support

- 📖 [Documentation](docs/)
- 🐛 [Report Issues](https://github.com/darrowz/eimemory/issues)
- 💬 [GitHub Discussions](https://github.com/darrowz/eimemory/discussions)
- ❓ [FAQ](FAQ.md)

---

**Building intelligent agents that learn and remember. Safely.** 🚀
