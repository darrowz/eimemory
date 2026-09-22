# Remediation status — 2026-09-22 audits

| Field | Value |
| --- | --- |
| Base (start) | `d25c9b5` (GOV-01 promotion_manager missing L0/L1 scores) |
| Package | **1.13.17** |
| Final HEAD | `a2c2fad` (1.13.17 tip) |
| Date | 2026-09-22 (Asia/Shanghai) |
| Scope | `/workspace/eimemory` only; pushed to origin/master; **no** production Hongxin deploy on this box (`/opt/eimemory` absent) |

## Closed / partial / open

| ID | Status | Commit | Tests |
| --- | --- | --- | --- |
| **GOV-01** (promotion `_rollout_gate`) | **closed** (prior) | `d25c9b5` | `tests/test_audit_security_boundaries.py::test_missing_safety_regression_scores_fail_closed` |
| **GOV-01** completeness (`learning_eval` defaults) | **closed** | `b4b6189` | `tests/test_learning_eval.py` |
| **GOV-02** ledger fail-closed | **closed** | `e0d1608` | `tests/test_gov02_ledger_fail_closed.py` |
| **SCH-01** nightly missing ok | **closed** | `66a4a97` | `tests/test_sch01_nightly_ok_semantics.py`, `tests/test_business_closure_bc.py` |
| **SCORE-01** missing confidence / evaluator baseline | **closed** | `58315c3` | `tests/test_research_evidence_gate.py`, `tests/test_scoring.py` |
| **RET-01 / RET-07** batch hydrate `get_by_exact_refs` | **closed** | `c5b981a`, `33a4c91` | `tests/test_ret01_batch_hydrate.py` |
| **LOCK-01** unbound lock fail-closed | **closed** (partial wrap) | `e686abf` | `tests/test_lock01_sqlite_guard.py`, `tests/test_storage.py` |
| **RET-02** `id(item)` keys | **closed** | `dc86649` | `tests/test_phase_c_arch_sch_ret.py` |
| **SCH-02** sandbox status | **closed** | `b7f2550` | `tests/test_phase_c_arch_sch_ret.py` |
| **ARCH-02** vector_sync Runtime import | **closed** | `ba92d3a` | import AST check in phase-c test |
| Symlink `lexists` defense | **closed** | `0421b9b` | `tests/test_phase_c_arch_sch_ret.py` |
| **ARCH-01** Data↔Control cycle | **closed** (migration residual) | `9b6890c` | `tests/test_arch01_import_boundaries.py` |
| **B01** active-surface exclusive lease | **closed** | `9bdac13` | `tests/test_b01_b02_promotion_boundaries.py` |
| **B02** artifact rollback undo | **closed** (deploy residual) | `db4afb2` | `tests/test_b01_b02_promotion_boundaries.py` |
| **PERF P0** FTS top-N safety net | **closed** | `50e9f35` | `tests/test_recall_perf_bounds.py` |
| **PERF P1 §3.1** schema PRAGMA dedup | **closed** | `6f764ce` | `tests/test_recall_perf_bounds.py` |
| **PERF P1 §3.2** pollution-gate memoization | **closed** | `70ab5e3` | `tests/test_recall_perf_bounds.py` |
| **SECURITY §4** promotion mid-flight + watch orphan check | **partial** | `1170917` | `tests/test_b01_b02_promotion_boundaries.py` |
| **GOV-03** auto-commit/deploy defaults | **kept closed** | n/a | unchanged |

## Residuals

### ARCH-01
- Storage hot paths no longer import Control/Recall at module level; shared types live in `eimemory.contracts/`.
- **Allowlist residual:** `storage/migrations/backfill_capability_v3.py` still imports `eimemory.capabilities.*` (one-shot ops script).

### LOCK-01 (partial wrap — left documented)
- Public `execute`/`commit`/`rollback` wrappers assert lock ownership; RuntimeStore hot BEGIN/commit/rollback use wrappers.
- Residual bare `conn.execute` remains for PRAGMA/diagnostics and rebuild SQL. Mutate paths still hold `_lock`.

### B02
- Supported undo: intent_pattern; rule/playbook/memory status rewrite; **code_patch worktree restore** from durable backups when `production_applied` is false.
- **Still impossible / fail-closed:** code artifacts without backups; **production deploy undo** — `artifact_rollback_required`, never ok.

### PERF
- P0+P1 landed; see `docs/audit/PERF-LANDING-2026-09-22.md`.
- PERF-05 narrow indexes **skipped** (migration path unclear).
- §4.2/§4.3 lexical prune **not** implemented (unvalidated; changes results).

### Security pack §4 e2e
- **Landed this wave:** promotion post-apply persist failure → `requires_reconciliation`; `check_promotion_watch_orphans` fail-closed scan.
- **Still open:** full effect-owner digest reconciliation; production health identity binding; scheduler lease reread; claiming production L5.

### Recall backlog #5–8
- #5 identity lookup / empty: **re-checked on HEAD** — `search_identity_candidates` returns exact_title hits when identity indexes ready; no empty-backfill bug reproduced on Runtime+sqlite.
- #6–8 real-chain: local Runtime+sqlite pytest exercised; prod embeddings/PG paths need authority host.

## Verification (focused)

```text
pytest -q \
  tests/test_recall_perf_bounds.py \
  tests/test_ret01_batch_hydrate.py \
  tests/test_b01_b02_promotion_boundaries.py \
  tests/test_arch01_import_boundaries.py \
  tests/test_lock01_sqlite_guard.py
```

Full suite not claimed green unless run separately. Production Hongxin deploy **not** performed on this box.
