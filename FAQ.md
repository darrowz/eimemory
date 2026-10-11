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

The earlier version-identified production run was **1.14.57 / `eab88240`**. Deployment and
nightly execution succeeded; known-item smoke passed 10/10. **Formal business
recall acceptance did not pass** because qualifying evidence was incomplete.
Memory evaluation had no dataset, nine evolution items awaited hypotheses and
model review was unavailable. See [receipt-based acceptance status](docs/acceptance-status.md).

The later reported **1.14.58 / `534bb2f0`** receipt `ref_4cc83efb2f5d` failed at
`research_closure_review`. Detailed records report
`research_review_llm_unconfigured`: the managed review route was missing.
The latest supplied production readback is **1.14.62 / `82cba75a`**:
deployment receipt acceptance passed and current services are healthy, but the
October 11 nightly failed at `recall_quality_gate`. Smoke passed 9/10; one case
returned results without its expected item, giving an error rate 0.10 above
the ≤0.05 limit. Formal recall remains 0/15; 312 reviews produced zero passes,
the memory benchmark has zero retrieval cases, and all nine evolution results
have zero candidate hypotheses. The reason-classification repair is confirmed.
Mainline **1.14.63** repairs digest identity collisions and preserves diagnostic IDs/counts;
its production behavior and recall recovery have not been validated.
Since 1.14.60, research review automatically discovers installed Hermes and reads its current
review provider/model when no explicit command is set. It also retains the
1.14.59 durable failure diagnostics and deployment preflight. Production acceptance still
requires verified evidence. No blanket production-quality or L5 claim follows
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
latest 1.14.62 run has 0/15 accepted cases and zero passing reviews out of 312.
Its smoke passed 9/10, with an explicit error-rate violation that failed nightly.
Formal evidence remains incomplete; the older 10/10 smoke does not certify it.

### What does the latest smoke error rate of 0.10 mean?

The diagnostic metric counts cases that return items while missing their expected
item, plus erroneous returns on no-answer cases, divided by all evaluated cases.
For `ref_a2dee7107fde`, the operator read back one known-item miss with results
and zero no-answer returns, over ten cases. The failed case returned two items
and matched neither to its expectation. This is a measured gate violation, but
it does not establish natural-query or no-answer quality. The ≤0.05 threshold
remains unchanged. Version 1.14.63 retains expected record IDs and explicit
counts in newly persisted diagnostic reports; older receipts are not rewritten.
It also repairs deduplication that merged different dated digest pages sharing
a summary prefix. Local native regression checks retain the original target;
production recall recovery still needs its own persisted verification.

### Should automatic reviews remain pending when evidence is missing?

From 1.14.61, each scanned case receives a durable pass or fail conclusion.
Unavailable semantic review or insufficient independent signals means fail,
with recorded reasons and no trusted relevance label. Original captures remain
active; new evidence can trigger automatic reevaluation. A completed review does
not by itself meet the 15-case business gate or release-authorization requirement.

### Why was memory evaluation skipped?

`memory_eval_dataset_empty` means no usable retrieval benchmark ran. A separate
code or capability case pass cannot substitute for memory evaluation. That error
was supplied for the earlier 1.14.57 run. The October 11 1.14.61 readback
reports `replay_dataset`, zero retrieval cases and `memory_eval_dataset_empty`.
The latest 1.14.62 run still has zero retrieval cases with the same empty-dataset
reason; its dataset-source field was not supplied.
Supply an
independent retrieval dataset and inspect its persisted verdict. Mainline repairs
keep catalog execution separate and persist explicit not-run receipts; those
repairs began in 1.14.58; mainline 1.14.59 also preserves research-review failure
reasons in durable nightly receipts. Formal recall quality remains unaccepted. See [evaluation](docs/evaluation.md).

### What did `reason_not_allowlisted` mean in the earlier 1.14.61 run?

It combined codes omitted from the safe diagnostic list. Follow-up readback of
the per-case receipts found capture boundary rejection (10), no matching
authoritative capture decision (186) and semantic items not delivered (5),
totaling 201 and matching the summary, with 201 distinct affected receipts.
Missing matching decisions can reflect lost
evidence or mismatched boundaries; the code alone does not prove pruning or
algorithm failure. Version 1.14.62 preserves those exact codes and other fixed
review failure reasons, while continuing to redact arbitrary text. The latest
1.14.62 summary confirms direct counts of 10, 186 and **6** for those codes;
the historical third count remains **5**.

### Does research review require a fixed Hermes command or model?

No. With neither research-specific nor shared command configured, version
1.14.60 discovers the service user's Hermes installation and follows its current
provider/model configuration. Explicit commands and protected installation/profile
overrides remain supported. Invalid runtime contracts, model mismatch and failed
completion keep the review unavailable. See [automatic Hermes review](docs/evaluation.md#automatic-hermes-review).

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
embedding route and host timeout. The earlier 1.14.57 fixed-item smoke had P95
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
