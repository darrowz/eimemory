# eimemory FAQ

## General Questions

### What is eimemory?

`eimemory` is a local-first memory and evidence-gated learning runtime for long-running AI agents. It provides:

- **Durable memory**: Store and retrieve agent experiences, not just chat history
- **Smart recall**: Hybrid indexing using lexical, semantic, graph, quality, and recency signals
- **Safe learning**: Governance gates for autonomous improvement without unlimited authority
- **Operations tooling**: JSONL records, SQLite projections, authenticated RPC, immutable releases and systemd templates. Deployment health and business recall acceptance are separate checks.

### Who should use eimemory?

- Teams building long-running AI agents that need persistent learning
- Organizations requiring safe, auditable agent self-improvement
- Projects using Codex, Hermes Agent, optional OpenClaw, eibrain or custom Python/RPC clients
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

The runtime includes immutable releases, systemd templates, health checks,
rollback and audit evidence. Readiness must be assessed for the actual workload,
release, scope and host channels.

The latest reported production run is **1.14.57 / `eab88240`**. Deployment and
nightly execution succeeded; known-item smoke passed 10/10. **Formal business
recall acceptance did not pass** because qualifying evidence was incomplete.
Memory evaluation had no dataset, nine evolution items awaited hypotheses and
model review was unavailable. See [receipt-based acceptance status](docs/acceptance-status.md).

Mainline workflow repairs are packaged as **1.14.58** and have not been deployed
or re-evaluated in production. No blanket production-quality or L5 claim follows
from installation, tests or service health.

## Installation & Setup

### How do I install eimemory?

Install from a reviewed source checkout:

```bash
git clone https://github.com/darrowz/eimemory.git
cd eimemory
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

For reproducibility, select an exact reviewed commit before installing. Use
[immutable releases](docs/deployment.md) for production and verify the installed
package version with `python -c "from eimemory import __version__; print(__version__)"`
and deployed commit identity through RPC health.

### What are the Python version requirements?

Python 3.11 or higher.

### How do I initialize a new memory store?

```bash
eimemory init
```

This creates the local JSONL and SQLite storage structure.

### What dependencies does eimemory have?

eimemory has zero runtime dependencies by default. Optional features may require additional packages (specified in `[project.optional-dependencies]` in `pyproject.toml`).

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

Memory and evidence bind to an exact owner tuple: `tenant_id`, `agent_id`,
`workspace_id`, and `user_id`, with source and visibility policies. Project
separation can be represented by workspace identity; it is not an additional
implicit hierarchy. Host adapters also retain separate channel authority.
A query must use the intended owner/source/channel context; scopes do not grant
cross-user or cross-channel access.

### How does evaluation work?

Evaluation in eimemory:
1. **Recall quality**: Did the right memory get retrieved?
2. **Living memory**: Is retrieved memory still accurate?
3. **Regression cases**: Do recalled rules still work?
4. **Task outcomes**: Did memory help achieve the goal?
5. **Capability evidence**: Did one exact revision/provider binding pass its sealed catalog case with independently persisted evidence?

### What is the dynamic evaluation catalog?

Executable evaluators are loaded only from installed Python entry points in
`eimemory.capability_catalog.bootstrap.v1`. The bootstrap accepts trusted Python
callables and typed `CatalogCase` descriptors, then seals the catalog. YAML,
JSON, database rows, and adapter advertisements cannot register executable code.
An absent catalog leaves ordinary memory available but blocks dynamic evaluation
with `catalog_not_configured`.

The maintained Hongtu build ships its catalog from the main `eimemory` package.
It evaluates scoped recall independently for Hermes and OpenClaw; it does not
activate the other seeded capabilities without their own bindings and evidence.

### What does L5 v3 `ready` mean?

L5 v3 reports four independent axes: loop maturity, capability readiness,
adapter readiness, and deployment assurance. `ready` means the selected profile
has no blocking gap under those contracts. It does not mean every discovered
capability is active, every original criterion has fresh production proof, or
the loop has reached `compounding`.

The historical 2026-08-22 Hongtu reference state was `ready` at loop stage `evolving`:
Hermes and OpenClaw recall bindings are reliable, and a canonical knowledge
link/hypothesis/evaluation/feedback chain is live. A fresh production
code-evolution transaction with regression, commit, push, deployment, and
rollback evidence remains outstanding. See
[the dated closure record](docs/audit/l5-v3-production-closure-2026-08-22.md).
That historical assessment does not certify the latest release or the formal
business recall gate. See [current acceptance status](docs/acceptance-status.md).

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

Use `--dry-run` to preview a CLI cycle. To stop scheduled work, disable the
specific timers enabled in your deployment; nightly and learning companion
timers are documented in the [systemd guide](deploy/systemd/README.md).
A dry run of one command does not disable other running services. Deployment
machine policy separately controls automatic code apply, commit and deployment.

## Deployment

### How do I deploy eimemory to production?

Follow the [Deployment Guide](docs/deployment.md) and
[systemd installation instructions](deploy/systemd/README.md). Install an exact
commit into an immutable release, configure credentials outside the checkout,
and start the user services. Then verify release identity, channel write/readback,
managed services and the task-specific persisted acceptance receipts.

```bash
deploy/install_immutable_release.sh <full-40-character-commit>
/opt/eimemory/current/.venv/bin/python -c "from eimemory import __version__; print(__version__)"
curl -fsS http://127.0.0.1:8091/health
```

Those checks verify installation and health. They do not replace the formal
recall gate or memory benchmark.

### Can I use eimemory in Docker?

Persist the runtime root outside the container and supply credentials through
the deployment environment. The maintained production runbook uses Linux
immutable releases and user systemd services; this repository does not provide
an equivalent container acceptance runbook.

### What's the recommended memory store backend?

The maintained store uses local JSONL records and SQLite. Optional PostgreSQL/
pgvector serves retrieval projections; it does not replace the authoritative
record or capability-domain storage. See [architecture](docs/architecture.md)
and [vector candidates](docs/postgres-vector-candidates.md).

## Troubleshooting

### Memory isn't being retrieved correctly

Check:
1. Run `eimemory doctor` for diagnostics
2. Verify memories exist: `eimemory recall "test"`
3. Check scoping settings match your query
4. Review quality stats: `eimemory quality stats`

### Why did 10/10 smoke still fail formal acceptance?

Known-item smoke verifies that fixed entries can be found. Formal acceptance
requires qualifying natural production cases, trusted labels, exact source/scope
boundaries, release authorization and unchanged ranking/noise standards. The
latest run had 0/15 accepted cases, with two pending. Its gate stopped at
`recall_quality_evidence_incomplete`; the smoke noise and precision observations
were not treated as a completed business evaluation.

### Why was memory evaluation skipped?

`memory_eval_dataset_empty` means no usable retrieval benchmark ran. A separate
code or capability case pass cannot substitute for memory evaluation. Supply an
independent retrieval dataset and inspect its persisted verdict. Mainline repairs
keep catalog execution separate and persist explicit not-run receipts; those
repairs are part of mainline 1.14.58 and are not deployed. See [evaluation](docs/evaluation.md).

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

Use the [optional external bridge](integrations/openclaw/eimemory-bridge/README.md)
and [systemd adapter guide](deploy/systemd/README.md). It exposes eight lifecycle
hooks and a status tool through the configured external plugin path. OpenClaw
is optional; an enabled but incomplete integration fails closed.

### How do I integrate Codex or Hermes?

Codex uses [hooks and MCP tools](integrations/codex/eimemory/README.md).
Hermes uses its native [memory provider](integrations/hermes/eimemory/README.md)
plus the [host hook bridge](integrations/hermes/eimemory_hook/README.md).
Install a matching reviewed runtime and configure authenticated RPC outside
tracked plugin files. Each host retains its own channel authority. Trusted task
receipts require an operator-separated producer profile; recall alone cannot
mint that evidence. See [operations](docs/operations.md).

### Can I use eimemory with other AI frameworks?

eimemory's core runtime is framework-agnostic. HTTP/RPC interfaces allow integration with:
- LangChain
- LlamaIndex
- Custom agent frameworks

See [integration architecture](docs/architecture.md#integration-plane) for an overview.

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

Measure the actual workload, memory size, source/scope policy, payload budget,
embedding route and host timeout. The latest reported fixed-item smoke had P95
340.8 ms, but it is not a general latency guarantee or a natural-query benchmark.
See [evaluation](docs/evaluation.md) and [acceptance status](docs/acceptance-status.md).

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

- [Documentation index](docs/README.md)
- [Acceptance status](docs/acceptance-status.md)
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
