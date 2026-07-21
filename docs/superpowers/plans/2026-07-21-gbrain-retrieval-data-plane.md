# GBrain-Inspired Retrieval Data Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` and TDD. Track every task in the SDD
> ledger; no version bump or deployment before final closure.

**Goal:** Add a scalable, explainable retrieval data plane without creating a
second authority or weakening channel isolation and L5 governance.

**Architecture:** A physical `CandidateSource` returns IDs/rank hints only. One
`GovernedRecallEngine` owns authoritative rehydration, boundary checks, graph,
hard gates, RRF, page pooling, feedback, and final bundles. SQLite remains the
authority and default source; optional Postgres is candidate-only.

**Tech stack:** Python 3.11+, stdlib, SQLite WAL/FTS5, optional psycopg 3,
pytest, RTK, code-review-graph, Open Code Review/MiniMax-M3, systemd immutable
deployments.

## Global gates

- Red-green-refactor for every behavior change.
- Preserve `channel -> source_id`; never infer authority from source alone.
- Do not add GBrain or an LLM to the authoritative path.
- Optional Postgres must timeout, circuit-break, audit, and bypass safely.
- Do not read full transcripts or materialize unbounded databases.
- Exactly one full test run after focused layers are green.
- Advance the patch only after independent review and live closure evidence.

### Task 1: Lock terminal receipt, redaction, idempotency, and atomicity

**Files:**
- Modify: `eimemory/governance/tool_receipts.py`
- Modify: `eimemory/adapters/runtime/service.py`
- Modify: `eimemory/adapters/codex/hook.py`
- Modify: `eimemory/adapters/hermes/provider_core.py`
- Modify: `integrations/hermes/eimemory/__init__.py`
- Modify: authenticated RPC server/transport configuration
- Modify: `eimemory/governance/capability_dashboard.py`
- Modify: terminal persistence/storage transaction boundary
- Test: `tests/test_tool_receipt_attestation.py`
- Test: `tests/test_runtime_terminal_evidence.py`
- Test: `tests/test_codex_adapter.py`
- Test: relevant event/outcome and L5 tests

- [ ] Red-test a separate producer/channel attestation credential; normal
  recall/MCP credentials cannot sign or submit attestations.
- [ ] Red-test Codex PostToolUse and Hermes post_tool_call issuance. Runtime
  computes digest/pass/policy from the bounded host result; arbitrary caller
  `passed=true`, echo, generic success, and terminal-time inline receipts fail.
- [ ] Red-test acceptance of one pre-existing eligible v2 receipt bound to
  channel/session/run/release/deployment/policy and one-time tool/trace use.
- [ ] Red-test receipt expiry, key rotation, cross-trace replay, structural
  secret redaction, signed-field allowlisting, and fail-closed missing key.
- [ ] Red-test deterministic adapter/outcome-trace IDs, two-connection races,
  and retry after failure between event/outcome/trace persistence.
- [ ] Implement `adapter.attest_tool_result`, server-side eligible verification
  policies, stored receipt-ID transport, runtime signing, and atomic/resumable terminal
  persistence while keeping old OpenClaw evidence readable, not newly trusted.
- [ ] Run focused receipt/terminal/L5 tests and request independent review.

### Task 2: Add explicit source partitions end to end

**Files:**
- Modify: `eimemory/models/records.py`
- Modify: `eimemory/storage/sqlite_store.py`
- Modify: `eimemory/storage/runtime_store.py`
- Modify: memory API and intake/replay/snapshot projections found by graph query
- Test: `tests/test_source_partition.py`
- Test: relevant storage migration, recall, replay, and adapter tests

- [ ] Red-test top-level serialization, legacy migration version/readiness,
  normalization/invalid/collision behavior, indexed filters, CRUD preservation,
  replay/snapshot, and channel/source non-leakage.
- [ ] Add NFKC/casefold/slug/128-char normalization and explicit per-kind
  backfill mapping; ambiguous hints stay `default` with diagnostics.
- [ ] Add records/recall-index columns, same-transaction backfill, covering
  indexes, and schema-ready migration ID.
- [ ] Thread source through contracts without changing channel scope authority.
- [ ] Run focused storage/migration/replay layers and request review.

### Task 3: Close shared authoritative mutations and Hermes lifecycle

**Files:**
- Modify: `eimemory/ei_bridge/protocol.py`
- Modify: `eimemory/adapters/eibrain/rpc.py`
- Modify: `eimemory/adapters/runtime/service.py`
- Modify: `eimemory/adapters/hermes/provider_core.py`
- Modify: authoritative memory/storage API as required
- Test: `tests/test_hermes_adapter.py`
- Test: `tests/test_runtime_adapter_rpc.py`
- Test: channel/source mutation race and fault-injection tests

- [ ] Red-test add/replace/remove, target record, expected revision,
  deterministic idempotency, supersedes/tombstone, provenance retention, and
  cross-channel/source rejection.
- [ ] Implement one mutation RPC/service. Hermes remains fail-open as a host
  plugin; the authoritative service fails closed on invalid mutation.
- [ ] Prove superseded/removed records leave recall and replay sees the chain.
- [ ] Run focused Hermes/RPC/storage/recall tests and request review.

### Task 4: Freeze CandidateSource and the single final RecallEngine

**Files:**
- Create: `eimemory/retrieval/__init__.py`
- Create: `eimemory/retrieval/contracts.py`
- Create: `eimemory/retrieval/engine.py`
- Create: `eimemory/retrieval/sqlite_source.py`
- Modify: `eimemory/api/memory.py`
- Modify: `eimemory/storage/runtime_store.py`
- Test: `tests/test_recall_engine_contract.py`
- Test: existing recall, graph, policy, and usage-feedback suites

- [ ] Red-test that candidate sources return IDs/hints only and cannot produce
  final envelopes or bypass channel/source/status/policy gates.
- [ ] Red-test the one final engine owns rehydration, graph, hard gates,
  feedback, fusion, page pooling, diagnostics, and final bundle ordering.
- [ ] Extract current orchestration without a second sort or behavior drift;
  wrap current SQLite candidate generation rather than duplicate ranking.
- [ ] Run focused recall/graph/policy tests and request review.

### Task 5: Implement deterministic explainable recall fusion

**Files:**
- Create: `eimemory/retrieval/fusion.py`
- Modify: SQLite candidate projection, record alias projection, recall response
- Test: `tests/test_recall_fusion.py`
- Test: existing recall-ranking and graph-recall suites

- [ ] Red-test RRF/component ranks/deterministic ties, exact title, unique and
  conflicting aliases, graph paths, evidence values, and create-safety.
- [ ] Red-test `target_source_id` vs search allowlist and cross-source ambiguity.
- [ ] Red-test page keys for pages, parents, documents, raw
  session/source-event chunks, and channel/source namespace isolation.
- [ ] Implement bounded fusion and per-page max-pool with stable explanations.
- [ ] Verify existing ranking fixtures and request review.

### Task 6: Add optional Postgres vector candidates and safe bypass

**Files:**
- Create: `eimemory/retrieval/postgres_source.py`
- Modify: candidate configuration/status/health
- Modify: `pyproject.toml` optional dependencies only if required
- Test: `tests/test_postgres_recall_source.py`

- [ ] Red-test bounded/prepared SQL, channel/source predicates, statement
  timeout, queue bound, circuit state, bad config, empty success, stale/partial
  IDs, and observable SQLite continuation.
- [ ] Implement an injectable candidate-only backend. Unit tests use a fake
  executor; real-Postgres integration is opt-in and cannot gate default mode.
- [ ] Verify default zero-dependency SQLite installation and request review.

### Task 7: Close proactive context and usage feedback

**Files:**
- Create: `eimemory/retrieval/proactive.py`
- Modify: shared runtime adapter service
- Modify: OpenClaw, Codex, and Hermes prefetch/injection adapters
- Extend: existing memory-usage feedback and bounded audit records
- Test: `tests/test_proactive_recall.py`
- Test: adapter-specific proactive integration tests

- [ ] Red-test latest four turns, deterministic entity/intent extraction,
  confidence >=0.70, max three, same-session dedupe, and prompt safety.
- [ ] Red-test cache keys include scope/source/policy/release and scope changes
  invalidate state.
- [ ] Red-test volunteered/injected/used/rejected transitions and control cohort.
- [ ] Implement one bounded service without transcript reads; run all adapter
  and feedback replay layers and request review.

### Task 8: Extend the eligible real-query gate and bind release/L5

**Files:**
- Modify: `eimemory/evaluation/production_recall.py`
- Modify: capture/redaction and CLI/nightly entry points
- Modify: `eimemory/governance/release_closure.py`
- Modify: `eimemory/governance/l5_readiness.py`
- Test: `tests/test_production_recall.py`
- Test: `tests/test_release_closure.py`
- Test: `tests/test_l5_readiness.py`

- [ ] Red-test eligible production-redacted provenance, immutable dataset and
  baseline digests, minimum labelled samples/per-channel coverage, and
  exclusion of generated/smoke/unlabelled cases.
- [ ] Red-test formulas and thresholds for Recall@5, Precision@5, MRR, nDCG@5,
  Top-1 stability, Jaccard@K, p50/p95, peak memory, and proactive feedback.
- [ ] Red-test any channel/source leak blocks and synthetic probes cannot pass.
- [ ] Persist only digests/labels/metrics/redacted features, never raw query or
  returned text.
- [ ] Bind dataset/baseline/policy/result digests to current version, commit,
  deployment receipt, release closure, and L5; run focused layers and review.

### Task 9: Performance, third-party audit, release, and deployment

**Files:**
- Modify: `README.md`, `docs/architecture.md`, `docs/evaluation.md`,
  `docs/operations.md`, changelog/version files
- Add: focused performance and production closure evidence as needed

- [ ] Verify candidate/query/index bounds, keyset scans, SQLite plans/indexes,
  optional queue/timeouts, cache scope keys, latency, and peak memory.
- [ ] Run focused layers followed by exactly one full suite.
- [ ] Rebuild code-review-graph and run complete branch-diff OCR with
  MiniMax-M3; triage/fix every P0/P1 and rerun only affected verification.
- [ ] Request final spec and quality reviews from fresh subagents.
- [ ] Advance one patch version, generate release closure, merge `master`, push,
  and deploy the exact master commit as an immutable honxin release.
- [ ] Verify immutable path/package digest, health identity, three services,
  deployment receipt, real-query gate, zero leakage, and L5 on one identity.
