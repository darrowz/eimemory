# PERF landing — 2026-09-22

| Field | Value |
| --- | --- |
| Package | **1.13.18** |
| Date | 2026-09-22 (Asia/Shanghai) |
| Plan | `docs/audit/PERF-PLAN-2026-09-22.md` |
| Scope | `/workspace/eimemory` local box; no Hongxin/production deploy |

## Landed (zero open / skipped / partial)

| Item | Status | Commit(s) | Tests |
| --- | --- | --- | --- |
| **P0** FTS top-N equivalence safety net | **closed** | `50e9f35` | `tests/test_recall_perf_bounds.py` |
| **P1 §3.1** `_ensure_recall_schema_once` | **closed** | `6f764ce` | recall PRAGMA ≤2 after ensure; writes re-verify |
| **P1 §3.2** pollution-gate memoization | **closed** | `70ab5e3` | compute ≤ unique records; `updated_at` invalidates |
| **RET-07** `get_by_exact_refs` | **closed** | `c5b981a`, `33a4c91` | `tests/test_ret01_batch_hydrate.py` |
| **PERF-05** narrow indexes | **closed** | `ec9ca42` | `tests/test_source_partition.py` |
| **§4.2 / §4.3** lexical prune | **closed: rejected** | `b0ae89f` | `tests/test_perf_lexical_rejected.py` — default OFF; P0 locks path |

## Local before/after (synthetic, this box)

Environment: Linux box / CPython in `.venv` / bundled SQLite. Absolute latency is not a production promise; PRAGMA/SQL structure is.

| Scenario | Before (HEAD `4e57512`) | After P1 §3.1 |
| --- | ---: | ---: |
| Full `MemoryAPI.recall` PRAGMA total (N≈30) | **442** | **0** (after warm `_ensure_recall_schema_once`) |
| `search_identity_candidates` PRAGMA | ~13 / call × many | cached → 0 on repeat |
| `_ensure_recall_schema_once(force=True)` | n/a | ~16 PRAGMA (write/migrate re-verify) |
| Direct `sqlite.search` PRAGMA | 0 | 0 |

## Must-stay-green

```bash
python -m pytest tests/test_recall_perf_bounds.py tests/test_ret01_batch_hydrate.py tests/test_perf_lexical_rejected.py tests/test_source_partition.py -q
```

## Lexical §4.2/§4.3 closure note

Rank-only / bounded-inner / df-prune change top-N under pervasive bm25 ties (plan counterexample). Implementation ships an explicit **rejected/disabled** path: `EIMEMORY_LEXICAL_DF_PRUNE` / `EIMEMORY_LEXICAL_TIEGROUP_OPT` default OFF; without `EIMEMORY_LEXICAL_PRUNE_QUALITY_GATE=1` the helper is a no-op. Default FTS SQL remains composite `ORDER BY bm25 ASC, quality DESC, updated_at DESC`, locked by P0.

## Residuals

**None.** PERF-05 and §4.2/§4.3 are closed (landed / rejected-with-safe-default).

## Local deploy / smoke (this box)

| Item | Result |
| --- | --- |
| `/opt/eimemory` | **absent** — no production Hongxin deploy |
| Package identity | `1.13.18` |
| Local smoke | doctor + RPC health expected `1.13.18` after release install under `/workspace` |
