# Evaluation audit remediation — 2026-09-23

Source: `/workspace/audit-report-20260923b.txt` (copied to `EVALUATION-AUDIT-REPORT-2026-09-23.txt`).
Re-verified on master after recall absorb (`61dc50f`), then fixed on subsequent commits. **No Hongxin production deploy. No full-suite claim.**

## Finding → commit → focused tests

| Finding | Severity | Commit | Focused tests |
|---------|----------|--------|---------------|
| **RQG-1** tracemalloc external tracer forever blocking | P0 | `fe6f226` | `tests/test_production_real_query_gate.py` (external tracemalloc + deterministic gate) |
| **A1** bare `_lock`/`conn` + task_replay unlocked SQL | P0 | `88f0c71` | `tests/test_storage_conn_facade_inventory.py`, `tests/test_delegated_recall_review.py` (excl. pre-existing auth), `tests/test_production_query_dataset.py`, task_replay cases in `tests/test_public_benchmarks_and_replay.py` |
| **SEC-1** benchmark size/isolation + `publish_into` sealed | P0 | `a39191d` | `tests/test_evaluation_sec1_benchmark_bounds.py`, longmemeval/livingmem/public benchmarks |
| **A2** real_query_gate god-file / cycles / silent except | P1 | `9601a8f` | `tests/test_production_real_query_gate.py`, dataset + delegated review suites |
| **SEC-4** longmemeval fallback inflate + swallowed excepts | P1 | `7b1c280` | `tests/test_longmemeval_adapter.py`, public benchmarks |
| **CC-2** normalize_json_payload int digit limit | P1 | `2797929` | `tests/test_evaluation_p1_remediation.py` |
| **RW-1/3** reward recall_quality + success status words | P1 | `5697c13` | `tests/test_evaluation_p1_remediation.py` |
| **SEC-2** private cross-package imports → public API | P1 | `e19e432` | delegated review / explicit recall / task_replay / gate import paths |

## P0 notes

- **RQG-1:** `memory_measurement.mode=skipped_external_tracer`, `ok=True`, not blocking. Isolated tracemalloc path unchanged.
- **A1:** Added `RuntimeStore.fetch_latest_event_outcome_payload`; evaluation uses `locked()` / public fetch helpers. Inventory guards `getattr(..., "_lock")` and evaluation `getattr(..., "conn")`.
- **SEC-1:** `_benchmark_limits.py` enforces case/chunk/seed budgets; `run_*` refuse non-isolated runtimes unless `EIMEMORY_ALLOW_BENCHMARK_ON_RUNTIME=1` or isolation marker; `publish_into` calls `destination._require_mutable()` first.

## P1 notes

- **A2:** New `real_query_schema.py` (~456 lines) + `real_query_engine.py` / `real_query_baseline.py` facades. Gate still holds orchestration (~2847 lines) — further body moves deferred. Silent capacity/identity/proactive failures now log; proactive metrics carry `blocked_reasons`.
- **SEC-4:** Fallback returns `[]` with warning (no store-order inflation). Swallowed excepts logged. `_messages_text` uses `_text.extract_text_from_turn`.
- **CC-2:** `MAX_INT_DIGITS=4096` inside `_normalize_json_value`.
- **RW:** `_recall_quality` derives from hit/mrr metrics; success words include `completed`/`succeeded`.
- **SEC-2:** Public aliases with private backward-compat names.

## P2 not done this round

Protocol for recall evaluators, catalog seal RLock, probe evidence alignment, etc.

## Known unrelated failures (not introduced here)

- `tests/test_source_partition.py::test_evaluation_framework_seed_preserves_explicit_source_partition` — already failed on `c5cd2ec`.
- `tests/test_delegated_recall_review.py::test_authenticated_channel_review_reaches_dataset[*]` — `http_boundary.bearer_matches` expects `get_all` on headers; pre-existing on pre-A1 HEAD.

## Recall absorb (Phase 1)

See `docs/audit/RECALL-AUTHORITY-ABSORB-2026-09-23.md`. Absorb commit: `61dc50f`.
