# Remediation status — 2026-09-22 audits

| Field | Value |
| --- | --- |
| Base (start) | `d25c9b5` (GOV-01 promotion_manager missing L0/L1 scores) |
| Final HEAD | `0889f19ff88d3fc1048695633d066ee328331a84` |
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
| **ARCH-01** Data↔Control cycle | **partial** | `6c533d5` | `tests/test_arch01_import_boundaries.py` |
| **B01** active-surface exclusive lease | **closed** | `9bdac13` | `tests/test_b01_b02_promotion_boundaries.py` |
| **B02** artifact rollback undo | **partial** | `9bdac13` | `tests/test_b01_b02_promotion_boundaries.py` |
| **PERF P0** FTS top-N safety net | **closed** | (this commit) | `tests/test_recall_perf_bounds.py` |
| **GOV-03** auto-commit/deploy defaults | **kept closed** | n/a | unchanged |

## Residuals

### ARCH-01 (partial)
- `storage/capability_store.py` still imports `eimemory.capabilities.*` (persistence owns capability entities; documented allowlist).
- `storage/sqlite_store.py` still imports `eimemory.governance.policy_rollout` ledger helpers (allowlist).
- `storage/independent_evidence.py` → `retrieval.evidence_query` (allowlist).
- `storage/replay_buffer.py` → `governance.evidence_contract` (allowlist).
- Full type sink of `AdapterCapabilityAdvertisement` into contracts deferred (large dataclass + validators).

### LOCK-01 (partial wrap)
- Public `execute`/`commit`/`rollback` wrappers assert lock ownership.
- RuntimeStore hot BEGIN/commit/rollback paths migrated to wrappers.
- Residual bare `conn.execute` remains for PRAGMA/diagnostics and some rebuild SQL; mutate paths used by RuntimeStore hold `_lock`.

### B02 (partial)
- Supported undo: intent_pattern via `rollback_intent_pattern`; rule/playbook/memory records via status rewrite.
- **Unsupported**: code_patch file paths / commit SHAs — still returns `artifact_rollback_required` and never ok.
- Full effect-owner reconciliation / digest verify not implemented.

### PERF (P0 only)
- P1 schema PRAGMA dedup / pollution-gate memoization **not** landed (out of this pass; P0 was prerequisite).
- No ranking semantics changes.

### Security pack §4 e2e notes
- Non-code promotion multi-write crash window, orphan/watch reconciliation, outcome idempotency, health identity binding, scheduler lease reread — **open** (documented in SECURITY-BOUNDARIES-AUDIT §4; not in this remediation scope beyond B01/B02).

## Verification run (focused)

```text
pytest -q \
  tests/test_learning_eval.py \
  tests/test_gov02_ledger_fail_closed.py \
  tests/test_sch01_nightly_ok_semantics.py \
  tests/test_business_closure_bc.py \
  tests/test_audit_security_boundaries.py \
  tests/test_scoring.py \
  tests/test_research_evidence_gate.py \
  tests/test_ret01_batch_hydrate.py \
  tests/test_lock01_sqlite_guard.py \
  tests/test_phase_c_arch_sch_ret.py \
  tests/test_arch01_import_boundaries.py \
  tests/test_b01_b02_promotion_boundaries.py \
  tests/test_recall_perf_bounds.py
```

Full suite not claimed green unless run separately.
