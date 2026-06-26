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
     promotion, ledger, dashboard, and retention.
   - `eimemory.governance.closed_loop.post_experience_hook` is the immediate
     Memory 3.0 feedback path: an outcome is evaluated, written back as
     feedback memory, projected into SAG-style event memory, converted into
     policy-search evidence, sent to learning, and rewarded through replay/RL
     policy values.
   - Candidate portfolio generation is lane-aware: memory recall, tool routing,
     proactive judgment, knowledge intake, and code implementation goals can
     each produce concrete candidates in the same pass. Empty code-patch
     outputs are converted to SOP/eval candidates rather than promoted as
     patches.
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
   - `deploy/systemd/` contains the production unit templates. No standalone
     Karpathy-loop timer is part of production deployment.

## Autonomous Evolution Boundary

There is one production self-improvement line:

```text
signals/outcomes/replay/web evidence
  -> closed_loop event projection
  -> SAG-style event_trace memory + memory_edges
  -> governance.autonomous_learning
  -> governance.autonomous_evolution
  -> promotion_manager gates
  -> memory/rule/playbook/code patch application
  -> eval, health, rollback, ledger
```

`eimemory.autonomous` remains as an experimental utility package. It contains
useful mechanisms such as hard time boxes, experiment logs, hypothesis
generation, compounding context, business feedback, and seven-day review. Those
ideas can be reused by the governance loop, but this package must not schedule
its own nightly production run or write competing learning state.

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
