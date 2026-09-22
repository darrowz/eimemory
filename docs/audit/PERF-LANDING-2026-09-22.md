# PERF landing — 2026-09-22

| Field | Value |
| --- | --- |
| Package | **1.13.17** |
| Date | 2026-09-22 (Asia/Shanghai) |
| Plan | `docs/audit/PERF-PLAN-2026-09-22.md` |
| Scope | `/workspace/eimemory` local box; no Hongxin/production deploy |

## Landed

| Item | Status | Commit(s) | Tests |
| --- | --- | --- | --- |
| **P0** FTS top-N equivalence safety net | closed (prior) | `50e9f35` | `tests/test_recall_perf_bounds.py` |
| **P1 §3.1** `_ensure_recall_schema_once` | **landed** | `6f764ce` | recall PRAGMA ≤2 after ensure; writes re-verify |
| **P1 §3.2** pollution-gate memoization | **landed** | `70ab5e3` | compute ≤ unique records; `updated_at` invalidates |
| **RET-07** `get_by_exact_refs` | **landed** (prior + test) | `c5b981a`, `33a4c91` | `tests/test_ret01_batch_hydrate.py` |
| **PERF-05** narrow indexes | **skipped** | n/a | Migration/index-contract surface not clear enough this wave; leave 12-col index |
| **§4.2 / §4.3** lexical prune | **not implemented** | n/a | Plan: unvalidated; changes result set |

## Local before/after (synthetic, this box)

Environment: Linux box / CPython in `.venv` / bundled SQLite. Absolute latency is not a production promise; PRAGMA/SQL structure is.

| Scenario | Before (HEAD `4e57512`) | After P1 §3.1 |
| --- | ---: | ---: |
| Full `MemoryAPI.recall` PRAGMA total (N≈30) | **442** | **0** (after warm `_ensure_recall_schema_once`) |
| `search_identity_candidates` PRAGMA | ~13 / call × many | cached → 0 on repeat |
| `_ensure_recall_schema_once(force=True)` | n/a | ~16 PRAGMA (write/migrate re-verify) |
| Direct `sqlite.search` PRAGMA | 0 | 0 |

Notes:
- Hot-path cost was `_recall_identity_physical_ready` (~34× per recall) doing `PRAGMA index_list/table_info/index_xinfo`.
- Schema cache is invalidated on upsert/rewrite and migrations; next ensure re-verifies.
- Pollution-gate memoization keys `(record_id, updated_at)` with maxsize 4096.

## Must-stay-green

```bash
python -m pytest tests/test_recall_perf_bounds.py tests/test_ret01_batch_hydrate.py -q
```

## Residuals

- PERF-05 wide index split needs a dedicated migration + `_source_partition_physical_ready` contract update.
- Lexical arm bm25/TEMP B-TREE remains; P0 snapshot must stay green before any ranking change.
- No claim of Linux production p95 improvement until `benchmarks/l5_v3_baseline.py` is re-run on the authority host.
