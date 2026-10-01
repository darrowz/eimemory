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
  <a href="LICENSE"><img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green"></a>
</p>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="Version" src="https://img.shields.io/badge/version-1.14.30-blue">
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
  Semantic retrieval is an optional capability: it requires configuring an
  external OpenAI-compatible embedding API, and without it recall degrades to
  lexical and graph-aware hybrid retrieval.
  Optional [semantic admission](docs/deployment/semantic-admission.md) adds a
  loopback-only cross-encoder, no-evidence decisions, revision-fenced incremental
  PostgreSQL maintenance, and separate positive/negative acceptance metrics.
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
durable capture, verified outcome, and status. Authority is `per_channel`:
Codex uses `embodied::channel::codex`, Hermes uses
`embodied::channel::hermes`, and recall never crosses those scopes.

| Host | Surface |
| --- | --- |
| Codex | Hook + MCP surfaces (`eimemory.adapters.codex`) |
| OpenClaw (optional) | Eight lifecycle hooks + configured external bridge plugin |
| Hermes | Provider core + host-context authentication (official plugin packages) |
| eibrain | SDK + bounded HTTP/RPC server and bridge agent |

OpenClaw is not a dependency of the core memory/RPC runtime. Deployment uses
`EIMEMORY_OPENCLAW_ADAPTER=auto|enabled|disabled` (default `auto`); a host without
an OpenClaw installation, configuration or service does not enable that adapter.
An enabled but incomplete integration fails closed. The bridge loads from its
explicit external plugin path and uses the public authenticated Gateway SDK,
without bundled-plugin impersonation or upstream OpenClaw modifications.
See [deployment and recovery](deploy/systemd/README.md) for the operating modes.

Remote clients use `EIMEMORY_RPC_URL` / `EIMEMORY_RPC_TOKEN`; credentials stay
outside tracked configuration. Recall and outcome hooks are deliberately fail-open
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
- **Source-faithful maintenance.** Known user-requested repairs use the same
  strict verification, deployment and 48-hour observation machinery under a
  one-shot machine policy. Their actual provenance remains visible and never
  earns autonomous system-discovery credit in the product L5 assessment.
- **Evidence-bound maturity.** Package versions, hosts, and models are context
  — never capability identity. Maturity moves only through replay, acceptance,
  observation, and independent readiness evidence bound to the deployed commit.

## Current package status (1.14.30, unreleased)

1.14.30 keeps L1 extraction fail-closed when the configured extractor is missing or returns an unusable result, and can store reusable `fact` atoms. Conflict updates may supersede only same-scope, same-source targets the judge was shown. Legacy zero-atom completions can be retried explicitly; a completion marker alone is no longer treated as an authoritative v2 extract. Raw retrieval gives an explicit Chinese correction a ranking boost so a later correction can outrank a lexically stronger stale statement. Local regressions do not certify production recall recovery or a historical backfill.

## Current package status (1.14.29, unreleased)

1.14.29 keeps latest-task selection within the existing relevance score band before ordering by event time, and binds project state/history/constraint assertions to their sentence or bounded adjacent project/version heading. Responsibility questions avoid task-state routing, and project identity parsing no longer consumes release-token tails. Selector regressions enforce local admission with post-delivery quality evaluation. Delegated factual results, explicit history and unknown-attribute semantic handling remain available. Local regressions do not certify current production state or the original acceptance-requirements paraphrase failure. See `docs/audit/recall-1.14.29-2026-10-01.md`.

1.14.26 separates retrieval from model quality evaluation: caller evidence review, cross-encoder scoring and raw model reranking no longer run on the synchronous recall path. Authority checks, local ordering, deduplication and delivery bounds remain. Ordinary results carry no model-verified proof. The existing bounded post-delivery semantic monitor continues to assess recorded deliveries; it does not generate parent-span proofs or cover standalone SDK recalls without a delivery ledger. See `docs/audit/recall-posthoc-quality-2026-09-30.md`.

1.14.24 keeps Hermes proactive recall inside the host's fixed 8s prefetch window: the server bounds recall and verification to the window minus a margin (`host_window_capped` / `host_window_exhausted` diagnostics), and the Hermes provider uses a dedicated 7.6s proactive client so a slow call cannot make Hermes skip the provider on later turns. `sync_turn` no longer runs a full memory recall as a create-safety probe (it could never match a target and cost seconds per turn). A real `hermes.task_end` producer closes a Hermes turn automatically when Hermes finishes it and the turn holds passed host-attested tool receipts; receipt-verified Hermes channel traffic now counts toward release lineage, acceptance-generated cases never do (`docs/audit/hermes-task-end-scope-policy-2026-09-30.md`). A nightly capability hypothesis producer derives hypotheses only from real blocked gaps with an already registered, applicable knowledge link, reports every skip, and is revocable. The retired `hongxin` gateway is no longer an expected gateway.

1.14.22/1.14.23 add a daily verified backup job (`eimemory-backup.timer`: online SQLite backups with integrity check, verified record export, state and config archive, sha256 manifest, keep 5). It pools the Luna verifier bridge process for caller-assisted recall, so each verification pays only provider time instead of about 2.3s of interpreter, import and client setup. It also stops the nightly from failing when dynamic capability evolution is only waiting for a capability hypothesis that no producer has proposed yet: that case is now an evidence wait with diagnostics, and any real error still fails. See `docs/audit/ops-backup-verifier-pool-nightly-2026-09-30.md`.

Proactive session dedupe now counts only what the model has actually seen (delivered items or items in an open decision), so control-suppressed and never-injected memories stay eligible. The explicit `eimemory_recall` tool uses its own client with a timeout covering the server's full completion bound (default 30s), and caller-assisted verification is capped by `EIMEMORY_RECALL_VERIFIER_TIMEOUT_SECONDS` (default 12s). Hermes deploy acceptance times the official recall tool call once, never retried. See `docs/audit/recall-delivery-and-latency-2026-09-30.md`.

Self-evolution is auto-authorized. When a release changes evolution-engine paths that the ordinary deployment receipt does not cover, release lineage now accepts a signed automatic authorization (`code-evolution-auto-authorization.v1`, authority `code-evolution-auto-authorizer`, never an operator identity) in place of a strict code-evolution transaction receipt. It is minted during lineage recording, bound to the exact deployment receipt, ancestor, changed domains and paths, signed with the evidence-receipt keyring, revocable (`eimemory learn code-evolution-auto-authorization-revoke`) and controlled by `EIMEMORY_CODE_EVOLUTION_AUTO_AUTHORIZATION` (default on) and the code-evolution kill switch. All other lineage domain gates are unchanged. See `docs/audit/code-evolution-auto-authorization-2026-09-30.md`.

Hermes delivery acknowledgement is now bound to the prefetch actually injected into the current turn. Hermes runs `pre_llm_call` before `prefetch_all`, so the adapter no longer acknowledges there; `post_llm_call` acknowledges only citations that appear in this turn's model-facing user message (`api_content` sidecar or text part) or are cited by the assistant, and records `used` only for delivered citations. A context that was merely returned to Hermes is never counted as delivered. See `docs/audit/hermes-delivery-ack-2026-09-30.md`.

The semantic relevance monitor now also judges Hermes `research.task` recall decisions, with the same verifier and prompt as `memory.recall`, in every exact channel scope, and records `decision_surface` and `channel` provenance. A new nightly step, `semantic_relevance_monitor`, runs before label auto-review. See `docs/audit/semantic-relevance-research-task-2026-09-30.md`.

Production recall labels are now auto-reviewed: pending production-query cases whose delivered candidates are marked relevant by a validated semantic observation **and** backed by a verified parent-span proof or host `used` feedback are accepted under a separate, signed `auto_review` authority (policy flag `EIMEMORY_PRODUCTION_RECALL_AUTO_REVIEW`, default on; revocable). Cases without agreeing evidence stay pending with a recorded reason. The 15-label threshold is unchanged. See `docs/audit/production-recall-auto-review-2026-09-30.md`.

Residual closure of governance deep-audit open/partial rows (A1/A2/god-file/CE-3/dead symbols/S6 lease). See `docs/audit/GOVERNANCE-AUDIT-REMEDIATION-2026-09-22.md` (no Open/partial rows). Hardening absorb notes remain in `docs/audit/ABSORB-HARDENING-2e57f59-2026-09-23.md`.

Round-3 audit remediation closed (S1/budget/SEC-1/B1/ARCH-01 + P1/P2 items). See `docs/audit/ROUND3-REMEDIATION-2026-09-22.md`.

As of 2026-09-22 ops-acceptance wave on `master`:

- **Nightly / SCH-01:** `replay_rules` returns a dict; empty successful replays no longer false-fail aggregation (`step_result_not_dict`).
- **Eval:** expected-empty + got-empty is pass; threshold `0.0` no longer contradicts failing sample labels.
- **Deploy:** colleague gateway discovery refreshes Hongxin/Hongtai/Xiaomage(/Hongrui) runtime units with hermes/openclaw.
- **Release impact:** FAQ/LICENSE/CONTRIBUTING ignored; `eimemory/contracts` classified (not `unknown_production`).
- **Closing loop:** evidence-wait (`证据不足` / tip_safety not_ready / lineage mismatch awaiting samples) is `evidence_waiting`, not `failure_detected`.
- **Identity:** stamp on ingest; nightly repair is scoped and skips fresh writes.
- **Real-query evidence:** pending capture with operator acceptance or (since 1.14.16) evidence-bound `auto_review` acceptance; historical delegated labels remain stored but do not count as gold.
- **Not claimed here:** production Hongxin deploy, L5 maturity from health alone.

### Current closure limits

Stated plainly, because overstated autonomy is worse than none:

- L5 readiness is never claimed from service health alone.
- Automatic commit and production deployment default to **off** and need their
  own explicitly enabled machine policies plus deployment evidence.
- Knowledge refresh coordinates concurrent workers inside one atomic
  transaction; it is not a distributed scheduler or parallel ledger.
- PERF-05 narrow-index split and unvalidated lexical prune remain open; see the PERF landing note.
- Promotion mid-flight reconciliation and watch orphan **scan** landed; full effect-owner digest repair and production health identity binding remain open.

The [production closure review](docs/audit/l5-v3-production-closure-2026-08-22.md)
and [2026-09-22 remediation status](docs/audit/REMEDIATION-STATUS-2026-09-22.md)
document exact identity, counts, and remaining limits for the current profile.

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
| [Remediation status (2026-09-22)](docs/audit/REMEDIATION-STATUS-2026-09-22.md) | Audit closures + residuals |
| [PERF landing (2026-09-22)](docs/audit/PERF-LANDING-2026-09-22.md) | Recall P0/P1 metrics |

## Maintainer contact

Maintainer: [darrowz](https://github.com/darrowz)  
Email: [shelinedouville@gmail.com](mailto:shelinedouville@gmail.com)

## License

[MIT](LICENSE) — free to use, modify, and ship, including commercially.

> AI生成
