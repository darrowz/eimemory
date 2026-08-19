# L5 Dynamic Cognitive Architecture v3 Implementation Plan

Status: ready for execution handoff, 2026-08-20.

Primary executor: `gpt-5.6-terra` after the user switches models.

Target specification:
`docs/superpowers/specs/2026-08-20-l5-dynamic-cognitive-architecture-v3-design.md`.

## Execution contract for the next model

Read this complete plan and the target specification before changing code. Work
task by task, update the checkboxes and evidence log in this file, and continue
until the current task's exit gate passes. Do not infer completion from existing
green tests, a healthy service, or the presence of a module.

This plan is intentionally executable without ECC or another external agent
harness. It adopts contract-first, eval-first, expand-contract migration,
measured benchmarking, and evidence reporting as repository-local disciplines.

## Goal

Replace the fixed L5 capability taxonomy with a dynamic, provider-independent,
knowledge-connected capability architecture; migrate the data model safely;
preserve adapter behavior; retain fully machine-gated code evolution without a
human approval queue; and prove semantic, migration, and performance parity
before removing the old paths.

## Starting-state warning

The worktree already contains an unreleased cleanup and closure batch. It
includes removed zombie modules, OpenClaw surface cleanup, automatic code
proposal/apply work, PDF artifact closure, knowledge refresh, tests, and
documentation. These changes belong to the user.

The executor must:

- inspect `git status --short`, `git diff --stat`, and overlapping diffs first;
- never reset, restore, overwrite, or silently reformat unrelated changes;
- keep the existing cleanup batch logically separate from L5 v3 changes;
- avoid a version bump until release work is explicitly authorized;
- stop if a prerequisite requires discarding or redefining user changes;
- not push or deploy unless the user explicitly authorizes those actions in the
  active turn.

## Global constraints

1. No fixed strong/weak/core/readiness capability universe may remain after
   cutover.
2. New capabilities are registered as data and consumed through contracts.
3. Package version and machine identity are never capability identity.
4. Deployment-dependent evidence remains bound to commit, receipt, and session.
5. Adapters advertise different capabilities; they do not need identical
   model-facing tools.
6. Knowledge never promotes itself. It must pass through hypothesis, eval,
   candidate, result, and outcome links.
7. No human approval state is added to code evolution. Machine policy is the
   decision authority.
8. SQLite remains the local transactional store; PostgreSQL/pgvector remains an
   optional read projection.
9. Schema and data migrations are separate, forward-only, idempotent, bounded,
   and restartable.
10. All new query-critical capability fields use typed columns; JSON remains an
    extensibility and audit envelope rather than the only query plan.
11. Every work package begins with a contract or characterization test and ends
    with focused tests plus evidence.
12. Do not run the full test suite during individual tasks. Run focused suites;
    run one full suite only in the final integration task when authorized by this
    plan.
13. Generated code-evolution verification may not execute Git, shell, network,
    credential, external-message, `python -c`, or broad full-suite commands.
14. Preserve fail-closed behavior for malformed evidence, stale knowledge,
    unknown graders, missing artifacts, and unsupported adapters.
15. Documentation must describe implemented behavior, not planned behavior, at
    each cutover.

## Standard work-package loop

For every task:

1. Name the invariant and affected consumers.
2. Add characterization or RED tests for the intended boundary.
3. Run only the focused RED batch and record the expected failure.
4. Implement the smallest complete owner-level change.
5. Run the focused GREEN batch.
6. Run `compileall` for modified Python package roots.
7. Run `git diff --check` on changed files.
8. If data or hot paths changed, run the task benchmark/migration probe.
9. Perform an automated fresh-context diff review for correctness, security,
   performance, migration, and adapter impact.
10. Record results under the task's Evidence section before marking it complete.

Use RTK on this Windows workspace:

```powershell
& 'C:\Users\maiph\.local\bin\rtk.exe' pytest -q <focused tests>
& 'C:\Users\maiph\.local\bin\rtk.exe' proxy python -m compileall -q <package roots>
& 'C:\Users\maiph\.local\bin\rtk.exe' proxy git diff --check -- <paths>
```

## Phase and dependency map

```text
WP0 baseline custody
  -> WP1 ADRs/contracts
      -> WP2 performance baseline
      -> WP3 Storage v2 schema
          -> WP4 registry and profiles
              -> WP5 adapter advertisements
              -> WP6 dynamic eval catalog
                  -> WP7 observations and ledger migration
                      -> WP8 knowledge-capability bridge
                      -> WP9 capability projector
                          -> WP10 L5 v3 assessment
                              -> WP11 consumer migration
                              -> WP12 release/deployment separation
                                  -> WP13 automatic evolution integration
                                      -> WP14 backfill and shadow mode
                                          -> WP15 cutover and cleanup
                                              -> WP16 final integration
                                                  -> WP17 release/deploy only if authorized
```

---

## WP0 — Establish change custody and a reproducible baseline

**Purpose:** Prevent the existing cleanup batch and the L5 refactor from being
mixed, lost, or incorrectly credited.

**Files:**

- Read: all current modified/untracked paths.
- Add: `docs/audit/l5-v3-pre-refactor-baseline.md`
- Add: `artifacts/l5-v3/baseline-manifest.json` only if `artifacts/` is an
  established tracked evidence location; otherwise keep generated benchmark
  output below `EIMEMORY_ROOT` and link its digest from the audit document.

- [ ] Capture repository HEAD, branch, upstream, status, diff stat, changed-file
  list, Python version, SQLite version, platform, and relevant optional
  dependency availability.
- [ ] Classify every current change as `cleanup-existing`, `l5-v3-new`, or
  `unrelated-user-change`; do not rewrite existing changes merely to classify
  them.
- [ ] Confirm whether the existing cleanup batch has a reviewable baseline
  commit. If not, keep a path-level ownership manifest so later diffs can be
  reviewed separately.
- [ ] Record the previously focused adapter and closure test evidence as
  historical evidence only; rerun a test only when required by an overlapping
  change.
- [ ] Record current hard-coded taxonomy locations with `rg`, including at least
  capability contract, acceptance, replay packs, readiness, release lineage,
  goal queue, self model, replay dataset, and world watchers.

**Exit gate:** No file is unowned, the starting commit and dirty state are
recorded, and the executor can distinguish pre-existing changes from new work.

**Evidence:** Add commands, exit codes, and the baseline document digest here.

---

## WP1 — Record architecture decisions and define v3 contracts

**Depends on:** WP0.

**Purpose:** Make the architectural choices machine-checkable before storage or
consumer changes.

**Files:**

- Add: `docs/adr/0001-dynamic-capability-identity.md`
- Add: `docs/adr/0002-l5-multi-axis-assessment.md`
- Add: `docs/adr/0003-storage-v2-authority-and-projections.md`
- Add: `docs/adr/0004-adapter-capability-negotiation.md`
- Add: `docs/adr/0005-knowledge-capability-feedback.md`
- Add: `eimemory/capabilities/__init__.py`
- Add: `eimemory/capabilities/contracts.py`
- Add: `eimemory/capabilities/models.py`
- Add: `tests/test_capability_v3_contracts.py`

- [ ] Define validated models for `CapabilityDefinition`,
  `CapabilityRevision`, `CapabilityRelation`, `CapabilityBinding`,
  `CapabilityProfile`, `EvaluationSpec`, `CapabilityObservation`,
  `EvaluationRun`, `CapabilityKnowledgeLink`, `CapabilityStateSnapshot`, and
  `L5AssessmentV3`.
- [ ] Define stable canonical serialization and digest functions.
- [ ] Reject empty/oversized IDs, unknown lifecycle states, cyclic direct
  supersession, invalid relation types, unbounded payloads, and executable
  content in definitions/specifications.
- [ ] Keep capability ID independent from provider, version, commit, hostname,
  model, and environment fingerprint.
- [ ] Represent schema revisions and compatibility explicitly.
- [ ] Add tests proving a new arbitrary capability such as
  `planning.constraint_resolution` validates without modifying a source list.
- [ ] Add tests proving two providers and two machines can bind the same
  capability revision without changing its identity.
- [ ] Add tests proving package-version-only changes do not affect semantic
  identity or applicability.

**Focused tests:**

```powershell
& 'C:\Users\maiph\.local\bin\rtk.exe' pytest -q tests/test_capability_v3_contracts.py
```

**Exit gate:** Contracts exist, arbitrary capabilities validate, identity rules
are proven, and all five ADRs agree with the target specification.

---

## WP2 — Capture functional and performance baselines

**Depends on:** WP1 contracts, but not WP3 schema.

**Purpose:** Establish measurable budgets before changing storage and L5 hot
paths.

**Files:**

- Add: `benchmarks/l5_v3_baseline.py`
- Add: `tests/performance/test_l5_v3_baseline_contract.py`
- Add: `docs/audit/l5-v3-performance-baseline.md`

- [ ] Build deterministic small/medium/large fixtures covering records,
  capability scores, replay results, outcomes, knowledge links, and adapters.
- [ ] Measure recall p50/p95/p99, append, atomic mutation, existing readiness,
  ledger construction, replay-pack build, SQLite/WAL size, and startup/migration
  time.
- [ ] Measure result parity using digests, not only timing.
- [ ] Run warm and cold variants separately.
- [ ] Store hardware/OS/Python/SQLite as benchmark context, not pass/fail
  identity.
- [ ] Establish versioned relative budgets in a benchmark profile. If the local
  machine is too noisy, use repeated samples and confidence bands rather than
  hard-coded machine names.
- [ ] Ensure the benchmark cannot mutate production runtime data.

**Exit gate:** A reproducible baseline report and benchmark profile exist, with
scale tiers and explicit variance policy.

---

## WP3 — Add Storage v2 schema and migration framework

**Depends on:** WP1 and WP2.

**Purpose:** Introduce normalized capability/eval storage without changing
existing reads.

**Files:**

- Modify: `eimemory/storage/sqlite_store.py`
- Modify: `eimemory/storage/runtime_store.py`
- Add: `eimemory/storage/capability_store.py`
- Add: `eimemory/storage/migrations/capability_v3.py`
- Modify or add: the central migration registry used by deferred migrations.
- Add: `tests/test_capability_storage_v3.py`
- Modify: `tests/test_storage_deferred_migrations.py`
- Modify: `tests/test_runtime_store_concurrency.py`

- [ ] Add the normalized entities required by the specification with scope,
  revision, provenance, digest, timestamp, and idempotency fields.
- [ ] Add foreign keys or equivalent deterministic integrity checks. Enable and
  test SQLite foreign-key enforcement for these tables.
- [ ] Add indexes from the Storage v2 indexing contract and verify query plans
  with representative fixtures.
- [ ] Keep large fixtures and evidence bodies as content-addressed references.
- [ ] Separate schema creation from data backfill.
- [ ] Add migration status, cursor, counts, digest, last error, started/finished
  timestamps, and restart semantics.
- [ ] Make schema migration transactional and idempotent.
- [ ] Make data migration bounded by rows and elapsed time.
- [ ] Add operation-journal/outbox integration so a committed domain mutation is
  recoverable before audit export.
- [ ] Prove concurrent registration/observation cannot create duplicate active
  revisions or lose idempotent writes.

**Focused tests:** storage v3, deferred migrations, and runtime-store concurrency.

**Exit gate:** Empty v3 tables can be created repeatedly, old reads are
unchanged, migration state is observable, and concurrency tests pass.

---

## WP4 — Implement the capability registry and profiles

**Depends on:** WP3.

**Purpose:** Create the sole owner for capability definitions, revisions,
relations, bindings, and readiness profiles.

**Files:**

- Add: `eimemory/capabilities/registry.py`
- Add: `eimemory/capabilities/profiles.py`
- Add: `eimemory/capabilities/service.py`
- Modify: `eimemory/api/runtime.py`
- Add: `tests/test_capability_registry.py`
- Add: `tests/test_capability_profiles.py`

- [ ] Implement register, revise, deprecate, retire, quarantine, relate, bind,
  advertise, list, and resolve operations.
- [ ] Require optimistic revision/digest checks for mutable lifecycle operations.
- [ ] Reject dependency cycles and incompatible active revisions.
- [ ] Keep history immutable and expose an effective-at-time read.
- [ ] Implement profiles with per-capability or selector-based evidence,
  reliability, freshness, risk, and dependency requirements.
- [ ] Seed existing capabilities through an idempotent migration manifest, not a
  runtime global list.
- [ ] Treat seeding as initial data that can be revised/deprecated through the
  registry.
- [ ] Expose bounded runtime methods without exposing database rows directly.

**Exit gate:** An arbitrary capability and profile can be added at runtime and
resolved without editing any L5 or replay source file.

---

## WP5 — Add provider-independent adapter advertisements

**Depends on:** WP4.

**Purpose:** Let adapters declare different capability sets and normalize
outcomes without forcing tool-surface parity.

**Files:**

- Add: `eimemory/adapters/runtime/capability.py`
- Modify: `eimemory/adapters/runtime/__init__.py`
- Modify: `eimemory/adapters/codex/hook.py`
- Modify: `eimemory/adapters/codex/mcp_server.py` only if internal advertisement
  plumbing requires it; do not add unnecessary model tools.
- Modify: `eimemory/adapters/hermes/provider_core.py`
- Modify: `eimemory/adapters/hermes/provider_registry.py`
- Modify: `eimemory/adapters/openclaw/hooks.py`
- Modify: `eimemory/adapters/openclaw/e2e.py`
- Modify: `eimemory/adapters/eibrain/rpc.py`, `rpc_server.py`, and `sdk.py` only
  for additive internal contract support.
- Add: `tests/test_adapter_capability_advertisements.py`
- Modify: `tests/test_codex_adapter.py`
- Modify: `tests/test_hermes_adapter.py`
- Modify: `tests/test_openclaw_outcome_hooks.py`
- Modify: `tests/test_adapters.py`

- [ ] Define advertisement and outcome-normalization protocols.
- [ ] Persist advertisement revision, digest, freshness, limits, and provider
  instance separately from semantic capability identity.
- [ ] Prove Codex and Hermes can retain their current public contract.
- [ ] Prove OpenClaw keeps lifecycle behavior in hooks and bridge status on its
  model surface; do not restore the removed dead Python tool wrapper.
- [ ] Mark unsupported host events explicitly instead of mapping them to a
  guessed capability.
- [ ] Reject stale, oversized, unsigned where required, or schema-invalid
  advertisements.
- [ ] Keep host/machine metadata diagnostic and secret-safe.

**Exit gate:** All adapters pass their own focused contract suite with different
advertised capability sets and unchanged required public surfaces.

---

## WP6 — Replace fixed acceptance and replay cases with an eval catalog

**Depends on:** WP4 and WP5.

**Purpose:** Make capability evaluation data-driven and extensible.

**Files:**

- Add: `eimemory/evaluation/capability_catalog.py`
- Add: `eimemory/evaluation/capability_graders.py`
- Modify: `eimemory/experience/capability_contract.py`
- Modify: `eimemory/governance/capability_acceptance.py`
- Modify: `eimemory/governance/capability_probe_executor.py`
- Modify: `eimemory/governance/capability_replay_packs.py`
- Modify: `eimemory/governance/capability_replay_executor.py`
- Add: `tests/test_dynamic_capability_evals.py`
- Modify: `tests/test_capability_acceptance.py`
- Modify: `tests/test_capability_replay_packs.py`
- Modify: `tests/contract/test_capability_closure_contract.py`

- [ ] Migrate existing cases into registered `EvaluationSpec` revisions with
  artifact digests and explicit executors/graders.
- [ ] Replace Python case switches with executor and grader registries keyed by
  bounded IDs.
- [ ] Support deterministic code graders and schema/rule graders first.
- [ ] Add a bounded model-grader contract, but do not require it for existing
  deterministic cases.
- [ ] Remove human-grader release states; ambiguous cases become inconclusive or
  blocked.
- [ ] Preserve anti-self-blessing, distinct evidence source, tamper detection,
  latest-run, and fail-closed semantics.
- [ ] Generate replay packs from active registry/profile requirements, not a
  `CORE_REPLAY_CAPABILITIES` list.
- [ ] Prove registering an eval for a newly registered arbitrary capability makes
  it discoverable and runnable without editing acceptance/replay code.

**Exit gate:** Existing cases pass through the catalog, arbitrary new evals run,
and no fixed case universe remains in the evaluator boundary.

---

## WP7 — Unify observations, outcomes, and capability ledger projection

**Depends on:** WP6 and WP3.

**Purpose:** Replace capability scoring based on scattered record scans and
fixed seeding with normalized observations and incremental projections.

**Files:**

- Add: `eimemory/capabilities/observations.py`
- Modify: `eimemory/experience/outcome.py`
- Modify: `eimemory/governance/capability_attribution.py`
- Modify: `eimemory/governance/capability_ledger.py`
- Modify: `eimemory/governance/capability_dashboard.py`
- Modify: `eimemory/governance/capability_seeding.py`
- Add: `tests/test_capability_observations_v3.py`
- Modify: `tests/test_capability_attribution.py`
- Modify: `tests/test_capability_ledger.py`
- Modify: `tests/test_capability_dashboard_metrics.py`

- [ ] Normalize accepted outcomes, eval runs, adapter lifecycle results, and
  regressions into append-only observations.
- [ ] Require capability revision, binding where applicable, idempotency,
  verdict, evidence refs, and applicability context.
- [ ] Preserve raw outcome/eval records; normalization is additive.
- [ ] Implement watermarked incremental aggregation.
- [ ] Keep a compatibility reader for old capability-score records during shadow
  mode.
- [ ] Backfill old score/replay/outcome evidence through deterministic adapters,
  recording unmappable rows rather than guessing.
- [ ] Prove repeated normalization is idempotent and late-arriving failures
  invalidate affected snapshots.

**Exit gate:** New capability evidence enters through one observation boundary,
and the old ledger can be compared with a v3 projection.

---

## WP8 — Implement the knowledge-capability feedback bridge

**Depends on:** WP4 and WP7. It also assumes the current PDF artifact and
knowledge refresh closure remains intact.

**Purpose:** Turn accumulated knowledge into testable capability improvement
without allowing knowledge to self-authorize.

**Files:**

- Add: `eimemory/knowledge/capabilities.py`
- Add: `eimemory/governance/capability_hypotheses.py`
- Modify: `eimemory/knowledge/claims.py`
- Modify: `eimemory/knowledge/relations.py`
- Modify: `eimemory/knowledge/refresh.py`
- Modify: `eimemory/governance/research_planner.py`
- Modify: `eimemory/governance/autonomous_learning.py`
- Add: `tests/test_knowledge_capability_bridge.py`
- Modify: `tests/test_knowledge_refresh.py`
- Modify: `tests/test_knowledge_projectors.py`

- [ ] Persist typed knowledge links with source trust, review, temporal validity,
  contradiction state, applicability, and provenance.
- [ ] Create hypotheses as separate records referencing knowledge links and a
  capability revision.
- [ ] Require a hypothesis to produce an eval/replay or bounded candidate before
  it can affect behavior.
- [ ] Propagate rejected, stale, contradicted, refreshed, and superseded source
  state into link applicability.
- [ ] On successful/failed experiments, record feedback to applicability without
  mutating original source claims.
- [ ] Prevent missing canonical artifacts, conflicted claims, and unsupported
  environments from producing active hypotheses.
- [ ] Prove knowledge volume alone does not change maturity.

**Exit gate:** A source-to-hypothesis-to-eval-to-result trace is queryable, and a
failed/contradicted source fails closed without leaving an active projection.

---

## WP9 — Implement the capability state projector

**Depends on:** WP7 and WP8.

**Purpose:** Compute reproducible, profile-specific maturity from evidence.

**Files:**

- Add: `eimemory/capabilities/projector.py`
- Add: `eimemory/capabilities/applicability.py`
- Add: `tests/test_capability_state_projector.py`
- Add: `tests/performance/test_capability_projector_performance.py`

- [ ] Project maturity states from observations, eval runs, dependencies,
  freshness, risk, and profile requirements.
- [ ] Include input watermark and algorithm revision in every snapshot.
- [ ] Recompute only affected capabilities and dependent composites.
- [ ] Define deterministic handling for late evidence, superseded revisions,
  stale bindings, contradictions, and quarantines.
- [ ] Avoid a single irreversible score; preserve metrics and reason codes.
- [ ] Add pass@1, controlled retry reliability, consecutive stability, sample
  sufficiency, and regression streak metrics only when meaningful to the profile.
- [ ] Prove hostname/version-only changes do not change projected state.
- [ ] Benchmark incremental versus full projection on all baseline scale tiers.

**Exit gate:** Snapshots are reproducible from evidence, incremental performance
meets budget, and every state has exact reason/evidence references.

---

## WP10 — Build L5 Assessment v3 and shadow API

**Depends on:** WP9.

**Purpose:** Separate loop maturity, capability readiness, adapter readiness, and
deployment assurance.

**Files:**

- Add: `eimemory/governance/l5_assessment_v3.py`
- Add: `eimemory/governance/l5_shadow.py`
- Modify: `eimemory/governance/l5_loop.py`
- Modify: `eimemory/governance/l5_readiness.py` only to add shadow plumbing; do
  not remove v2 yet.
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Add: `tests/test_l5_assessment_v3.py`
- Add: `tests/test_l5_v3_shadow.py`

- [ ] Implement four independent axes from the specification.
- [ ] Select capability requirements by active profile and registry query.
- [ ] Preserve per-capability/provider results even when producing a summary.
- [ ] Keep deployment assurance separate and retain exact release evidence where
  required.
- [ ] Add read-only CLI/API shadow output with v2/v3 semantic difference report.
- [ ] Classify differences as expected taxonomy removal, evidence mapping gap,
  profile difference, adapter difference, or defect.
- [ ] Never allow shadow mode to affect promotion, deployment, or current L5
  state.
- [ ] Persist shadow reports only under an explicit option and stable digest.

**Exit gate:** L5 v3 runs read-only beside v2, explains every axis, and reports
differences without changing current behavior.

---

## WP11 — Migrate goals, self-model, replay classification, and dashboards

**Depends on:** WP10.

**Purpose:** Remove fixed taxonomy from secondary consumers.

**Files:**

- Modify: `eimemory/governance/autonomy_goal_queue.py`
- Modify: `eimemory/governance/self_model.py`
- Modify: `eimemory/governance/replay_dataset.py`
- Modify: `eimemory/governance/world_watchers.py`
- Modify: `eimemory/governance/thoughts.py`
- Modify: `eimemory/governance/learning_dashboard.py`
- Modify: `eimemory/governance/capability_dashboard.py`
- Add: `tests/test_dynamic_capability_consumers.py`
- Modify focused existing tests for each owner.

- [ ] Replace fixed default goal capabilities with registry/profile queries.
- [ ] Move user value, risk, cost, and priority into versioned profile or policy
  data.
- [ ] Replace keyword-to-fixed-capability fallbacks with explicit attribution
  rules stored as data; unknown remains `unclassified`, not a guessed default.
- [ ] Let the self model render arbitrary capability IDs and relations.
- [ ] Make dashboards paginate and aggregate dynamic capability sets.
- [ ] Preserve historical labels through aliases and migration metadata.
- [ ] Prove adding and deprecating a capability changes consumers without source
  edits.

**Exit gate:** Secondary consumers use registry/profile/attribution contracts;
no consumer relies on a compiled capability universe.

---

## WP12 — Separate release lineage from capability identity

**Depends on:** WP10 and WP11.

**Purpose:** Retain strong deployment evidence while eliminating version/machine
coupling from cognitive state.

**Files:**

- Modify: `eimemory/governance/release_lineage.py`
- Modify: `eimemory/governance/evidence_contract.py`
- Modify: `eimemory/governance/l5_maturity.py`
- Modify: release closure/readiness consumers identified by `rg`.
- Add: `tests/test_l5_v3_release_independence.py`
- Modify: `tests/test_release_lineage.py`
- Modify: `tests/test_governance_evidence_contract.py`

- [ ] Replace fixed release domains and weak/core capability groups with
  capability revision applicability declarations.
- [ ] Keep `(commit, receipt, session)` as deployment evidence authority.
- [ ] Keep version descriptive and machine fingerprint diagnostic.
- [ ] Define deterministic evidence inheritance by compatibility digest and
  affected implementation domain.
- [ ] Require current-release evidence only when an eval or observation declares
  deployment dependence.
- [ ] Prove same capability revision on a new machine retains portable evidence
  and invalidates environment-specific evidence only.
- [ ] Prove a changed implementation contract invalidates affected evidence even
  when the package version does not change.

**Exit gate:** Deployment assurance remains strict; cognitive maturity is no
longer globally reset or granted by version/machine identity.

---

## WP13 — Connect automatic evolution to dynamic capability gaps

**Depends on:** WP8 through WP12.

**Purpose:** Make dynamic capability evidence drive fully machine-gated
improvements and feed results back into L5.

**Files:**

- Modify: `eimemory/governance/autonomous_learning.py`
- Modify: `eimemory/governance/autonomous_evolution.py`
- Modify: `eimemory/governance/code_evolution.py`
- Modify: `eimemory/governance/code_evolution_bridge.py`
- Modify: `eimemory/governance/promotion_manager.py`
- Modify: `eimemory/governance/code_patch_command_policy.py`
- Modify: `eimemory/governance/promotion_watch.py`
- Modify: `eimemory/scheduler/jobs.py`
- Add: `tests/test_dynamic_capability_evolution.py`
- Modify focused code-evolution, promotion, and autonomous-learning tests.

- [ ] Select gaps from profile-specific projected states, not weak lists.
- [ ] Require hypotheses and candidates to reference capability revision,
  evidence watermark, eval specs, and expected metric movement.
- [ ] Preserve proposal generation, positive argv command policy, isolated
  validation, subject-state digests, transactional write, recovery, rollback,
  and quarantine.
- [ ] Assert no human-approval status, queue, or branch exists.
- [ ] Let policy automatically enable local apply, commit, or deployment as
  distinct capabilities; absent policy blocks/skips without waiting for a human.
- [ ] Feed verification and real outcomes back as observations before maturity
  projection.
- [ ] Prevent a code change from self-grading with evidence it generated unless
  an independent deterministic verifier attests it.
- [ ] Add interruption, stale-subject, malicious-command, failed verification,
  rollback, quarantine, and successful feedback tests.

**Exit gate:** A dynamic capability gap can produce and evaluate a bounded change
without human approval, and success/failure updates the correct capability state.

---

## WP14 — Backfill, dual-write, and run L5 v3 shadow mode

**Depends on:** WP13.

**Purpose:** Prove data and semantic migration before switching readers.

**Files:**

- Add: `eimemory/storage/migrations/backfill_capability_v3.py`
- Add: `eimemory/governance/l5_v3_reconcile.py`
- Add: `deploy/verify_l5_v3_migration.py`
- Add: `tests/test_l5_v3_backfill.py`
- Add: `tests/test_l5_v3_reconcile.py`
- Modify: `tests/test_storage_deferred_migrations.py`

- [ ] Backfill definitions, revisions, bindings, eval specs/runs,
  observations, knowledge links, and snapshots in bounded batches.
- [ ] Record cursor, source count, mapped count, skipped count by reason,
  destination count, source/destination digests, and duration.
- [ ] Make restart, duplicate execution, and partial-failure recovery tests pass.
- [ ] Keep dual write active at central owners, not scattered consumers.
- [ ] Run v2/v3 shadow comparisons across representative scopes and datasets.
- [ ] Require zero unexplained structural differences and explicitly accepted
  semantic differences caused by removing fixed taxonomy.
- [ ] Measure new performance against WP2 budgets and identify query/index
  regressions with query plans.
- [ ] Verify optional PostgreSQL projections can be rebuilt and are not required
  for local correctness.

**Exit gate:** Backfill is complete and reproducible, dual writes agree, shadow
differences are classified, and performance budgets pass.

---

## WP15 — Cut over reads and remove fixed taxonomy

**Depends on:** WP14.

**Purpose:** Make v3 the sole production owner and remove zombie compatibility
paths only after proof.

**Files:**

- Modify: `eimemory/governance/l5_readiness.py`
- Modify: `eimemory/governance/l5_loop.py`
- Modify: `eimemory/api/runtime.py`
- Modify: `eimemory/cli/main.py`
- Modify: `eimemory/scheduler/jobs.py`
- Remove or reduce fixed constants and case tables in every path found in WP0.
- Update: `docs/architecture.md`
- Update: `docs/modules.md`
- Update: `docs/l5-roadmap-spec.md`
- Update: `README.md`
- Update: `CHANGELOG.md`
- Add: `tests/test_no_fixed_l5_taxonomy.py`

- [ ] Switch reads behind a reversible configuration flag.
- [ ] Run focused cutover regressions with old writes still enabled.
- [ ] Switch default to v3 after shadow evidence passes.
- [ ] Stop old writes only after the rollback observation window.
- [ ] Remove `READINESS_CAPABILITIES`, `STRONG_CAPABILITIES`,
  `WEAK_CAPABILITIES`, `CORE_REPLAY_CAPABILITIES`, weak acceptance tuples,
  fixed goal priority/value/risk maps, and equivalent aliases.
- [ ] Replace source-text tests with behavioral contract tests.
- [ ] Remove v2 compatibility only when no production import, dynamic entry,
  adapter reference, migration requirement, or rollback reader remains.
- [ ] Add a static guard that fails when fixed-taxonomy identifiers or known
  lists are reintroduced outside migration fixtures/history.
- [ ] Document explicit remaining limits and migration rollback window.

**Exit gate:** v3 is the sole active reader, no fixed capability universe exists
in production code, and rollback remains possible through the declared window.

---

## WP16 — Final integration, performance, and architecture verification

**Depends on:** WP15.

**Purpose:** Prove the refactor as a whole before any release action.

### Focused integration matrix

- [ ] Capability contracts, registry, profiles, storage, migrations.
- [ ] Dynamic evals, observations, projector, knowledge bridge.
- [ ] L5 v3 axes, shadow reconciliation, release independence.
- [ ] Codex, Hermes, OpenClaw, eibrain adapter contracts.
- [ ] Automatic learning/evolution, command policy, transaction recovery,
  promotion, rollback, quarantine.
- [ ] Recall and knowledge refresh non-regression.
- [ ] Deployment tooling tests without deploying.
- [ ] Security tests for untrusted advertisements/specs/knowledge and scope
  isolation.
- [ ] Small/medium/large performance profiles and migration timing.

### Full-suite rule

After all focused matrices pass and only once for the final integration state:

```powershell
& 'C:\Users\maiph\.local\bin\rtk.exe' pytest -q
```

If it fails:

- classify failures as refactor regression, pre-existing dirty-tree failure,
  environment/optional dependency, or flaky/non-deterministic;
- fix only in-scope regressions;
- rerun the failed focused group first;
- rerun the full suite only when the final integrated state has materially
  changed and a full result is needed.

### Static and packaging verification

- [ ] Compile all retained Python packages.
- [ ] Run `git diff --check`.
- [ ] Validate package manifests and adapter manifests.
- [ ] Search for deleted module imports and fixed taxonomy remnants.
- [ ] Rebuild the local `code-review-graph` index after the final source shape is
  stable.
- [ ] Perform automated fresh-context reviews for architecture, database,
  performance, security, adapters, and migration.
- [ ] Reconcile all findings; no P0/P1 finding may remain unexplained.

**Exit gate:** Focused and final test evidence is green, performance budgets
pass, migration is verified, and the review report contains no unresolved
release blocker.

---

## WP17 — Release, push, and deployment closure

**Depends on:** WP16 and explicit user authorization in the active turn.

This plan does not itself authorize external writes, GitHub push, or production
deployment. When authorized:

- [ ] Reconfirm exact upstream and intended branch.
- [ ] Rebase/synchronize safely without losing user changes.
- [ ] Update version and changelog exactly once.
- [ ] Commit with an intentional path inventory and evidence summary.
- [ ] Push the reviewed commit.
- [ ] Deploy the exact full commit through the immutable-release workflow.
- [ ] Verify origin branch, local HEAD, deployed release target, import root,
  package digest, RPC/HTTP health, adapter contracts, migration state, and
  rollback ancestor all agree.
- [ ] Run fresh production L5 v3 assessment and retain separate reporting for
  cognitive loop maturity, capability readiness, adapter readiness, and
  deployment assurance.
- [ ] Never claim L5 from service health alone.

**Exit gate:** Repository and production identity agree; live evidence and
rollback are verified; the final report distinguishes deployment health from L5
and capability state.

## Required final deliverables

1. Five accepted ADRs.
2. Dynamic capability contracts and registry.
3. Storage v2 schema, migrations, backfill, and reconciliation report.
4. Adapter advertisement and normalization contracts for all retained adapters.
5. Dynamic eval catalog and migrated cases.
6. Knowledge-capability hypothesis/result feedback chain.
7. Incremental capability state projector and benchmark evidence.
8. Multi-axis L5 v3 assessment and shadow comparison history.
9. Fully machine-gated evolution evidence without a human approval queue.
10. Fixed-taxonomy deletion proof and updated module/architecture docs.
11. Focused and final integration test evidence.
12. Performance report for all scale tiers.
13. Release/deployment report only if WP17 is authorized.

## Progress ledger

Update this table during execution. Do not mark a row complete without its exit
gate evidence.

| Work package | Status | Evidence reference | Commit |
| --- | --- | --- | --- |
| WP0 baseline custody | pending | | |
| WP1 ADRs/contracts | pending | | |
| WP2 performance baseline | pending | | |
| WP3 Storage v2 schema | pending | | |
| WP4 registry/profiles | pending | | |
| WP5 adapter advertisements | pending | | |
| WP6 eval catalog | pending | | |
| WP7 observations/ledger | pending | | |
| WP8 knowledge bridge | pending | | |
| WP9 state projector | pending | | |
| WP10 L5 v3 | pending | | |
| WP11 consumer migration | pending | | |
| WP12 release separation | pending | | |
| WP13 automatic evolution | pending | | |
| WP14 backfill/shadow | pending | | |
| WP15 cutover/cleanup | pending | | |
| WP16 final integration | pending | | |
| WP17 release/deploy | not authorized | | |

## Terra handoff prompt

Use the following as the first instruction after switching models:

```text
在 E:\eimemory 按照
docs/superpowers/specs/2026-08-20-l5-dynamic-cognitive-architecture-v3-design.md
和
docs/superpowers/plans/2026-08-20-l5-dynamic-cognitive-architecture-v3.md
执行重构。先完整读取两份文档，从 WP0 开始，保护现有脏工作树，不回滚用户改动。
逐工作包执行 contract/RED/implementation/GREEN/benchmark/review/evidence，实时更新计划
中的 Progress ledger。阶段内只跑定向测试，WP16 才运行一次全集测试。代码演进不增加人工
审批状态；推送和部署只有在当前对话得到明确授权后才能执行。
```

