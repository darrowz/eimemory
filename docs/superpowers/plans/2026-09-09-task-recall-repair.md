# Task recall repair implementation plan

**Goal:** Native recall of task progress/history returns task evidence rather than an old preference, without opening audit/log access, and retrieval leaves time for final authority validation.

**Architecture:** Classify explicit task progress/history queries before the native research fallback. Open only eligible memory evidence in the task-context lane and preserve exact scope, source, status, pollution and final authority checks. Separate candidate collection, admission and hard deadlines; bound optional work and reuse query preparation within a request, never cache authorization across requests.

**Tech stack:** Python, SQLite, optional PostgreSQL fragment retrieval, pytest.

## Constraints and choices

- Implement in this workspace; no production configuration edits, restart or deployment.
- Prefer a narrow task-evidence route over globally enabling report/operational recall.
- Prefer bounded collection plus validation reserve over merely increasing the timeout.
- Do not change global score thresholds without labeled validation. Synthetic fixtures demonstrate semantics, not production calibration or latency.
- No full suite or L5 release closure is required for this local repair; run changed and adjacent focused tests.

## Execution checklist

- [x] Task route: native default and query intent routing, narrow evidence filtering, Chinese/English status/history tests, source/scope/audit negatives. Native RPC-to-loadout regression also found and fixed unconditional conversation removal.
- [x] Admission: preference-before-score-gap exclusion, two eligible task results, answer-shape checks, separate timeout and authority labels. Effective score configuration remains observable and unchanged.
- [x] Budget: collection/hydration/admission reserves in all modes, SQLite collection and per-row cutoffs, remote embedding/database remaining timeouts, no caller-assistance deadline extension. Timed-out SQL cancellation does not trip availability circuit.
- [x] Local overhead: JSON parsing reduced from twice to once per candidate; repeated Chinese boundary scans replaced with indexed intervals; projection cache excludes scheduling cutoff but retains authority fences. Recorded synthetic kernel timings and output equivalence.
- [x] Verification: failing regressions preceded implementation, final focused batch 405 passed in 24.95s. Independent review findings fixed and rereviewed. Evidence and live calibration limits recorded in `docs/audit/task-recall-remediation-2026-09-09.md`.

## Acceptance

The native tool's default query `最近已授权任务、进展、待验收` must reach a task-specific route. A temp-store integration case must return matching task evidence, not an older authorization preference, while other-user/source records and logs remain excluded. An exhausted validation loop reports timeout separately from actual failed authority checks. Deterministic multi-scope tests prove candidate work stops before the hard deadline and already-collected evidence can still be validated. Production latency and label-based threshold quality remain unclaimed until measured on production-equivalent data.
