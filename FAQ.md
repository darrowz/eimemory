# eimemory FAQ

## General Questions

### What is eimemory?

eimemory is a local-first memory and learning runtime designed for long-running AI agents. It provides:

- **Durable memory**: Store and retrieve agent experiences, not just chat history
- **Smart recall**: Hybrid indexing using lexical, semantic, graph, quality, and recency signals
- **Safe learning**: Governance gates for autonomous improvement without unlimited authority
- **Production ready**: SQLite + JSONL storage, HTTP/RPC interfaces, systemd deployment

### Who should use eimemory?

- Teams building long-running AI agents that need persistent learning
- Organizations requiring safe, auditable agent self-improvement
- Projects with OpenClaw or EI stack integration needs
- Anyone needing local-first agent memory with governance controls

### How does it differ from conversation history?

| Aspect | Chat History | eimemory |
|--------|--------------|----------|
| What's stored | Messages only | Outcomes, corrections, incidents, learned rules |
| Retrieval | Linear, dump all | Hybrid recall, scoped, ranked by relevance |
| Learning | None (or external) | Built-in governance loops |
| Authority | Not applicable | Tiered (L0-L3), safe by default |
| Storage | Ephemeral | Persistent, indexed, analyzable |

### Is eimemory production-ready?

Yes, eimemory is designed for production use with:
- Immutable release deployments
- systemd service templates
- Health checks and monitoring
- Rollback capabilities
- Audit logging

Current focus: production-grade memory governance with conservative rollout paths.

## Installation & Setup

### How do I install eimemory?

```bash
pip install eimemory
```

Or for development:
```bash
git clone https://github.com/darrowz/eimemory.git
cd eimemory
pip install -e .
```

### What are the Python version requirements?

Python 3.11 or higher.

### How do I initialize a new memory store?

```bash
eimemory init
```

This creates the local JSONL and SQLite storage structure.

### What dependencies does eimemory have?

eimemory has zero runtime dependencies by default. Optional features may require additional packages (specified in `extras_require`).

## Usage Questions

### How do I add knowledge to memory?

```bash
# Add general memories
eimemory ingest "Remember concise replies" --title "Concise reply style"

# Ingest papers/URLs
eimemory paper ingest --url https://example.com/paper --title "Example Paper"

# Run knowledge intake pipeline
eimemory intake run --source-kind paper --limit 10
```

### How do I recall information?

```bash
# Retrieve relevant context for a query
eimemory recall "how should this agent reply?"

# Get quality statistics
eimemory quality stats
```

### What does "scoped memory" mean?

eimemory supports hierarchical memory scoping:
- **User scope**: Individual user memories
- **Workspace scope**: Shared team memories
- **Project scope**: Project-specific context
- **Agent scope**: Agent-specific learned rules

This ensures agents only access relevant context and prevents memory pollution.

### How does evaluation work?

Evaluation in eimemory:
1. **Recall quality**: Did the right memory get retrieved?
2. **Living memory**: Is retrieved memory still accurate?
3. **Regression cases**: Do recalled rules still work?
4. **Task outcomes**: Did memory help achieve the goal?

## Learning & Governance

### How does autonomous learning work?

```bash
eimemory learn cycle --dry-run    # See what would be learned
eimemory learn cycle              # Apply learning with gates
eimemory learn ledger --limit 50  # View learning history
eimemory learn dashboard          # Visualize learning progress
```

### What are governance gates?

Governance gates ensure safe learning:
- **Evidence gate**: Learning must be backed by data
- **Health gate**: Learned rules must pass health checks
- **Canary gate**: Small rollout before full deployment
- **Rollback gate**: Previous behavior available if issues occur

### What are authority tiers (L0-L3)?

| Tier | Examples | Authority |
|------|----------|-----------|
| L0 | Records, reports, dashboards | Read-only analysis |
| L1 | Memory rules, playbooks, eval fixtures | Local low-risk changes |
| L2 | Gated rollout | Requires evidence + eval + health + canary checks |
| L3 | External sends, spending, auth changes | Blocked by default, requires explicit adapter |

### Can I disable autonomous learning?

Yes. Use `--dry-run` for preview, or configure gates to reject changes. Learning runs only on the nightly schedule by default.

## Deployment

### How do I deploy eimemory to production?

See [Deployment Guide](docs/deployment.md) for detailed instructions. Quick version:

```bash
# 1. Install release
/opt/eimemory/releases/<commit>/bin/eimemory --version

# 2. Enable systemd service
systemctl --user enable --now eimemory-rpc.service

# 3. Enable nightly governance
systemctl --user enable --now eimemory-nightly.timer

# 4. Verify
curl http://127.0.0.1:8091/health
```

### Can I use eimemory in Docker?

eimemory is filesystem-based and works with Docker volumes. See `docs/deployment.md` for containerization examples.

### What's the recommended memory store backend?

SQLite (included) for most deployments. For distributed systems, custom backends can implement the memory interface.

## Troubleshooting

### Memory isn't being retrieved correctly

Check:
1. Run `eimemory doctor` for diagnostics
2. Verify memories exist: `eimemory recall "test"`
3. Check scoping settings match your query
4. Review quality stats: `eimemory quality stats`

### Autonomous learning seems stuck

```bash
# Diagnose learning pipeline
eimemory learn cycle --dry-run

# Check ledger for recent activity
eimemory learn ledger --limit 10

# View service logs
journalctl --user -u eimemory-nightly.service -n 50
```

### RPC service won't start

```bash
# Check health endpoint
curl http://127.0.0.1:8091/health

# Review logs
journalctl --user -u eimemory-rpc.service -n 100 --no-pager

# Run diagnostics
eimemory doctor --json
```

### High memory usage

- Review stored memories: `eimemory quality stats`
- Archive old memories if needed
- Consider scoping to reduce active set
- Check for circular dependencies in rules

## Integration

### How do I integrate eimemory with OpenClaw?

See `docs/architecture.md` for OpenClaw-specific integration points. eimemory provides:
- Event hooks for action capture
- RPC interfaces for memory access
- Governance adapters for policy enforcement

### Can I use eimemory with other AI frameworks?

eimemory's core runtime is framework-agnostic. HTTP/RPC interfaces allow integration with:
- LangChain
- LlamaIndex
- Custom agent frameworks

See `docs/integration/` for examples.

### What's the relationship with eibrain?

eibrain is the cognition layer that uses eimemory for context. eimemory handles memory, while eibrain handles decision-making.

## Performance

### How much storage does eimemory use?

Depends on memory volume:
- Small agents: MB range
- Medium deployments: GB range
- Large systems: Depends on indexing strategy

Use `eimemory quality stats` to monitor growth.

### Is retrieval fast enough for real-time use?

Yes. Hybrid indexing optimizes for:
- Lexical exact match (< 1ms)
- Semantic similarity (< 100ms)
- Graph traversal (depends on graph size)

### Can I tune retrieval performance?

Yes, through:
- Scoping (reduce search space)
- Index strategy tuning
- Memory archival
- Custom retrieval adapters

## Community & Support

### How do I report issues?

[Open a GitHub Issue](https://github.com/darrowz/eimemory/issues) with:
- Steps to reproduce
- Expected vs actual behavior
- Python version and OS
- Relevant logs

### Where's the full documentation?

- [Architecture](docs/architecture.md)
- [Deployment](docs/deployment.md)
- [Evaluation](docs/evaluation.md)
- [Memory Scoring Contract](docs/scoring/memory-scoring-contract-v1.md)

### Can I contribute?

Absolutely! See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

### What's the roadmap?

See [L5 Roadmap](docs/l5-roadmap-spec.md) for planned features and improvements.

---

**Didn't find your answer?** [Open an issue](https://github.com/darrowz/eimemory/issues) or check the [documentation](docs/).
