<h1 align="center">eimemory</h1>

<p align="center">
  <strong>Local-first memory, autonomous thinking, and self-evolution runtime for long-running AI agents.</strong>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#why-eimemory">Why eimemory</a> ·
  <a href="#how-it-fits-together">Architecture</a> ·
  <a href="#governed-learning-boundary">Safety model</a> ·
  <a href="#documentation">Docs</a>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Release" src="https://img.shields.io/github/v/tag/darrowz/eimemory">
  <img alt="Platform" src="https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey">
</p>

<p align="center">
  <img src="docs/assets/eimemory-github-hero.png" alt="eimemory architecture overview" width="720">
</p>

---

## Why eimemory?

Agents that run for days, weeks, or across projects have a problem: they
forget, they repeat mistakes, and they cannot safely act on what they learned.
A vector store remembers *text* — it does not turn experience into *behavior*.

`eimemory` is a runtime that closes that loop:

- **Durable memory** — decisions, corrections, incidents, outcomes, knowledge,
  and capability evidence survive across sessions as repairable local records
  (JSONL + SQLite projections).
- **Quality-aware recall** — hybrid lexical, semantic, graph-aware, and
  proactive retrieval with provenance and confidence scoring, exposed over CLI,
  RPC, and host adapters.
- **Autonomous thinking** — scheduled passes turn weak signals, stale goals,
  recent failures, and long-term objectives into reviewable hypotheses and
  learning goals.
- **Gated self-evolution** — candidate improvements must pass isolated
  evaluation, evidence-bound replay, safety checks, and preflight before they
  touch anything; failures roll back and leave audit records.
- **Honest readiness** — an L5 v3 control plane tracks per-capability maturity
  from evidence. A healthy process is never mistaken for a learned skill.

**Conservative autonomy by design.** Learning never grants authority: spending,
external sends, credential changes, private-data export, irreversible deletion,
and production deployment stay outside automatic reach — enforced by policy,
not prompts.

## Quick start

Python 3.11+ required.

```bash
python -m pip install -e .
eimemory init

# Store a durable preference.
eimemory ingest "Be concise and direct" --title "Communication style"

# Recall relevant memory.
eimemory recall "How should this agent reply?"

# Inspect the learning loop without applying anything.
eimemory learn cycle --dry-run

# Run local diagnostics.
eimemory doctor --json
```

Or from Python:

```python
from eimemory import Runtime

runtime = Runtime.create(root="./data")
runtime.memory.ingest(
    text="Deploy only after tests and health checks pass.",
    title="Release rule",
    scope={"agent_id": "main", "workspace_id": "default"},
)
bundle = runtime.memory.recall(
    query="What is the release rule?",
    scope={"agent_id": "main", "workspace_id": "default"},
)
```

Use `Runtime`, the RPC service, or an adapter contract — storage internals stay
private. See the [Quick Start guide](docs/QUICKSTART.md) for a longer tour and
the [FAQ](FAQ.md) for common questions.

## How it fits together

```text
agent or operator
  -> CLI / RPC / runtime adapter
  -> ingest, outcome, or recall API
  -> record store + indexes + memory graph
  -> retrieval and evidence assembly
  -> evaluation and governance
  -> gated promotion, observation, reward, or rollback
```

The source tree is organized around four planes:

| Plane | Main packages | Responsibility |
| --- | --- | --- |
| Data | `models`, `storage`, `raw`, `knowledge` | Records, payloads, indexes, provenance, compiled knowledge |
| Recall | `recall`, `retrieval`, `embeddings`, `scoring` | Candidate generation, filtering, ranking, quality |
| Control | `capabilities`, `experience`, `evaluation`, `governance` | Capability contracts, outcomes, replay, promotion, rollback |
| Integration | `api`, `adapters`, `ei_bridge`, `cli`, `ops` | Public APIs, host hooks, RPC, operations |

See [Architecture](docs/architecture.md) for execution boundaries and the
[Module map](docs/modules.md) for the complete package inventory.

## Runtime integrations

All host adapters implement the same lifecycle contract
(`agent.runtime.v1`) with four public memory operations: recall,
durable capture, verified outcome, and status.

| Host | Surface |
| --- | --- |
| Codex | Hook + MCP surfaces (`eimemory.adapters.codex`) |
| OpenClaw | Eight lifecycle hooks + official bridge plugin |
| Hermes | Provider core + host-context authentication (official plugin packages) |
| eibrain | SDK + bounded HTTP/RPC server and bridge agent |

Remote clients use `EIMEMORY_RPC_URL` / `EIMEMORY_RPC_TOKEN`; credentials stay
outside tracked configuration. Recall and outcome hooks deliberately fail open
for host availability, while persistence and promotion gates stay fail-closed
for trust decisions.

```bash
eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
curl http://127.0.0.1:8091/health
```

Non-health RPC methods require the configured authentication and attestation
policy. Do not expose the service beyond loopback without a strong private
credential.

## Governed learning boundary

There is exactly one production learning flow:

```text
scoped outcomes, reviewed knowledge, adapter advertisements
  -> capability registry + trusted evaluation catalog
  -> correction and capability replay
  -> autonomous_learning
  -> isolated evaluation + safety replay
  -> promotion_manager
  -> observe + reward + ledger
  -> retain or rollback
  -> L5 readiness assessment
```

Key properties:

- **One state owner.** Historical experimental loops and test-only shadow
  implementations hold no competing state.
- **Fail-closed catalog.** Dynamic evaluators load only from trusted installed
  entry points; data files, database rows, and JSON payloads cannot register
  executable evaluation logic. No trusted catalog means dynamic selection stops
  with `catalog_not_configured` — it does not improvise.
- **Machine-gated code evolution.** Automatic local patches bind to one
  repository state, an allowlist, complete file digests, and focused
  verification commands (`compileall` / targeted `pytest`). Authority comes
  exclusively from a deployment-controlled environment policy — proposals and
  payloads cannot grant it. Interrupted applies recover recorded state or
  quarantine ambiguity; they never retry a prior patch.
- **Evidence-bound maturity.** Package versions, hosts, and models are context
  — never capability identity. Maturity moves only through replay, acceptance,
  observation, and independent readiness evidence bound to the deployed commit.

### Current closure limits

Stated plainly, because overstated autonomy is worse than none:

- L5 readiness is never claimed from service health alone.
- Automatic commit and production deployment default to **off** and need their
  own explicitly enabled machine policies plus deployment evidence.
- Knowledge refresh coordinates concurrent workers inside one atomic
  transaction; it is not a distributed scheduler or parallel ledger.
- Missing pieces (unmigrated historical records, unmeasured performance
  budgets) remain explicit work items rather than silent assumptions.

The [production closure review](docs/audit/l5-v3-production-closure-2026-08-22.md)
documents exact identity, counts, and remaining limits for the current profile.

## Paper knowledge closure

PDF intake archives content-addressed raw files, canonical UTF-8 text, and an
immutable parser manifest; hashes are re-verified before extraction. Malformed,
image-only, or unparseable documents stay explicitly blocked — never silently
converted into empty knowledge. Compiled pages retire and recompile only from
still-active, non-conflicted claims with verified provenance, under atomic
source-version-coordinated refresh plans.

## Development

During iterative work, run only the directly affected behavior suites, then:

```bash
python -m compileall -q eimemory
git diff --check
```

Do not treat full-suite collection as the default verification step for a local
change; release-baseline validation is a separate operational decision. Tests
are organized by behavior and production boundary. See
[CONTRIBUTING.md](CONTRIBUTING.md).

Production deployment uses immutable releases and user-level systemd services:

```bash
deploy/install_immutable_release.sh <full-40-character-commit>
```

After installation, verify RPC health identity, the current-release symlink,
managed services, and task-specific closure evidence. See
[Deployment](docs/deployment.md) and [systemd templates](deploy/systemd/README.md),
plus the [Operations runbook](docs/operations.md).

## Documentation

| Document | Contents |
| --- | --- |
| [Quick Start](docs/QUICKSTART.md) | Guided first session |
| [Architecture](docs/architecture.md) | Execution boundaries and data flow |
| [Module map](docs/modules.md) | Complete package inventory |
| [Deployment](docs/deployment.md) | Immutable releases, systemd, health gates |
| [Operations](docs/operations.md) | Runbooks and diagnostics |
| [Evaluation](docs/evaluation.md) | Acceptance runs and catalogs |
| [Comparison](docs/COMPARISON.md) | How this differs from vector stores and RAG helpers |
| [L5 roadmap spec](docs/l5-roadmap-spec.md) | Readiness axes and maturity definitions |
| [Changelog](CHANGELOG.md) | Release history |
