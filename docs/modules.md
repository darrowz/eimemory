# eimemory Module Map

This document is the authoritative ownership map for the `eimemory` package.
It records which submodules are production owners, which are integration
surfaces, and where duplicated responsibilities must not be reintroduced.

## Public and integration surfaces

| Module | Status | Responsibility |
| --- | --- | --- |
| `eimemory` | Public | Exports `Runtime` and `__version__`. |
| `api.memory` | Active | Ingest, recall, feedback, and memory-facing operations. |
| `api.evolution` | Active | Observation, evolution, and quality repair facade. |
| `api.runtime` | Active | Composition root and public runtime facade. |
| `cli.main`, `cli.doctor` | Entry | Operator CLI and diagnostics. |
| `adapters.runtime.*` | Active | Shared channel, auth, redaction, HTTP, receipt, and service contracts. |
| `adapters.codex.*` | Entry | Codex hooks and MCP surface. |
| `adapters.openclaw.*` | Entry | OpenClaw lifecycle hooks, bridge-status surface, QMD compatibility/export, and task contracts. |
| `adapters.hermes.*` | Entry | Hermes provider core, registry, and private host context. |
| `adapters.eibrain.*` | Public/entry | eibrain SDK and bounded RPC service. |
| `ei_bridge.*` | Active/entry | Agent and channel routing, monitoring, audit, and OpenClaw runtime bridge. |
| `llm.command_client` | Active | Optional bounded LLM command client. |
| `llm.openclaw_adapter` | Entry | Command adapter launched through deployment configuration. |

## Durable data and models

| Module group | Responsibility |
| --- | --- |
| `models.records` | Canonical record envelope, scope, time, and recall bundle. |
| `models.claim_cards`, `entity_records`, `relation_records` | Typed knowledge evidence. |
| `models.paper_sources`, `paper_extracts`, `knowledge_pages` | Paper and compiled knowledge models. |
| `models.memory_edges`, `recall_views`, `source_partitions` | Graph, recall, and partition projections. |
| `models.identity_aliases` | Bounded identity aliases and compatibility projections. |
| `storage.jsonl`, `atomic_file`, `payload_segments` | Durable append, atomic state, and segmented payloads. |
| `storage.sqlite_store`, `runtime_store` | Materialized indexes and runtime access. |
| `storage.maintenance`, `replay_buffer` | Migration, compaction, verification, and replay storage. |
| `raw.chunks`, `raw.store`, `raw.retrieval`, `raw.synthetic` | Bounded raw evidence and two-stage recall support. |

## Recall, scoring, and identity

| Module group | Responsibility |
| --- | --- |
| `recall.intent`, `recall.lexical`, `recall.indexing` | Query intent and lexical index primitives. |
| `retrieval.engine`, `contracts`, `fusion`, `proactive`, `sqlite_source` | Governed recall pipeline and diagnostics. |
| `retrieval.postgres_*` | Optional PostgreSQL and vector backend. |
| `embeddings.local` | Local embedding provider. |
| `scoring.contract`, `evaluator`, `adapters`, `thresholds`, `labels`, `reports` | Canonical score model and score reporting. |
| `identity`, `identity_ops`, `runtime_identity` | Scope normalization, repair, and release identity. |
| `judgment`, `metadata`, `events` | Shared decision, metadata, and event primitives. |

## Intake and knowledge

| Module group | Responsibility |
| --- | --- |
| `intake.connectors`, `source_discovery`, `autonomous_sources` | Source registration and discovery. |
| `intake.safe_transport`, `fulltext` | SSRF-safe retrieval and bounded text extraction. |
| `intake.pipeline`, `loop`, `packs`, `registry` | Intake orchestration and persisted candidates. |
| `intake.review`, `policy`, `closure`, `closure_review` | Review, promotion policy, and closure evidence. |
| `intake.papers.metadata`, `normalize`, `sources`, `artifacts`, `pdf_parse` | Paper identity, content-addressed PDF artifacts, canonical text, and extraction evidence. |
| `knowledge.ingest`, `extract`, `compiler`, `refresh` | Convert reviewed inputs into knowledge records and safely recompile conflicted pages. |
| `knowledge.claims`, `relations`, `pages`, `projectors`, `views` | Queryable knowledge projections; stale projections are retired before refresh. |
| `knowledge.evidence_gate`, `source_trust`, `safety` | Evidence, trust, and content safety. |
| `knowledge.synthesis`, `daily_brief` | Synthesis and operator-facing briefs. |

`intake.papers.pdf_parse` is an optional `pypdf` backend. It emits real
canonical text plus parser/page evidence into a content-addressed artifact
manifest, or explicitly blocks the source (`parser_unavailable`, `ocr_required`,
or invalid input). It never returns an empty body as evidence. `artifacts`
seals the raw PDF, canonical text, and manifest by digest and revalidates the
manifest and both blobs before a runtime reader can use them; an arbitrary
caller-supplied text reference is not reusable evidence. `knowledge.refresh`
requires that verified chain, retires stale operational projections atomically,
and only reactivates pages compiled from non-conflicted claims.

## Experience and evaluation

| Module group | Responsibility |
| --- | --- |
| `experience.bridge`, `capability_contract` | Host outcome and capability contracts. |
| `experience.outcome`, `diagnosis`, `sanitize` | Verified outcomes, diagnosis, and secret-safe persistence. |
| `evaluation.framework`, `contracts`, `metrics`, `reward` | Shared evaluation contracts and metrics. |
| `evaluation.regression_replay`, `task_replay` | Regression and task replay. |
| `evaluation.production_recall`, `production_query_dataset`, `real_query_gate` | Release-bound production recall evidence. |
| `evaluation.actionable_memory`, `livingmem` | Behavior and living-memory evaluation. |
| `evaluation.locomo`, `longmemeval`, `benchmarks`, `public_benchmarks` | Benchmark adapters. |

## Governance ownership

| Concern | Active modules |
| --- | --- |
| Learning orchestration | `autonomous_learning`, `autonomy_controller`, `learning_state`, `learning_eval`, `learning_retention` |
| Evolution | `autonomous_evolution`, `code_evolution`, `code_evolution_bridge`, `rule_evolution`, `evolution_pruner` |
| Goals and episodes | `goal_graph`, `goal_registry`, `autonomy_goal_queue`, `episode_events`, `event_graph` |
| Candidate lifecycle | `candidate_search`, `skill_candidate`, `promotion_manager`, `promotion_watch`, `rollout_lifecycle` |
| Capability evidence | `capability_contract`, `capability_attribution`, `capability_ledger`, `capability_dashboard` |
| Replay and probes | `capability_replay_*`, `capability_probe_executor`, `correction_replay`, `outcome_replay`, `policy_replay` |
| Safety | `safety_replay`, `prompt_safety*`, `change_policy`, `policy_trust`, `safety.audit`, `safety.audit_verifier`, `safety.kill_switch` |
| Evidence | `evidence_contract`, `evidence_collector`, `snapshot`, `memory_graph`, `tool_receipts` |
| Learning output | `skill_sedimentation`, `capability_distiller`, `learning_report`, `learning_dashboard` |
| Research and signals | `research_planner`, `web_learning`, `world_watchers`, `signal_intake`, `curiosity`, `thoughts` |
| Closure and L5 | `release_closure*`, `closure_rehearsal`, `live_task_acceptance`, `l5_loop`, `l5_maturity`, `l5_readiness` |
| Operator services | `console`, `serve_console`, `supervisor`, `openclaw_channel_acceptance` |

### Automatic local code evolution

`autonomous_learning` can request a structured code proposal from a replay seed,
an injected runtime proposer, or `EIMEMORY_CODE_PATCH_LLM_COMMAND` (a non-empty
JSON argv array, with `EIMEMORY_LLM_COMMAND` as global fallback). Integrations
can inject `code_patch_proposer` or `autonomous_code_proposer` instead.
`code_evolution_bridge` is the proposal-only boundary: it returns either a
bounded, state-bound unified diff or an explicit blocked result. It requires
complete file updates, an allowlist, base/subject-state and per-file digests, and
focused verification commands. It does not change a repository itself.
Generated patches may verify only through argv-shaped `python -m compileall` or
focused `python -m pytest -q tests/...` commands. Shell, Git, network tools,
`python -c`, and a broad full-suite command are rejected; release-baseline
validation is a separate operation.

`promotion_manager` owns the direct-write transaction after the normal replay,
safety, isolated-evaluation, and preflight gates pass. With an explicitly
applying cycle there is no human approval record or review queue: the machine
gate either applies the bounded local patch or blocks it. A transaction is
persisted before the first write, verification failure rolls back, and recovery
at the start of each apply-enabled learning/evolution cycle only restores known
prior content or quarantines an ambiguous state; it never retries the old patch.
The cycle report exposes the result as `code_apply_recovery` and marks it skipped
when application is disabled. Repository commit and production deployment are
disabled by default and must be explicitly enabled; a local write is not a
deployment.

## Living memory and persona

| Module group | Responsibility |
| --- | --- |
| `living.schema`, `operations`, `posture` | Living-memory metadata, updates, and action posture. |
| `persona.schema`, `state`, `store`, `context_router`, `prompt`, `correction`, `evolver` | Persisted persona state and prompt guidance. |
| `persona.cli`, `persona.evals` | Operator and evaluation entry points. |

Unused persona summary/strategy wrappers and the disconnected living temporal
helper were removed. Their supported behavior is owned by `context_router`,
`prompt`, `posture`, and the evaluation layer.

## Operations and configuration

| Module group | Responsibility |
| --- | --- |
| `ops.openclaw_loop`, `ops.timer_monitor` | OpenClaw loop reconciliation and timer monitoring. |
| `scheduler.jobs` | Nightly and scheduled job composition. |
| `config.defaults`, `loader`, `schema` | Configuration defaults, loading, and validation. |
| `compatibility.migration_helpers` | Explicit supported migration helpers. |
| `core.clock`, `core.ids` | Shared time and identifier primitives. |

The Python Feishu delivery-state shadow was removed; the official OpenClaw
bridge owns that schema and delivery lifecycle.

## Explicit closure limits

- A missing or invalid code proposer produces a blocked code candidate; it is
  not a substitute for an automatic code-generation capability.
- Knowledge refresh rebuilds from surviving reviewed claims after artifact
  verification. Re-extraction, review, and reconciliation of a changed source
  remain separate work.
- The atomic refresh transaction is single-store safety, not a distributed
  source-version protocol for concurrent workers.
- L5 readiness still requires release-bound live evidence; it cannot be inferred
  from the presence of these modules or a healthy service.

## Removed duplicate or zombie groups

The current unreleased cleanup removed:

- `eimemory.autonomous`: an isolated Karpathy experiment stack with no
  production entry; governance now owns learning and evolution.
- `governance.state_machine`: duplicated the active promotion lifecycle.
- `governance.held_out_split`: a fixed-path JSONL splitter superseded by current
  replay and evaluation datasets.
- `governance.evidence_first`: a test-only evidence wrapper superseded by
  evidence contracts and collectors.
- `governance.skills`: test-only skill merge/bridge code superseded by skill
  sedimentation and the eiskill registry contract.
- `governance.safety` prototypes for anomaly, circuit breaker, L3 queue,
  network proxy, outbound communication, profile promotion, and spend guard;
  active equivalents live in safe transport, safety replay, promotion manager,
  rollout policy, audit verifier, and kill switch.
- Empty, placeholder, or unreferenced helpers in `core`, `models`, `persona`,
  `living`, `intake.papers`, and `ops`.

## Review rule

Before adding a module, identify its production owner, entry point, durable
state, and verification suite. Before removing one, prove that it has no
production import, dynamic launcher, public adapter contract, or external
integration reference. Tests do not by themselves make a module production
code.
