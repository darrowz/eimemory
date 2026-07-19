# eimemory Quick Start Guide

Get up and running with eimemory in 5 minutes.

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
- **Python API**: Check `eimemory.memory` module
- **Architecture**: Read [docs/architecture.md](architecture.md)

### For Production Deployment

- Follow [Deployment Guide](deployment.md)
- Set up systemd services
- Configure RPC endpoints

### Integrate with Your Agent

```python
from eimemory.memory import MemoryRuntime
from eimemory.recall import HybridRecall

# Initialize runtime
runtime = MemoryRuntime()

# Add memory
runtime.ingest(
    content="Be helpful and thorough",
    title="Agent behavior"
)

# Recall context for a task
context = runtime.recall(
    query="How should I behave?",
    k=5
)

# Log outcomes
runtime.log_outcome(
    task_id="task-123",
    outcome="success",
    corrected_by="operator"
)

# Evaluate learning candidates
runtime.evaluate_candidates()
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

# View all memories
eimemory show

# Archive old memories
eimemory archive --older-than 90

# Export memory
eimemory export --format jsonl > backup.jsonl
```

### Learning & Governance

```bash
# Preview learning cycle
eimemory learn cycle --dry-run

# See learning history
eimemory learn ledger

# View learning dashboard
eimemory learn dashboard --persist

# Check learning readiness
eimemory learn check-readiness
```

### Health & Diagnostics

```bash
# Full system check
eimemory doctor

# Get health status
curl http://127.0.0.1:8091/health

# View logs
eimemory logs --tail 50
```

## Local RPC Service

Start an HTTP/RPC service for programmatic access:

```bash
eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
```

In another terminal, test it:

```bash
# Health check
curl http://127.0.0.1:8091/health

# Ingest via HTTP
curl -X POST http://127.0.0.1:8091/api/ingest \
  -H "Content-Type: application/json" \
  -d '{"content": "Test memory", "title": "Test"}'

# Recall via HTTP
curl -X GET "http://127.0.0.1:8091/api/recall?query=test"
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
