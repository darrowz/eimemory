# eimemory

`eimemory` is a local-first memory and governed-learning runtime for long-running
AI agents. It stores durable records, builds recall views, turns source material
into reviewed knowledge, records task outcomes, and promotes learned behavior
only after replay, safety, and rollback checks.

The production design has one state owner: the governance pipeline. Historical
experimental loops and test-only shadow implementations are not part of the
runtime.

## What eimemory provides

- Durable JSONL records with SQLite projections and repairable indexes.
- Scoped lexical, vector, graph-aware, and proactive recall.
- Source intake, review, canonical PDF evidence, knowledge compilation, and
  conflict-safe recall projection.
- Outcome traces, correction replay, policy evaluation, and capability ledgers.
- Governed autonomous learning with isolated evaluation, safety replay,
  promotion gates, observation, reward, rollback, and bounded automatic local
  code evolution.
- Runtime adapters for Codex, OpenClaw, Hermes, and eibrain.
- Production RPC health, deployment identity, diagnostics, and systemd jobs.

## Architecture at a glance

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
| Recall | `recall`, `retrieval`, `embeddings`, `scoring` | Candidate generation, filtering, ranking, and quality |
| Control | `experience`, `evaluation`, `governance` | Outcomes, replay, learning, promotion, readiness, rollback |
| Integration | `api`, `adapters`, `ei_bridge`, `cli`, `ops` | Public APIs, host lifecycle hooks, RPC, and operations |

See [Architecture](docs/architecture.md) for the execution boundaries and
[Module map](docs/modules.md) for the complete package inventory.

## Installation

Python 3.11 or newer is required.

```bash
python -m pip install -e .
eimemory init
```

Install the optional PDF parser when paper sources are part of the workload:

```bash
python -m pip install -e ".[pdf]"
```

Runtime state is written below `EIMEMORY_ROOT`; it must not be stored in the
source checkout in production.

## Quick start

```bash
# Store a durable preference.
eimemory ingest "Be concise and direct" --title "Communication style"

# Recall relevant memory.
eimemory recall "How should this agent reply?"

# Inspect learning without applying changes.
eimemory learn cycle --dry-run

# View the capability ledger.
eimemory learn ledger --limit 50

# Run local diagnostics.
eimemory doctor --json
```

Use `eimemory <command> --help` for command-specific options. The supported
top-level command families are:

- Storage and memory: `init`, `ingest`, `recall`, `export`, `import`, `backup`,
  `storage`, `migrate`, `rebuild-sqlite`.
- Knowledge intake: `paper`, `source`, `intake`, `brief`, `nightly`.
- Learning and evaluation: `experience`, `learn`, `governance`, `evolve`,
  `eval`, `quality`, `patch`.
- Operations and integration: `serve-eibrain-rpc`, `status`, `doctor`, `ops`,
  `openclaw-hook`, `ei-bridge`, `persona`, `emergency-stop`.

## Python API

```python
from eimemory import Runtime

runtime = Runtime.create(root="./data")

record = runtime.memory.ingest(
    text="Deploy only after tests and health checks pass.",
    title="Release rule",
    scope={"agent_id": "main", "workspace_id": "default"},
)

bundle = runtime.memory.recall(
    query="What is the release rule?",
    scope={"agent_id": "main", "workspace_id": "default"},
)
```

`Runtime` is the public in-process facade. Integrations should use it, the RPC
service, or an adapter contract instead of reaching into storage internals.

## Runtime integrations

- Codex: hook and MCP surfaces under `eimemory.adapters.codex`, with the public
  recall, durable-capture, verified-outcome, and status contract.
- OpenClaw: eight lifecycle hooks and the official bridge plugin. Its only
  model-visible bridge tool is runtime status; lifecycle capture and recall stay
  hook-mediated. `eimemory eval openclaw-e2e` is an operator diagnostic, not a
  second model-tool surface.
- Hermes: provider core and host-context authentication used by the official
  plugin packages, with the same four public memory operations as Codex.
- eibrain: SDK plus bounded HTTP/RPC server and bridge agent.

All host adapters implement the `agent.runtime.v1` lifecycle contract. Remote
clients read `EIMEMORY_RPC_URL` and `EIMEMORY_RPC_TOKEN`; credentials must stay
outside tracked configuration. Canonical channel identities include
`embodied::channel::codex` and `embodied::channel::hermes`, with isolation
reported as `per_channel`. Recall and outcome hooks are deliberately fail-open
for host availability while persistence and promotion gates remain fail-closed
for trust decisions.

The eibrain RPC service can be started locally with:

```bash
eimemory serve-eibrain-rpc --host 127.0.0.1 --port 8091
curl http://127.0.0.1:8091/health
```

Non-health RPC methods require the configured authentication and attestation
policy. Do not expose the service on a non-loopback address without a strong
private credential.

## Governed learning boundary

There is one production learning flow:

```text
signals and outcomes
  -> correction and capability replay
  -> autonomous_learning
  -> isolated evaluation + safety replay
  -> promotion_manager
  -> observe + reward + ledger
  -> retain or rollback
  -> L5 readiness assessment
```

`governance.autonomous_learning`, `governance.autonomous_evolution`, and
`governance.promotion_manager` own this flow. A separate experimental scheduler,
promotion state machine, or shadow safety stack must not write competing state.

Service health does not by itself prove learning closure or L5 readiness. Those
claims require release-bound replay, live acceptance, observation, and readiness
evidence for the same deployed commit.

### Automatic local code evolution

A code-capable learning goal can use a structured replay patch, an injected
runtime proposer, or a configured command proposer. Configure the latter with
`EIMEMORY_CODE_PATCH_LLM_COMMAND` as a non-empty JSON argv array (with
`EIMEMORY_LLM_COMMAND` as its global fallback); an integration may instead
provide a runtime `code_patch_proposer` or `autonomous_code_proposer`. The
proposal bridge binds a ready change to one repository state, an allowlist,
complete file updates, a unified diff, file/base-state digests, and focused
verification commands. A missing or malformed proposal is persisted as blocked
evidence; it is not silently converted into an SOP or an unbounded shell action.
Generated-patch verification accepts only argv-shaped `python -m compileall`
targets or focused `python -m pytest -q tests/...` targets. It rejects a broad
full-suite command, shell, Git, network tools, and `python -c`; release-baseline
validation is a separate operational decision.

`eimemory learn cycle --apply` has no human approval queue for this local write
path. Once the existing isolated evaluation, replay, safety, and preflight gates
pass, the promotion manager can apply the bounded patch and run its declared
verification. It writes a transaction record before the first file change, rolls
back failed verification, and starts each apply-enabled learning or evolution
cycle by recovering known interrupted writes or quarantining an ambiguous state.
The learning/evolution report exposes this as `code_apply_recovery`; a
non-applying cycle reports it as skipped. Recovery never retries or reapplies a
prior patch: it only restores recorded old content or quarantines ambiguity.

This automatic path does **not** imply an automatic commit or production deploy:
both default to off and require an explicit patch or environment decision. It
also depends on an available code proposer; no configured provider means the
candidate remains explicitly blocked.

## Paper knowledge closure

PDF intake archives a content-addressed raw PDF, canonical UTF-8 text, and an
immutable parser manifest below the runtime root. The runtime verifies the
manifest, both references, and both content hashes again before extraction or
refresh; a caller-provided text path alone is not trusted. A malformed,
image-only, oversized, or unparseable document remains explicitly blocked; it is
never converted into an empty body. When claim reconciliation flags a compiled
page, the runtime retires the affected operational-memory projections first,
then recompiles only from still-active, non-conflicted claims with a verified
canonical source. Missing source text or an unresolved conflict leaves the page
blocked and unavailable to projection.

The refresh consumer is not a second extraction pipeline: it recompiles from
surviving reviewed claims after proving source provenance. Re-extracting a fresh
claim set from a changed PDF, then reviewing and reconciling it, remains a
separate intake/knowledge closure step.

## Current closure limits

- L5 is not claimed by a healthy process, module inventory, or focused test
  result. It still needs release-bound replay, live acceptance, observation, and
  independent readiness evidence for the deployed commit.
- Cross-worker source-version coordination for concurrent refreshes is not a
  replacement for the atomic single-store refresh transaction and remains a
  follow-up closure item.
- Automatic code application is local and machine-gated; absent proposer
  configuration, automatic commit, and automatic production deployment are not
  implied capabilities.

## Development

During iterative work, run only the directly affected behavior suites, followed
by syntax and diff checks. Do not use the full test collection as the default
verification step for a local change or generated patch; release-baseline
validation is a separate decision.

```bash
python -m compileall -q eimemory
git diff --check
```

Tests are organized by behavior and production boundary. Modules that have no
production entry, public contract, or external integration should be removed
together with tests that only preserve that dead implementation.

## Production deployment

Production uses immutable releases and user-level systemd services. Deploy from
the authoritative checkout and bind the release to a full commit:

```bash
deploy/install_immutable_release.sh <full-40-character-commit>
```

After installation, verify the RPC health identity, current release symlink,
managed services, host integrations, and task-specific closure evidence. See
[Deployment](docs/deployment.md) and [systemd templates](deploy/systemd/README.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Module map](docs/modules.md)
- [Deployment](docs/deployment.md)
- [Operations runbook](docs/operations.md)
- [Evaluation guide](docs/evaluation.md)
- [Changelog](CHANGELOG.md)
