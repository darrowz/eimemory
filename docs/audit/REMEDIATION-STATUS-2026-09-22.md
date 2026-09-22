# Remediation status — 2026-09-22 audits

| Field | Value |
| --- | --- |
| Base (start) | `d25c9b5` (GOV-01 promotion_manager missing L0/L1 scores) |
| Prior HEAD (this turn start) | `76d93ce` |
| Final HEAD | `0a23f72f619c6a0585cfa011787efb00395d9693` |
| Date | 2026-09-22 (Asia/Shanghai) |
| Scope | `/workspace/eimemory` only; pushed to origin/master; no prod deploy |

## Closed / partial / open

| ID | Status | Commit | Tests |
| --- | --- | --- | --- |
| **GOV-01** (promotion `_rollout_gate`) | **closed** (prior) | `d25c9b5` | `tests/test_audit_security_boundaries.py::test_missing_safety_regression_scores_fail_closed` |
| **GOV-01** completeness (`learning_eval` defaults) | **closed** | `b4b6189` | `tests/test_learning_eval.py` |
| **GOV-02** ledger fail-closed | **closed** | `e0d1608` | `tests/test_gov02_ledger_fail_closed.py` |
| **SCH-01** nightly missing ok | **closed** | `66a4a97` | `tests/test_sch01_nightly_ok_semantics.py`, `tests/test_business_closure_bc.py` |
| **SCORE-01** missing confidence / evaluator baseline | **closed** | `58315c3` | `tests/test_research_evidence_gate.py`, `tests/test_scoring.py` |
| **RET-01** batch hydrate | **closed** | `c5b981a` | `tests/test_ret01_batch_hydrate.py` |
| **LOCK-01** unbound lock fail-closed | **closed** (partial wrap) | `e686abf` | `tests/test_lock01_sqlite_guard.py`, `tests/test_storage.py` |
| **RET-02** `id(item)` keys | **closed** | `dc86649` | `tests/test_phase_c_arch_sch_ret.py` |
| **SCH-02** sandbox status | **closed** | `b7f2550` | `tests/test_phase_c_arch_sch_ret.py` |
| **ARCH-02** vector_sync Runtime import | **closed** | `ba92d3a` | import AST check in phase-c test |
| Symlink `lexists` defense | **closed** | `0421b9b` | `tests/test_phase_c_arch_sch_ret.py` |
| **ARCH-01** Data↔Control cycle | **closed** (migration residual) | `9b6890c` | `tests/test_arch01_import_boundaries.py` |
| **B01** active-surface exclusive lease | **closed** | `9bdac13` | `tests/test_b01_b02_promotion_boundaries.py` |
| **B02** artifact rollback undo | **closed** (deploy residual) | `db4afb2` | `tests/test_b01_b02_promotion_boundaries.py` |
| **PERF P0** FTS top-N safety net | **closed** | `50e9f35` | `tests/test_recall_perf_bounds.py` |
| **GOV-03** auto-commit/deploy defaults | **kept closed** | n/a | unchanged |

## Residuals

### ARCH-01
- Storage hot paths no longer import Control/Recall at module level; shared types live in `eimemory.contracts/` (capability models/validators, `ReleaseIdentity`, evidence-query policy).
- `sqlite_store` lazy-binds `governance.policy_rollout` symbols via module globals (no module-level import).
- **Allowlist residual (justified):** `storage/migrations/backfill_capability_v3.py` still imports `eimemory.capabilities.*` — one-shot dual-write ops script must call Observation/Registry APIs that cannot live in contracts. Comment in `tests/test_arch01_import_boundaries.py`.

### LOCK-01 (partial wrap — left documented)
- Public `execute`/`commit`/`rollback` wrappers assert lock ownership; RuntimeStore hot BEGIN/commit/rollback use wrappers.
- Residual bare `conn.execute` remains for PRAGMA/diagnostics and rebuild SQL (`runtime_store.rebuild_sqlite_from_jsonl`, maintenance RO connects). Routing those through the wrapper without breaking migrations/rebuild was judged risky this pass; mutate paths used by RuntimeStore still hold `_lock`.

### B02
- Supported undo: intent_pattern; rule/playbook/memory status rewrite; **code_patch worktree restore** from durable `code_apply` transaction backups when `production_applied` is false and repo root matches `EIMEMORY_AUTONOMOUS_CODE_REPO`.
- **Still impossible / fail-closed:** code artifacts without recoverable backups; paths not in the transaction; **production deploy undo** (no deploy access) — returns `artifact_rollback_required`, never ok.
- Full effect-owner digest reconciliation beyond backup verify not claimed.

### PERF (P0 only)
- P1 schema PRAGMA dedup / pollution-gate memoization **not** landed (out of this pass).
- No ranking semantics changes.

### Security pack §4 e2e notes
- Non-code promotion multi-write crash window, orphan/watch reconciliation, outcome idempotency, health identity binding, scheduler lease reread — **open** (SECURITY-BOUNDARIES-AUDIT §4; beyond B01/B02 wiring).

## This-turn commits

| SHA | Summary |
| --- | --- |
| `9b6890c` | ARCH-01: sink shared types into contracts; clear storage allowlist except migration |
| `db4afb2` | B02: restore code_patch files from code_apply transaction backups |
| (docs) | this file — residual truth + SHAs |

## Verification run (focused)

```text
pytest -q \
  tests/test_arch01_import_boundaries.py \
  tests/test_b01_b02_promotion_boundaries.py \
  tests/test_independent_evidence_architecture.py \
  tests/test_capability_storage_v3.py \
  tests/test_governance_evidence_contract.py \
  tests/test_policy_rollout.py
```

Full suite not claimed green unless run separately.
