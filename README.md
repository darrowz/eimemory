---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'bc7a2425-6837-4b53-b1dd-740bf81f9428'
  PropagateID: 'bc7a2425-6837-4b53-b1dd-740bf81f9428'
  ReservedCode1: '3ace922e-4b8b-4f43-9332-4696fdee4417'
  ReservedCode2: '3ace922e-4b8b-4f43-9332-4696fdee4417'
---

<h1 align="center">eimemory</h1>

<p align="center">
  <strong>Local-first memory and evidence-gated learning for long-running AI agents.</strong>
</p>

<p align="center">
  Durable context · Scoped hybrid recall · Reviewed knowledge · Replay, promotion and rollback
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#why-eimemory">Why eimemory</a> ·
  <a href="#runtime-integrations">Integrations</a> ·
  <a href="#current-validation-status">Validation status</a> ·
  <a href="#documentation">Docs</a>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
  <img alt="Python 3.11 or newer" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Package version 1.14.60" src="https://img.shields.io/badge/version-1.14.60-blue">
  <img alt="Platform: Linux and macOS" src="https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey">
</p>

<p align="center">
  <img src="docs/assets/eimemory-github-hero.png" alt="Illustration of durable memory, retrieval, evaluation and governance" width="720">
</p>

`eimemory` preserves decisions, preferences, corrections, incidents, outcomes,
knowledge and capability evidence across agent sessions. It retrieves context
through CLI, Python, authenticated RPC and host adapters, then uses evaluation
and governance to decide whether a proposed improvement can advance.

**Latest reported production acceptance:** version **1.14.57 / `eab88240`**
deployed successfully and nightly execution succeeded. **Formal recall quality
has not passed, and the capability-evolution loop remains incomplete.** Known-item
smoke passed 10/10; that result does not certify natural queries or all colleagues.
See the [receipt-based acceptance status](docs/acceptance-status.md).

## Why eimemory?

- **Durable memory.** JSONL records, SQLite projections, provenance and recovery
  tools preserve operational context across sessions.
- **Scoped hybrid recall.** Lexical, graph, quality and recency signals select
  task-relevant context. Optional semantic retrieval requires an external
  OpenAI-compatible embedding API; without it, lexical and graph retrieval remain
  available. Optional [semantic admission](docs/deployment/semantic-admission.md)
  adds reranking, no-evidence decisions and separate positive/negative evaluation.
- **Reviewed knowledge.** Paper and URL intake retain raw artifacts, canonical
  text and parser manifests. Compiled claims and pages retain their provenance;
  malformed or unverifiable sources stay blocked.
- **Bounded learning.** Scheduled analysis turns weak signals and failures into
  hypotheses and candidate improvements. Isolated evaluation, evidence-bound
  replay, machine policy, observation and rollback govern promotion.
- **Explicit evidence.** Capability maturity is assessed by exact revision,
  provider binding, owner scope and deployed identity. Process health, smoke,
  memory benchmarks and formal business acceptance remain separate results.

The core is framework-agnostic and has no mandatory third-party Python runtime
dependencies. Optional PDF parsing, PostgreSQL, embedding services, model review
and host adapters add their own dependencies, costs and data boundaries.

## Quick start

Python 3.11+ is required. Install the source checkout in an isolated environment:

```bash
git clone https://github.com/darrowz/eimemory.git
cd eimemory
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .

eimemory init
eimemory ingest "Be concise and direct" --title "Communication style"
eimemory recall "How should this agent reply?"
eimemory learn cycle --dry-run
eimemory doctor --json
```

For a reproducible install, check out a reviewed commit before installing.
Production uses [immutable releases](docs/deployment.md), rather than editable
source installs. The [Quick Start](docs/QUICKSTART.md) explains store paths, RPC
and evaluation; the [FAQ](FAQ.md) covers common questions.

```python
from eimemory import Runtime

runtime = Runtime.create(root="./data")
runtime.memory.ingest(
    text="Deploy only after tests and health checks pass.",
    title="Release rule",
    memory_type="fact",
    scope={"agent_id": "main", "workspace_id": "default"},
)
bundle = runtime.memory.recall(
    query="What is the release rule?",
    scope={"agent_id": "main", "workspace_id": "default"},
)
```

Use the public `Runtime`, RPC or adapter contracts. Keep runtime data and
credentials outside the source checkout.

## How it fits together

```text
CLI / Python Runtime / authenticated RPC / host adapter
  -> ingest, recall, verified outcome
  -> durable records + indexes + memory graph
  -> scoped retrieval and evidence assembly
  -> hypotheses + trusted evaluation + governance
  -> gated promotion + observation + ledger
  -> retain or rollback
```

| Plane | Main packages | Responsibility |
| --- | --- | --- |
| Data | `models`, `storage`, `raw`, `knowledge` | Records, artifacts, provenance, indexes and compiled knowledge |
| Recall | `recall`, `retrieval`, `embeddings`, `scoring` | Candidate generation, filtering, ranking and diagnostics |
| Control | `capabilities`, `experience`, `evaluation`, `governance` | Capability contracts, evidence, replay, promotion and rollback |
| Integration | `api`, `adapters`, `ei_bridge`, `cli`, `ops` | Public APIs, host hooks, RPC and operations |

See [Architecture](docs/architecture.md) and the [Module map](docs/modules.md)
for ownership and execution boundaries.

## Runtime integrations

Host adapters share the `agent.runtime.v1` lifecycle contract: recall, durable
capture, verified outcome and status. Authority is `per_channel`; Codex and
Hermes use separate channel scopes such as `embodied::channel::codex` and
`embodied::channel::hermes`. There is no implicit cross-channel recall.

| Host | Integration | Guide |
| --- | --- | --- |
| Codex | Session hooks and four MCP tools | [Plugin](integrations/codex/eimemory/README.md) |
| Hermes Agent | Native memory provider and host hook bridge | [Provider](integrations/hermes/eimemory/README.md), [hooks](integrations/hermes/eimemory_hook/README.md) |
| OpenClaw | Optional external plugin with eight lifecycle hooks | [Bridge](integrations/openclaw/eimemory-bridge/README.md) |
| eibrain / custom agents | Public Python facade and bounded HTTP/RPC | [Operations](docs/operations.md), [architecture](docs/architecture.md#integration-plane) |

OpenClaw is optional. The immutable installer accepts
`EIMEMORY_OPENCLAW_ADAPTER=auto|enabled|disabled` (default `auto`); an enabled but
incomplete integration fails closed. See the [systemd guide](deploy/systemd/README.md).

Remote clients configure `EIMEMORY_RPC_URL` and `EIMEMORY_RPC_TOKEN` outside
tracked files. Hook failures are bounded and fail-open for host availability;
persistence, trusted outcomes and promotion remain fail-closed for trust decisions.
Task attestation requires a separate host producer boundary.

```bash
eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
curl -fsS http://127.0.0.1:8091/health
```

Health is unauthenticated; non-health RPC methods require the configured auth
and attestation policy. See the [RPC security runbook](docs/operations.md#rpc-security).

## Governed learning boundary

One production flow owns learning state:

```text
scoped outcomes + reviewed knowledge + adapter advertisements
  -> capability registry + sealed evaluation catalog
  -> hypotheses + replay + isolated evaluation
  -> autonomous_learning + promotion_manager
  -> observation + reward + ledger + rollback
  -> readiness assessment
```

- Executable evaluators come from trusted installed entry points. JSON, YAML,
  database records and advertisements cannot register evaluation code.
- A missing catalog, ambiguous hypothesis, absent binding or incomplete evidence
  remains a blocking gap.
- Code patches require exact repository identity, allowed files, digests and
  focused verification. Commit and production deployment default to off and
  require separate deployment-controlled machine policies and receipts.
- Learning cannot grant authority for spending, external sends, credentials,
  private-data export or irreversible deletion.
- L5 v3 tracks loop maturity, capability readiness, adapter readiness and
  deployment assurance separately. Historical profile readiness does not
  certify a different release or current business recall quality.

## Current package status (1.14.60, mainline)

Research review now automatically discovers the service user's installed Hermes
runtime and reads its current provider, model and credential configuration.
It supports installation-bound launchers, Python console installs and legacy
source virtual environments without a fixed host path or model. Explicit review
commands remain available. SDK responses must identify the actual model, and
failed or tool-calling responses remain unavailable.
The requested model selection is `gpt-6.1-sol` with `low` reasoning; the bridge
reads it from the active Hermes profile and delegates reasoning wire parameters
to the installed provider adapter.

Deployment tests isolate the service account's OS home and system PATH. They
separately require rejection without a service Hermes installation and discovery
of an installed runtime with its current configuration, even when the deployment
controller uses a different HOME or route. Changing only the test process's HOME
does not isolate the account lookup used by deployment preflight.

Earlier repairs add durable research-review diagnostics and check managed
review configuration before switching releases. They also include
configured model review, strict review validation, separate catalog and retrieval evaluation, persisted
not-run memory receipts, and exact binding diagnostics with consistent time
cutoffs. Package and Codex/Hermes plugin versions are aligned.

The code is on mainline. It has not been deployed or re-evaluated in production.
See [1.14.60 changes](CHANGELOG.md#11460) and
[workflow repair details](docs/acceptance-status.md#mainline-workflow-repairs).

## Current validation status

A later operator screenshot and readback report **1.14.58 / `534bb2f0`**,
nightly receipt `ref_4cc83efb2f5d`:
execution failed with exit 1 at `research_closure_review`, formal cases remain
0/15 with two pending, memory was not evaluated and ten evolution items await
hypotheses. The operator read back 15 research records with
`research_review_llm_unconfigured`: no research-specific or shared review
command was configured. The durable nightly receipt omitted per-record review
errors. This diagnosis comes from the detailed records, not the screenshot alone.

The earlier operator-supplied receipt readback covers **1.14.57 / `eab88240`**;
these results are bounded to that run.

| Check | Reported result | Meaning |
| --- | --- | --- |
| Deployment and nightly execution | Passed; `execution_ok=true`, exit 0 | The deployed workflow completed |
| Known-item smoke | 10/10; hit@1, hit@5 and MRR 1.00; P95 340.8 ms | Those fixed known items were found |
| Formal recall gate | **Blocked**: `recall_quality_evidence_incomplete` | 0/15 accepted cases; label trust and release authorization unverified |
| Memory benchmark | **Not evaluated**: `memory_eval_dataset_empty` | A code-capability pass cannot replace a memory benchmark |
| Dynamic evolution | **9 waiting items**: `hypothesis_missing_or_ambiguous` | The evolution loop is incomplete |
| Research model review | **Unavailable**: `codex` executable missing | No successful model-review evidence |

Smoke noise **0.80** and precision@3 **0.333** are observations, not formal
business conclusions. The formal standards remain noise **≤0.40** and
precision@3 **≥0.60**; missing evidence blocks the gate before formal scoring.
Two pending cases do not count as accepted cases.

Mainline **1.14.59** adds bounded review failure reasons to durable nightly
receipts, including command/bridge failures and review validation errors, and
blocks managed deployment before switching when the durable review route is missing or invalid.
Mainline **1.14.60** adds automatic Hermes review routing and configuration
discovery to deployment preflight. These changes keep all formal quality gates.
The 1.14.58 repairs cover model-command routing, memory/catalog separation,
persisted not-run evidence and binding diagnostics. No new production acceptance
or relaxed threshold is implied by these package changes.
See [acceptance status and remaining work](docs/acceptance-status.md).

## Development and deployment

Install `pytest` separately and run the suites affected by the change. Then:

```bash
python -m compileall -q eimemory
git diff --check
```

Release verification and production acceptance are separate checks. See
[Contributing](CONTRIBUTING.md). Production installation uses an exact commit:

```bash
deploy/install_immutable_release.sh <full-40-character-commit>
```

Verify the current-release symlink, RPC identity, managed services and persisted
acceptance evidence after installation. Health or a systemd exit code alone
cannot close the recall gate.

## Documentation

| Document | Contents |
| --- | --- |
| [Documentation index](docs/README.md) | Guides by task and historical evidence boundaries |
| [Quick Start](docs/QUICKSTART.md) | Install, ingest, recall and local diagnostics |
| [FAQ](FAQ.md) | Configuration, integrations and readiness questions |
| [Acceptance status](docs/acceptance-status.md) | Latest reported receipts, unchanged standards and remaining work |
| [Architecture](docs/architecture.md) / [Module map](docs/modules.md) | Data flow, public surfaces and ownership |
| [Evaluation](docs/evaluation.md) | Smoke, memory benchmarks, catalogs and formal acceptance |
| [Deployment](docs/deployment.md) / [systemd templates](deploy/systemd/README.md) | Immutable releases, services and recovery |
| [Operations](docs/operations.md) | Channel setup, security and live verification |
| [Comparison](docs/COMPARISON.md) | Choosing and combining memory approaches |
| [L5 v3 architecture](docs/architecture.md#dynamic-l5-v3) | Readiness axes and maturity contracts |
| [Changelog](CHANGELOG.md) | Released behavior and historical changes |

Dated audit and plan documents preserve their original evidence. Read them as
historical records; use current release-bound receipts for acceptance.

## Maintainer contact

Maintainer: [darrowz](https://github.com/darrowz)  
Email: [shelinedouville@gmail.com](mailto:shelinedouville@gmail.com)

## License

[MIT](LICENSE).

> AI生成
