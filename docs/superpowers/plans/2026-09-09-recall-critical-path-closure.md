# Recall Critical Path and Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. The operator requested final submission/deployment, so only the controller commits and releases the integrated change.

**Goal:** Remove local-search starvation of mandatory evidence, make SQLite reads respect recall deadlines, and align technical release success with explicitly reported business acceptance.

**Architecture:** Split exact-identity lookup from hybrid scoring for mandatory fragment consumers. Reuse a single pure release-impact policy for lineage and installer closure decisions. Keep all authority validation and formal evidence provenance intact.

**Tech Stack:** Python 3.14, SQLite, PostgreSQL/psycopg, pytest, Bash/systemd immutable deployment.

## Global Constraints

- Keep the default total recall budget at 3 seconds and the existing authority/admission thresholds.
- Keep production authentication, tenant/source partitions, payload digests, snapshot/rollback safeguards, and PostgreSQL dependency installation intact.
- Never label deliberate maintenance calls as natural traffic or invent host/external delivery receipts.
- Do not equate historical task evidence with an authoritative latest task state.
- Do not alter production during implementation or instantiate Runtime on live storage for a replay.
- Only the controller commits and deploys the reviewed integrated change.

### Task 1: Unify release impact and truthful completion status

**Files:** `eimemory/governance/release_lineage.py`, new dependency-free `eimemory/governance/release_impact.py`, new `deploy/release_impact.py`, `deploy/install_immutable_release.sh`, `deploy/summarize_release_closure.py` only if needed, focused release-impact/lineage/installer tests.

**Interfaces:** The pure impact module owns `DOMAINS`, `DOMAIN_PATHS`, path classification and a JSON-safe impact report. Existing lineage imports/re-exports compatible names. A CLI accepts a repository and exact prior/current commit, prints only classified paths/domains/reason and exits 0 when closure is required, 1 only when safely lightweight, 2 on failure (installer treats failure conservatively).

- [x] RED: add real git-fixture tests that a retrieval-only change, `models/records.py`, or unknown production Python path requires closure, while documentation-only does not; model changes are classified and unknown changes remain conservative. Example assertion: `assert impact['requires_closure'] is True` with `impact['unknown_production_paths'] == []` for known model paths.
- [x] RED: installer/summary tests distinguish a skipped closure and valid accumulating report from a completed closure; neither may emit an unqualified whole-business `complete` claim. Preserve strict transaction behavior.
- [x] GREEN: extract/reuse the existing classification semantics instead of duplicating its path list, replace the independent shell whitelist, and carry an explicit business closure outcome alongside technical status. Shared `models` contracts may affect all six domains when justified, but must no longer be unclassified.
- [x] Verify: run the focused impact/lineage/installer/summary tests under a clean test environment; report commands and failures/passes; no release or production reads are required for these tests.
- [x] Review this task's diff independently before integration.

### Task 2: Put required evidence ahead of full local hybrid scoring

**Files:** `eimemory/retrieval/sqlite_source.py`, `postgres_vector.py`, `engine.py`, tests `test_recall_budget_reserve.py`, `test_postgres_vector_source.py`, `test_recall_local_work.py`.

**Interfaces:** `SQLiteCandidateSource.search_identity(request) -> CandidateBatch` reuses existing exact-title/alias verification; `.search(request)` retains the full legacy hybrid behavior. The engine marks mandatory lightweight-fragment requests through an internal recall-filter flag. Only the matching fragment source uses identity-only local candidate lookup for those requests; authority hydration remains unchanged.

- [x] RED: a real SQLite/engine/lightweight-admission test returns the relevant verified fragment while the full local hybrid scorer is instrumented to fail if invoked in mandatory mode; baseline must fail. Add exact-identity, unavailable-backend, invalid-fragment and ordinary hybrid controls.
- [x] GREEN: extract the existing identity row verification without weakening its scope/source/status/quality checks. Use the cheap batch in mandatory fragment mode and preserve PostgreSQL query, index recheck, row validation and per-request evidence proof.
- [x] Verify: focused recall/authority/fragment/task/L0 tests, followed by an isolated same-data replay with production dependencies. Expected: evidence found for all four targeted query types, with full SQLite FTS removed from the mandatory normal-query path rather than a larger deadline.

### Task 3: Bound SQLite recall reads and preserve connection state

**Files:** new `eimemory/storage/recall_deadline.py` if a focused helper is appropriate, `sqlite_store.py`, `runtime_store.py`, `retrieval/sqlite_source.py`, new focused SQLite deadline tests.

**Interfaces:** A lock-owned read-deadline context applies the absolute collection cutoff to SQLite progress/busy waiting, restores prior owned state on normal/error/nested exits, and identifies only its own deadline cancellation. Non-deadline callers retain existing behavior.

- [x] RED: a real long-running SQLite recursive read is interrupted at its short request deadline; after exit `SELECT 1` and normal store writes still work. Cover genuine SQL error propagation, nested cleanup and incomplete-search reporting.
- [x] GREEN: install request-owned cancellation while holding the store lock, bound lock waiting where applicable, and translate only deadline-caused interruption into a structured incomplete result. Do not swallow arbitrary OperationalError or cancel another request.
- [x] Verify: deadline tests plus existing SQLite, identity, recall budget and authority tests; confirm no production SQLite connection is used in test fixtures.

### Task 4: First-request and formal acceptance, then release

**Files:** bounded replay/acceptance harness in `benchmarks/` or private diagnostic directory, audit report, version metadata only after code verification.

- [x] Run each of the four queries first in independent processes against the existing isolated snapshot using production dependencies and PostgreSQL read-only transactions; preserve one immutable result file per run. Also run a bounded concurrent-request case. Do not call these OS-cache-cold or natural production traffic.
- [x] Require the original first-query failure to disappear in the predefined suite before deployment. If it fails, diagnose changed evidence rather than repeat unchanged runs until green.
- [ ] Resolve the latest-task authority source requested asynchronously; implement a source-backed current-state contract only once it is known. Otherwise record the exact external dependency instead of claiming current-state completeness.
- [ ] Review the whole diff, run the consolidated risk-based tests, bump aligned version metadata, commit and push.
- [ ] Deploy through the immutable installer with PostgreSQL extras and post-switch gates retained, explicitly requesting full closure for this acceptance. Wait for installer completion before online testing.
- [ ] Run native online query acceptance and read formal closure results. Fix in-scope defects revealed by gates; never forge natural datasets/labels/host receipts. Report the exact complete or externally blocked outcome and preserve all failures.

### Pre-release evidence

- Final retrieval/integration/version regression: 497 passed in 35.11 seconds.
- Final isolated real-data replay: 16/16 checks passed in four independent processes, each query first once; 1,392.51–2,562.51 ms. Two requests sharing one runtime both passed, maximum 2,476.83 ms. Same predefined checks, no increased budget or disabled safeguards.
- Task reviews corrected authority snapshot, canonical probe, hydration, identity and final validation deadline escapes, plus the successful pre-observation outcome. No remaining Critical/Important findings in those reviewed scopes.
- Latest task authority remains an explicit external dependency: the user was asked which ledger/system is authoritative. Historical recall continues to mark latest state unverified; no current-state claims or fabricated materialization.
