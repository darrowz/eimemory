# BC-01..BC-11 fix changelog — 2026-09-17

| Field | Value |
| --- | --- |
| Date | 2026-09-17 (Asia/Shanghai / CST+8) |
| Base HEAD | `618dd8d` (1.13.14) |
| Release | **1.13.15** |
| Audit | `docs/audit/BUSINESS-CLOSURE-OPEN-LOOPS-2026-09-17.md` |

## Summary

Closed all business-closure findings BC-01..BC-11 without regressing CK-* contracts from 618dd8d.

| ID | Severity | Fix |
| --- | --- | --- |
| BC-01 | P0 | Nightly steps wrapped in `_nightly_step`; top-level `ok` aggregates step_reports + nested allowlist; supervisor binds aggregated ok; CLI nightly non-zero exit |
| BC-02 | P0 | Shared `create_safety_gate`; OpenClaw / runtime / eibrain create paths consume fusion create_safety (`exists` skip, `probable` require force) |
| BC-03 | P1 | Nightly runs `repair_memory_quality(apply=True)` into step_reports |
| BC-04 | P1 | CLI ingest `rejected` → exit 2 + `ok: false` |
| BC-05 | P1 | Rule evolution / operational projection / source discovery unavailable → `ok: False` |
| BC-06 | P1 | `_authoritative_identity_exists` returns `None` on error → `create_safety=unavailable` |
| BC-07 | P2 | `promote_candidate` uses `mutate_records_atomically` (append-first + CAS fallback) |
| BC-08 | P2 | Persona save marks `persona_audit_gap` and fails if audit append cannot complete after retry |
| BC-09 | P2 | Missing required arms → `retrieval_status=degraded`; OpenClaw limits full-text injection |
| BC-10 | P2 | Lock asserts on more sqlite hot paths; busy → `SqliteBusyError` |
| BC-11 | P2 | `evaluate_publish_gate`: receipt.ok + health identity + rollback_commands (non-bootstrap) |

## CK contracts preserved

CK-01..CK-10 from the audit remain intact (rejected durable, identity-before-write, deterministic promote id, engine create_safety, host unavailable recall, keyword eligibility, dense vector score, diagnostic operational lane, deploy loopback scoped, persona atomic file write).

## Tests

- `tests/test_business_closure_bc.py` (BC-01..BC-11)
- Regression: `test_prod_regression_1_13_14.py`, `test_a0_a2_remediation.py`, `test_version.py`

## Files (primary)

- `eimemory/scheduler/jobs.py`
- `eimemory/cli/main.py`
- `eimemory/adapters/create_safety_gate.py` (new)
- `eimemory/adapters/openclaw/hooks.py`
- `eimemory/adapters/runtime/service.py`
- `eimemory/adapters/eibrain/rpc.py`
- `eimemory/retrieval/engine.py`
- `eimemory/intake/review.py`
- `eimemory/persona/store.py`
- `eimemory/storage/sqlite_store.py`
- `eimemory/governance/deployment_receipt.py`
- version manifests + CHANGELOG
