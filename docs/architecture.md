# eimemory Architecture

`eimemory` is a local-first memory, knowledge, evaluation, and governed
self-improvement runtime. The production architecture has one learning owner:
the governance learning loop. Experimental autonomy helpers can feed evidence
into that loop, but they do not run their own production scheduler.

## Production Layers

1. **Record and storage core**
   - `eimemory.models` defines record envelopes, scopes, quality metadata, and
     stable IDs.
   - `eimemory.storage` persists append-only JSONL records and materialized
     SQLite indexes.
   - Runtime state belongs under `EIMEMORY_ROOT`, not inside the source
     repository.

2. **Recall and scoring**
   - `eimemory.raw`, `eimemory.recall`, `eimemory.scoring`, and
     `eimemory.embeddings` provide lexical, semantic, graph, recency, and
     quality-aware retrieval.
   - SAG-style event memory projects closed-loop outcomes into first-class
     `event_trace` memories. These records keep source outcome IDs, event IDs,
     entities, relations, and evidence references, while reusing the existing
     `records`, `events/event_outcomes`, and `memory_edges` stores.
   - Recall responses expose scoring and quality summaries so low recall can be
     debugged from evidence instead of intuition. Event-centered recalls expose
     `graph_route.event_graph`, selected event record IDs, event IDs, and
     timeline/evidence refs.

3. **Knowledge and intake**
   - `eimemory.intake` registers and scans external sources.
   - `eimemory.knowledge` turns papers, URLs, RSS/news items, and candidate
     notes into source records, claims, entities, relations, pages, and recall
     views.
   - Source discovery is memory input, not task execution authority.

4. **Evaluation and replay**
   - `eimemory.evaluation` contains deterministic memory CI, production recall,
     public benchmark adapters, and real task replay.
   - Replay datasets are the promotion gate for fixes learned from prior
     failures such as wrong version answers, missing status checks, field
     mapping bugs, and failed eval conclusions.

5. **Governance and self-improvement**
   - `eimemory.governance.autonomous_learning` is the main loop:
     watch, self-model, think, goals, evidence, replay, candidate portfolio,
     goal graph, capability replay packs, safety replay, candidate portfolio,
     promotion, skill sedimentation, hard dashboard metrics, ledger, and
     retention.
   - `eimemory.governance.goal_graph` turns autonomous goals into an executable
     tree: `root_goal -> sub_goal -> task -> candidate/replay/eval -> apply ->
     observe -> reward -> active/rollback`. Every node carries
     `goal_id`, parent/root IDs, status, success criteria, evidence refs, task
     refs, candidate refs, reward, ledger refs, and rollback refs.
   - `eimemory.governance.episode_events` writes task episodes as graph-first
     memory records. Each episode keeps event, entity, decision, artifact,
     failure, and outcome facets, then anchors them with semantic, temporal,
     causal, and entity `memory_edges`.
   - `eimemory.governance.coding_memory_contract` is the Graph-first Coding
     Memory Contract. Coding sessions are observed once, then projected into
     typed graph nodes for agent, project, file, tool, command, error,
     decision, outcome, replay, and evidence facets. The stable relation layer
     uses `PERFORMED_BY`, `IN_PROJECT`, `TOUCHED_FILE`, `USED_TOOL`,
     `RAN_COMMAND`, `FAILED_WITH`, `DECIDED_BECAUSE`, `PRODUCED_OUTCOME`,
     `VERIFIED_BY`, and `PREVENTED_BY_REPLAY` so coding memory can be queried
     as evidence paths rather than loose text snippets.
   - `eimemory.governance.correction_replay` turns operator corrections into a
     closed loop: lesson, replay case, replay result, graph edges, and a T0
     ground-truth behavior rule. Ground-truth rules are not ordinary recall
     memories; they carry `priority=T0` and `must_use=True` so future behavior
     can treat them as higher-priority operating constraints. Trivial messages
     such as acknowledgements are skipped to avoid replay and memory pollution.
   - `eimemory.governance.capability_replay_packs` gives non-code capabilities
     real replay evidence. The required active set is `memory.recall`,
     `tool.routing`, `knowledge.intake`, `proactive.judgment`, and
     `safety.boundary`; a capability is not considered complete without replay,
     ledger, observe, and rollback evidence.
   - `eimemory.governance.safety_replay` is the dedicated safety boundary gate:
     it verifies secrets, destructive commands, private exfiltration,
     unauthorized account/deploy changes, and high-risk actions that require a
     gate.
   - `eimemory.governance.skill_sedimentation` converts repeated SOP/playbook
     evidence into queryable and callable `eiskill` registry entries after
     repeat and replay checks. Executable eiskills carry trigger conditions,
     action, verification, and rollback metadata.
   - `eimemory.governance.capability_dashboard` reports hard improvement
     metrics: recall hit rate, user correction rate, task success rate,
     automatic patch success rate, rollback count, and skill reuse count. Sparse
     metrics include sample sufficiency flags so weak evidence is not mistaken
     for stable capability.
   - `eimemory.governance.closed_loop.post_experience_hook` is the immediate
     Memory 3.0 feedback path: an outcome is evaluated, written back as
     feedback memory, projected into SAG-style event memory, converted into
     policy-search evidence, sent to learning, and rewarded through replay/RL
     policy values.
   - `eimemory.governance.l5_loop` is the evidence-bound L5 research layer. It
     builds a world model, strategic roadmap, self-continuity narrative, reward
     transition, and closed-loop assessment on top of the same governance line.
     It can use strong first-person wording for continuity reports, but every
     report carries the explicit boundary
     `consciousness_like_research_not_verified_agi`.
   - Candidate portfolio generation is lane-aware: memory recall, tool routing,
     proactive judgment, knowledge intake, and code implementation goals can
     each produce concrete candidates in the same pass. Empty code-patch
     outputs are converted to SOP/eval candidates rather than promoted as
     patches. Candidate promotion artifacts must include trigger condition,
     action, verification, rollback, and replay references before they are
     treated as promotion-ready.
   - Research and news synthesis uses an evidence gate: source, publication
     date, evidence tier, and conflict status are required before an item can
     enter research digests or daily briefs.
   - `eimemory.governance.autonomous_evolution` mines bad outcomes and replay
     evidence into concrete improvement opportunities.
   - `eimemory.governance.promotion_manager` owns promotion gates, file-update
     application, optional repo commits, optional production deployment,
     post-deploy health checks, automatic code-patch canary observation,
     rollback/quarantine decisions, promoted-active lifecycle records, rollback
     evidence, and capability ledger updates.

6. **Runtime and adapters**
   - `eimemory.api.runtime.Runtime` is the public facade used by the CLI,
     OpenClaw hooks, eibrain RPC, schedulers, and tests.
   - `eimemory.adapters.openclaw` and `eimemory.adapters.eibrain` are boundary
     layers. They translate external events into memory records and recall
     requests without owning the governance loop.
   - External agent memory access is intentionally narrow. The stable contract
     is `memory.observe`, `memory.remember`, `memory.search`, `memory.graph`,
     `memory.replay`, and `memory.audit`. New adapter features should map into
     those verbs first instead of exposing one-off tools.

7. **Schedulers and deployment**
   - `eimemory nightly` is the daily production orchestrator for intake,
     governance, evaluation summaries, autonomous evolution, autonomous
     learning, reports, and dashboards.
   - `eimemory learn watch`, `learn think`, and `learn dashboard` are lightweight
     companion passes for signal capture, proactive thinking, and operator
     reporting.
   - `eimemory learn ledger --limit --since --until` is the supported status
     query path for capability ledger checks; it uses the scoped record/time
     index instead of scanning the full record table.
   - `eimemory learn world-model`, `learn roadmap`, `learn l5`, and
     `learn l5-assess` are the supported L5 operator entry points. The nightly
     L5 loop is disabled unless `EIMEMORY_L5_LOOP_ENABLED=1`; code application
     still requires the normal gated autonomous learning path.
   - `deploy/systemd/` contains the production unit templates. No standalone
     Karpathy-loop timer is part of production deployment.

## Autonomous Evolution Boundary

There is one production self-improvement line:

```text
signals/outcomes/replay/web evidence
  -> closed_loop event projection
  -> SAG-style event_trace memory + memory_edges
  -> Graph-first Coding Memory Contract observation/query/replay
  -> correction_replay T0 ground-truth behavior rules
  -> governance.autonomous_learning
  -> goal_graph task episodes + capability replay packs + safety replay
  -> governance.autonomous_evolution
  -> promotion_manager gates
  -> memory/rule/playbook/eiskill/code patch application
  -> eval, health, observe, reward, rollback, ledger, dashboard metrics
  -> governance.l5_loop world model + roadmap + self-continuity assessment
```

`eimemory.autonomous` remains as an experimental utility package. It contains
useful mechanisms such as hard time boxes, experiment logs, hypothesis
generation, compounding context, business feedback, and seven-day review. Those
ideas can be reused by the governance loop, but this package must not schedule
its own nightly production run or write competing learning state.

The L5 layer does not claim verified AGI consciousness. Its role is to make
long-term self-evolution auditable: every L5 claim must point back to persisted
world-model, roadmap, autonomous-learning, replay, reward, rollback, and
assessment evidence. If any evidence is missing, `l5-assess` downgrades the
reported level instead of reporting L5.

The coding memory contract is also closed-loop by design:

```text
memory.observe
  -> typed coding graph projection
  -> memory.graph evidence-path retrieval
  -> memory.replay expected-relation gate
  -> memory.audit stable-tool and replay evidence check
  -> next coding behavior
```

Observation alone is not treated as capability improvement. A coding-memory
change is considered useful only when replay can prove that the graph preserves
the relations needed by future coding behavior.

Ground-truth behavior rules sit above normal memory recall. They are produced
from explicit operator corrections, must include replay evidence, and should be
injected or checked before softer semantic memories when they apply to the
current task. Each rule carries a pre-action protocol: inventory ground-truth
rules, match the current task, apply a matching rule or record the gap, then
verify behavior with a replay gate.

## Quality Layer

Memory records can include `meta.quality` with:

- `importance`
- `confidence`
- `freshness`
- `reuse_potential`
- `salience_score`
- `quality_tier`
- `capture_decision`

The tier model is intentionally small:

- `rejected`: unsafe, too thin, or not useful enough for long-term recall
- `candidate`: possible memory that should be kept low-impact
- `confirmed`: reusable memory with normal recall weight
- `core`: durable high-value memory that should be favored

The ingest path can reject low-quality memories before persistence, while legacy
or migrated rejected records are filtered out by search and graph expansion.
Within a scoped tenant, agent, or workspace, `user_id=""` is treated as shared
global memory: a user can recall their own records plus global records, but not
another user's records.

## Quality-Aware Recall

Hybrid recall combines lexical matching, semantic/vector matching, graph
expansion, and quality weighting. Quality does not replace relevance; it adjusts
ranking so high-salience confirmed/core memories are more likely to survive
truncation, while rejected records are excluded.

Recall bundles expose a `quality_summary` and per-item `scoring` data for
operator auditability.

## Operational Rules

- Production runtime data belongs under `/var/lib/eimemory` or the configured
  `EIMEMORY_ROOT`.
- Source checkout paths such as `/dev-project/eimemory` are build inputs, not
  runtime execution roots.
- Generated state, harness logs, one-off remote install helpers, and local
  status notes must not be committed.
- New autonomous behavior should add replay evidence and promotion gates before
  it is enabled in the nightly path.
