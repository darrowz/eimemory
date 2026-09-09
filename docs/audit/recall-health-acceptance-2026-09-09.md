# Recall health and budget follow-through — 1.13.7 candidate

## Scope and root cause

Baseline: c6d2e9d / 1.13.6. The operator requested diagnosis, then explicitly
authorized implementation, commit, deployment and acceptance. This document
records pre-deployment evidence; serving identity and live acceptance must be
read back independently after deployment.

The original production acceptance was 11/12; its first L1 call returned
unavailable in 2675 ms. The subsequent 22:05 reacceptance was 12/12. Neither
artifact was overwritten. Source health subsequently recovered to available,
so the earlier unavailable snapshot was not proof of a persistent outage.

Source health mixed the last PostgreSQL attempt with backend availability.
A later scope's request budget cancellation cleared last-query validity, even
after an earlier query had verified the same index identity. The existing
1.13.6 admission fix correctly preserved this request's verified batches; the
health display still could not explain that distinction.

A fresh consistent SQLite backup and copied payload segments were replayed
with production Python dependencies and PostgreSQL read-only transactions.
No Runtime was constructed on production storage. The instrumented baseline
reproduced 10/12 evidence_found: first L1 exhausted collection before any
successful PostgreSQL batch (SQLite 1626 ms, initial index read 416 ms); one
named-task call spent 2636 ms in local search and missed hydration. The latter
incorrectly reported no_evidence rather than unavailable. These measurements
establish mechanisms on the snapshot, not the exact cause of every historic
production failure.

## Changes and safeguards

- Backend health requires a successfully queried, still-verified index
  identity. Budget cancellation preserves that proof only; real failures or
  incompatible identity refresh revoke it. No prior successful query means no
  availability claim. Admission continues using its separate request-local
  proof and is not reopened by health.
- Public health now exposes index_verified, query_valid and last_query_status.
  The last-query fields describe the last **PostgreSQL attempt**; empty/local
  identity shortcuts do not perform a new PostgreSQL query. last_error remains
  the existing diagnostic and is not a service-liveness boolean.
- Request-local source timings cover authority probes, SQLite, index reads,
  embedding gate/wait, PostgreSQL search and validation. Embedding wait excludes
  overlapped local work; it is not total embedding transport duration. Failed
  waits record the failed stage. Worker completion cannot mutate timing state.
- Compact/native RPC results retain bounded numeric stage totals, source
  counts, budget exhaustion and admission reasons. No queries, scopes, raw
  exceptions, URLs, credentials or arbitrary diagnostic keys are added.
- Empty searches interrupted by collection/hydration limits return unavailable,
  not a false absence claim. Verified nonempty results remain admissible.
- Task results explicitly say historical_only_latest_state_unverified. This
  does not construct an authoritative task-state materializer or certify that
  all updates were ingested.
- Local lexical matching computes only requested token membership instead of
  allocating every unrelated Chinese bigram; queries with more than 64 terms
  keep the existing linear full-token path. Chinese phrase substring semantics,
  mixed-language token boundaries and output order remain unchanged. Character
  whitespace normalization uses split/join with the same Unicode semantics.
  JSON-shaped score hints are bounded/frozen once at CandidateHit construction.
- Default time budgets, admission thresholds, source partitions, projection
  digests, authority checks, authentication and circuit failure thresholds are
  unchanged. No cross-scope result cache or extra persistent cache was added.

## Verification before deployment

- New health, timing, lexical-work and incomplete-search tests failed on the
  old behavior before their fixes. A corrected quality-accepted fixture and
  four-test differential rerun against c6d2e9d confirmed all four failures
  (record bigram materialization, per-character normalization, duplicate hint
  freezing, missing compact diagnostics); no checkout files were reverted.
- 200 deterministic mixed-language query/record cases preserve exact token
  membership. Existing lexical, authority, source, fragment and native RPC
  tests remain passing.
- Consolidated focused suite: **479 passed in 31.82 seconds**, 19 test files.
  JUnit evidence: `/home/darrow/tmp/eimemory-health-focused-tests.xml`.
- Same-snapshot optimized replay: **12/12 evidence_found**, 1247–2936 ms,
  including the first-query L1 case. Saved separately as
  `/home/darrow/tmp/eimemory-health-acceptance-optimized.json`; the failed
  instrumented baseline remains
  `/home/darrow/tmp/eimemory-health-acceptance-baseline.json`.
- A local-source cProfile run on 48 snapshot candidates changed from 1048 ms
  and 956501 calls to 644 ms and 361938 calls. This is a profiled micro-measurement,
  not a production SLA or a statistically controlled end-to-end benchmark.
- Independent read-only review of both change batches found no Important or
  Critical issue. git diff --check passed.

## Acceptance boundaries

The named task remains close to the collection/validation limits. Failures
must be retained and investigated, not hidden by rerunning until green.
Deployment must use the existing immutable installer with post-switch gates,
PostgreSQL dependencies and rollback safeguards. Live query acceptance,
deployment receipts, real host consumption/attestation, formal release closure
and L5 are different evidence chains. A lightweight deployment and deliberate
functional acceptance must never manufacture natural-quality or autonomous-L5
evidence. Current-release online results belong in the post-deployment receipt.
