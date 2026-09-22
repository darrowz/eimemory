# Remediation status — 2026-09-22 audits

| Field | Value |
| --- | --- |
| Base (start) | `d25c9b5` (GOV-01 promotion_manager missing L0/L1 scores) |
| Package | **1.13.18** |
| Final HEAD | `21c93f3` a822bfa |
| Date | 2026-09-22 (Asia/Shanghai) |
| Scope | `/workspace/eimemory` only; pushed to origin/master; **no** production Hongxin deploy on this box (`/opt/eimemory` absent) |

## Closed (zero open / partial / skipped)

| ID | Status | Commit | Tests |
| --- | --- | --- | --- |
| **GOV-01** (promotion `_rollout_gate`) | **closed** (prior) | `d25c9b5` | `tests/test_audit_security_boundaries.py::test_missing_safety_regression_scores_fail_closed` |
| **GOV-01** completeness (`learning_eval` defaults) | **closed** | `b4b6189` | `tests/test_learning_eval.py` |
| **GOV-02** ledger fail-closed | **closed** | `e0d1608` | `tests/test_gov02_ledger_fail_closed.py` |
| **SCH-01** nightly missing ok | **closed** | `66a4a97` | `tests/test_sch01_nightly_ok_semantics.py`, `tests/test_business_closure_bc.py` |
| **SCORE-01** missing confidence / evaluator baseline | **closed** | `58315c3` | `tests/test_research_evidence_gate.py`, `tests/test_scoring.py` |
| **RET-01 / RET-07** batch hydrate `get_by_exact_refs` | **closed** | `c5b981a`, `33a4c91` | `tests/test_ret01_batch_hydrate.py` |
| **LOCK-01** unbound lock + rebuild/maintain wrappers | **closed** | `e686abf`, `f7043db` | `tests/test_lock01_sqlite_guard.py` |
| **RET-02** `id(item)` keys | **closed** | `dc86649` | `tests/test_phase_c_arch_sch_ret.py` |
| **SCH-02** sandbox status | **closed** | `b7f2550` | `tests/test_phase_c_arch_sch_ret.py` |
| **ARCH-02** vector_sync Runtime import | **closed** | `ba92d3a` | import AST check in phase-c test |
| Symlink `lexists` defense | **closed** | `0421b9b` | `tests/test_phase_c_arch_sch_ret.py` |
| **ARCH-01** Data↔Control cycle + allowlist | **closed** | `9b6890c`, `3a31c93` | `tests/test_arch01_import_boundaries.py` (zero allowlist) |
| **B01** active-surface exclusive lease | **closed** | `9bdac13` | `tests/test_b01_b02_promotion_boundaries.py` |
| **B02** artifact rollback + production-deploy undo | **closed-by-design** (fail-closed durable reconciliation) | `db4afb2`, `241c934` | `tests/test_b01_b02_promotion_boundaries.py` |
| **PERF P0** FTS top-N safety net | **closed** | `50e9f35` | `tests/test_recall_perf_bounds.py` |
| **PERF P1 §3.1** schema PRAGMA dedup | **closed** | `6f764ce` | `tests/test_recall_perf_bounds.py` |
| **PERF P1 §3.2** pollution-gate memoization | **closed** | `70ab5e3` | `tests/test_recall_perf_bounds.py` |
| **PERF-05** narrow indexes | **closed** | `ec9ca42` | `tests/test_source_partition.py` |
| **§4.2 / §4.3 lexical** | **closed: rejected** after counterexample; default OFF locked by P0 | `b0ae89f` | `tests/test_perf_lexical_rejected.py`, `tests/test_recall_perf_bounds.py` |
| **SECURITY §4** mid-flight + digest + health identity + lease reread | **closed** | `1170917`, `c72fb7d` | `tests/test_b01_b02_promotion_boundaries.py`, `tests/test_security_section4_closure.py` |
| **Recall #5** identity lookup | **closed** (re-checked on Runtime+sqlite) | prior wave | Runtime+sqlite identity path |
| **Recall #6–8** real-chain | **closed** (local stub+SQLite stand-in) | `661aafc` | `tests/test_recall_real_chain_local_closure.py` |
| **GOV-03** auto-commit/deploy defaults | **kept closed** | n/a | unchanged |

## Residuals

**None.** Every previously open/partial/skipped row is closed or closed-by-design with commit SHA.

### B02 operator procedure (closed-by-design)

Production deploy undo cannot be performed from this box (`/opt/eimemory` absent). Rollback of `production_applied` artifacts enters durable `artifact_rollback_required` with ledger event and never returns ok. Operators must restore the prior release via deploy/receipts, then clear the flag only after effect-owner digest reconciliation confirms prior digests.

## Verification (focused)

```text
pytest -q \
  tests/test_lock01_sqlite_guard.py \
  tests/test_source_partition.py \
  tests/test_perf_lexical_rejected.py \
  tests/test_security_section4_closure.py \
  tests/test_b01_b02_promotion_boundaries.py \
  tests/test_arch01_import_boundaries.py \
  tests/test_recall_real_chain_local_closure.py \
  tests/test_recall_perf_bounds.py
# → 61 passed (pre-release focused band)
```

Production Hongxin deploy **not** performed on this box.
