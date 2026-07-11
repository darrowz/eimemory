# eimemory L5 Roadmap Spec

Status: first executable roadmap, 2026-06-28.

## Plain Definition

L5 does not mean "more tools" or "unbounded autonomy". For eimemory, L5 means
Hongtu and the user improve together through a measurable loop:

1. It remembers goals, corrections, outcomes, and capability evidence.
2. It notices weak spots from real outcomes and replay failures.
3. It proposes small improvements with evidence, safety gates, and rollback.
4. It applies only approved or policy-allowed changes inside bounded authority.
5. It reports what changed, whether it helped, and what must be rolled back.

The user-facing test is simple: when asked "how are we growing?", eimemory can
show the current goals, the evidence behind strengths and weaknesses, the last
improvement attempt, the measured outcome, and a rollback path. If any evidence
is missing, the system reports a lower stage instead of claiming L5.

## Rollback Rule

Every L4+ or L5 promotion must have at least one of:

- a no-op or dry-run mode that can be compared with active behavior;
- a previous policy/config value that can be restored;
- a quarantine path that prevents future automatic use;
- a documented rollback command or ledger reference.

No L5 stage may deploy, delete data, export private data, spend money, send
external messages, or use credentials without the existing safety authority
boundary.

## Stage Plan

### L3.5: Readiness and Evidence Inventory

Done when:
- `eimemory learn l5-readiness` reports current stage, gaps, evidence counts,
  and next actions without changing behavior.
- Weak capabilities are visible as first-class gaps:
  `search.discovery`, `research.synthesis`, `operations.uumit`,
  `device.control`.
- Existing strengths are tracked through the capability ledger:
  `memory.recall`, `tool.routing`, `knowledge.intake`, `safety.boundary`.

Data metrics:
- records by kind: memory, learning loop, eval, replay, candidate, promotion,
  rollout, L5 artifacts;
- capability score, evidence count, regression count, confidence;
- hard metrics from `capability_dashboard`: recall hit rate, correction rate,
  task success rate, patch success rate, rollback count.

Risk boundary:
- read-only reporting by default;
- optional `--persist` writes only a reflection report;
- no learning cycle, promotion, network access, deployment, or production data
  mutation.

### L4: Closed-Loop Learning With Measurable Outcomes

Done when:
- autonomous learning creates or updates goal graph nodes from outcome evidence;
- every candidate has replay or eval evidence before promotion;
- each run ends with one of: promoted, blocked with reason, or quarantined;
- dashboard metrics are produced for the same scope.

Data metrics:
- replay result count and pass rate;
- task success rate and user correction rate;
- candidate to promotion conversion rate;
- blocked promotion reasons;
- post-promotion observed count and failure rate.

Risk boundary:
- apply remains off by default;
- promotion authority stays constrained by existing gates;
- unsafe, costly, external-send, deletion, and credential actions stay blocked.

### L4.5: Self-Growth Reporting and Weak-Gap Closure

Done when:
- world model, roadmap, self-continuity, and assessment artifacts exist for
  repeated cycles;
- each weak capability has a replay pack and score >= 0.7 with at least three
  evidence refs;
- a non-destructive rollback or quarantine rehearsal exists;
- reports say which capability improved, which failed, and what evidence proved
  it.

Data metrics:
- L5 artifact counts by type;
- weak capability scores and evidence counts;
- rollback/quarantine rehearsal count;
- post-promotion failure rate <= 5% for canary-observed changes.

Risk boundary:
- no claim of L5 unless `l5_assessment.missing_evidence` is empty;
- roadmap items are plans, not authority to execute;
- first-person continuity language stays evidence-bound.

### L5: Evidence-Bound Co-Growth Loop

Done when:
- each L5 cycle has world model, roadmap, goal graph, autonomous learning
  result, candidate evidence, replay evidence, promotion/block decision, reward
  transition, self-continuity report, and rollback reference;
- weak capabilities are no longer unverified gaps;
- repeated cycles improve task success or reduce corrections without increasing
  safety incidents;
- rollback evidence is exercised and visible.

Data metrics:
- `l5_assessment.complete == true`;
- zero missing evidence in `l5_assessment`;
- weak capability score >= 0.7 and evidence count >= 3;
- replay pass rate >= 0.8 for relevant packs;
- rollback/quarantine path exists for promotions;
- no safety-boundary regression.

Risk boundary:
- L5 is downgraded immediately when required evidence disappears;
- autonomous code or policy changes remain gated by replay, safety, canary,
  ledger, and rollback;
- user trust and shared growth outrank tool count.

## Current Code Fit

Best existing modules to extend:
- `eimemory.governance.l5_loop`: world model, roadmap, self-continuity,
  assessment, reward transition.
- `eimemory.governance.autonomous_learning`: existing learning cycle, candidates,
  promotion limits, replay handoff.
- `eimemory.governance.capability_ledger`: capability scores, evidence refs,
  regression counts.
- `eimemory.governance.capability_replay_packs` and `replay_dataset`: replay
  evidence for capabilities.
- `eimemory.governance.learning_dashboard` and `capability_dashboard`: operational
  reporting and hard metrics.
- `eimemory.experience.outcome` and `eimemory.governance.event_graph`: outcome
  traces and graph-first memory anchors.
- `eimemory.evaluation.task_replay` and `regression_replay`: deterministic
  replay checks.
- `eimemory.governance.safety.*`, `promotion_manager`, `promotion_watch`:
  authority boundary, rollout ledger, post-promotion monitoring, rollback.

Main gaps:
- weak capabilities still need real, verified outcome traces in each production
  scope; replay packs now execute those traces instead of defaulting to pass;
- `l5-assess` can assess a supplied loop, but the CLI default currently assesses
  an empty report and therefore mainly shows missing evidence;
- rollback rehearsals are counted indirectly through promotion status, not yet
  first-class L5 evidence;
- readiness and self-growth reporting needs to be routine before any larger
  behavior change;
- search/research/operations/device capabilities need domain-specific success
  metrics, not only generic capability scores.

## Phase 1 Minimal Implementation

Implemented first step:

```bash
eimemory learn l5-readiness --json
```

Behavior:
- summarizes existing records, ledger scores, hard metrics, and capability gaps;
- reports stage as L3.5/L4/L4.5/L5 according to evidence;
- defaults to read-only;
- `--persist` writes one `reflection` report with
  `meta.report_type=l5_readiness_report`.

Next implementation steps:

1. Generate weak-capability replay packs from existing outcome traces.
2. Make `learn l5-assess` optionally read the latest persisted L5 loop instead
   of only assessing an empty report.
3. Add a rollback rehearsal record type or normalized report field.
4. Require readiness report + replay pack pass before any L4.5/L5 claim.

## Evidence Integrity Rules Added In 1.9.15

- A capability replay pass requires `hit=true`, a non-empty observation, and a
  distinct `evidence_source_id` from a verified outcome trace.
- Every replay invocation persists a distinct execution batch. Readiness uses
  only the latest execution for each capability/case, so a fixed regression can
  recover and an old pass cannot mask a new failure.
- Weak-capability replay cases are case-specific. Generic success outcomes do
  not satisfy search, research, UUMit, or device acceptance checks.
- `not_run` replay records remain visible for diagnostics but do not enter L4
  or L5 executed replay counts.
- L5 requires at least ten executed replays, overall pass rate at least 0.8,
  and at least three distinct passing evidence sources for each weak
  capability.
- The latest assessment must be produced by `eimemory.l5_loop`, use schema
  `l5_closed_loop.v1`, set `complete=true`, and contain no missing evidence.
- Patch promotion success requires an explicit code-patch target, executed
  gate, verification, production apply, post-deploy health, commit identity,
  and rollback evidence. A status-only `deployed` record is not success.
- Rollback readiness counts executed policy/lifecycle ledger actions, not a
  mutable promotion status alone.
- Closure rehearsal outcomes are labelled as rehearsals and never inflate the
  production task-success metric.
