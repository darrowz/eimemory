---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'ad9df863-50a7-46b8-8032-09d466fdbb7c'
  PropagateID: 'ad9df863-50a7-46b8-8032-09d466fdbb7c'
  ReservedCode1: '659a2507-b75b-4ae3-8771-650c451cf37e'
  ReservedCode2: '659a2507-b75b-4ae3-8771-650c451cf37e'
---

# eimemory Quick Start Guide

Package version: **1.14.0**. Get up and running with eimemory in 5 minutes.

## Installation

```bash
pip install eimemory
```

## Initialize Your Memory Store

```bash
eimemory init
```

This creates a local JSONL + SQLite store in `.eimemory/`.

## 1. Add Your First Memory

```bash
eimemory ingest "Be concise and direct in replies" \
  --title "Communication style"
```

## 2. Recall Information

Ask eimemory to retrieve relevant context:

```bash
eimemory recall "how should I write responses?"
```

Output:
```
Query: how should I write responses?

Results:
1. [0.87] Communication style
   "Be concise and direct in replies"
   [similarity, recency]
```

## 3. Check Memory Quality

```bash
eimemory quality stats
```

## 4. Record Experience

Log corrections and learnings:

```bash
eimemory reflect log communication "User said too wordy" \
  "Make replies 1-2 sentences max"
```

## 5. Run Learning Cycle

See what the system could learn (dry-run):

```bash
eimemory learn cycle --dry-run
```

Apply learning with safety gates:

```bash
eimemory learn cycle
```

View what was learned:

```bash
eimemory learn ledger --limit 10
```

## Next Steps

### For Developers

- **CLI Reference**: `eimemory --help`
- **Python API**: `from eimemory import Runtime`
- **Architecture**: Read [docs/architecture.md](architecture.md)

### For Production Deployment

- Follow [Deployment Guide](deployment.md)
- Set up systemd services
- Configure RPC endpoints

### Integrate with Your Agent

```python
from eimemory import Runtime

# Initialize runtime. `root` is keyword-only and defaults to a local data dir.
runtime = Runtime.create(root="./data")

# Store a durable memory. `text`, `memory_type`, `title`, and `scope` are required.
runtime.memory.ingest(
    text="Be helpful and thorough",
    title="Agent behavior",
    memory_type="fact",
    scope={"agent_id": "main", "workspace_id": "default"},
)

# Recall relevant context for a task. Returns a RecallBundle.
bundle = runtime.memory.recall(
    query="How should I behave?",
    scope={"agent_id": "main", "workspace_id": "default"},
)
```

### Explore Examples

- Check `examples/` directory for full code samples
- Browse `docs/` for detailed guides
- Review `tests/` for API usage patterns

## Common Operations

### Memory Management

```bash
# Search memory
eimemory recall "my query"

# Export memory records to a file path (no --format flag; writes to the path)
eimemory export backup.jsonl
```

### Learning & Governance

```bash
# Preview learning cycle
eimemory learn cycle --dry-run

# See learning history
eimemory learn ledger

# View learning dashboard
eimemory learn dashboard --persist
```

### Health & Diagnostics

```bash
# Full system check
eimemory doctor

# Get health status (requires the RPC service to be running)
curl http://127.0.0.1:8091/health
```

## Local RPC Service

Start an HTTP/RPC service for programmatic access:

```bash
eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
```

In another terminal, test it. The RPC surface is JSON-RPC over POST
(dispatched by the `method` field), not REST paths like `/api/ingest`:

```bash
# Health check (unauthenticated liveness/readiness)
curl http://127.0.0.1:8091/health

# Ingest via JSON-RPC (requires EIMEMORY_RPC_AUTH_TOKEN bearer auth)
curl -X POST http://127.0.0.1:8091/ \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $EIMEMORY_RPC_AUTH_TOKEN" \
  -d '{"method": "memory.ingest", "params": {"text": "Test memory", "title": "Test", "memory_type": "fact", "scope": {"agent_id": "main", "workspace_id": "default"}}}'
```

## Troubleshooting

### "No memories found"
- Verify you ran `eimemory init`
- Check you've added memories with `eimemory ingest`
- Ensure your query matches memory content

### "Learning cycle failed"
- Run `eimemory learn cycle --dry-run` to see what failed
- Check `eimemory doctor` for system issues
- Review logs for error details

### "RPC service won't start"
- Check port 8091 isn't already in use
- Try different port: `--port 9090`
- Review firewall settings

## Learn More

- **Full Guide**: [docs/architecture.md](architecture.md)
- **Deployment**: [docs/deployment.md](deployment.md)
- **Evaluation**: [docs/evaluation.md](evaluation.md)
- **FAQ**: [FAQ.md](../FAQ.md)
- **Contributing**: [CONTRIBUTING.md](../CONTRIBUTING.md)

## Get Help

- 📖 [Documentation](.)
- 🐛 [Report Issues](https://github.com/darrowz/eimemory/issues)
- 💬 [GitHub Discussions](https://github.com/darrowz/eimemory/discussions)
- 📝 [FAQ](../FAQ.md)

---

**You're ready!** Start building intelligent, learning agents with persistent memory. 🚀

> AI生成