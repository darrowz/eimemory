# Remaining issues after 1.13.15 / 2c90106 — 2026-09-17

| Field | Value |
| --- | --- |
| Date | 2026-09-17 (Asia/Shanghai / CST+8) |
| Base HEAD | `2c90106` (1.13.15 + release-contract fixes) |
| Scope | `/workspace/eimemory` only; no commit/push/deploy |

## Context

- BC-01..BC-11 closed in `d1228e6` (1.13.15)
- Release contracts fixed in `2c90106` (EXIT-trap `return 0`, systemd status 4, `pending_archival`)
- `linux_deployment` (`tests/test_deployment_tools.py -m linux_deployment`): **152 passed / 1 skipped**
- Production remains on **1.13.11** — ops-only upgrade; not done here

## Issues found this pass

| ID | Severity | Status | Evidence | Notes |
| --- | --- | --- | --- | --- |
| RI-01 | **High** | **fixed now** | BC-11 early-return left `append_learning_record_once` unreachable → `UnboundLocalError: record` | `eimemory/governance/deployment_receipt.py` |
| RI-02 | **Medium** | **fixed now** | `_fetch_health` + `max_redirects=0` raised `UnsafeURL("too many redirects")` instead of `health_redirect_not_allowed` | Map redirect UnsafeURL → dedicated error |
| RI-03 | **Medium** | **fixed now** | Restart-order harness early-returned on hosts without `systemctl` | Stub `systemctl` on PATH in tests |
| RI-04 | **Medium** | **fixed now** | Rollback-order test expected health **after** writers | Assertion aligned with core-health-before-background-writers |
| RI-05 | **Medium** | **fixed now** | Migration resume test assumed greenfield pending keyset work | Force deferred payload archival + >hot_window rows |
| RI-06 | **Medium** | **fixed now** | Quality-loop test used non-production outcomes / unbound events | Bind `audit_record_id` + verifier; doctor `--no-systemd` |
| RI-07 | **Medium** | **fixed now** | Installer recovery / optional-OpenClaw harnesses missing `_start_managed_runtime_timers`, brittle systemd mock, unbound `CURRENT_LINK`/`RELEASE_DIR` | Test harness fixes; installer behavior unchanged |
| RI-08 | **Medium** | **fixed now** | `test_nightly_jobs_reports_external_errors_without_failing` expected top-level `ok=True` | Aligned with BC-01 nested aggregation |
| RI-09 | Low / ops | **ops-only** | Prod still on 1.13.11 | Deploy/upgrade intentionally out of scope |
| RI-10 | Low / deferred | **deferred** | Blind spots BS-01..BS-03 (Hermes create_safety sweep, SQLite busy UX, outcome→rule quality) | Needs broader adapter/load audit |
| RI-11 | Low / deferred | **deferred** | `_latest_recall_audit_for_session` unused on event-bind path | Fail-closed explicit `audit_record_id` is intentional |
| RI-12 | Medium / deferred | **deferred** | Broader suite still has failures (e.g. production_recall quality_gate, autonomous_learning goal_count, identity_ops paging, intake excerpt, L5 baseline, safe_transport DNS mix) | Not clearly unfinished BC/deploy remediations; leave for follow-up |

## Files changed (working tree)

- `eimemory/governance/deployment_receipt.py` — RI-01, RI-02
- `tests/test_storage_deploy.py` — RI-04
- `tests/test_storage_writer_restart_order.py` — RI-03
- `tests/test_storage_maintenance.py` — RI-05
- `tests/test_quality_loop_130.py` — RI-06
- `tests/test_installer_recovery_boundaries.py` — RI-07
- `tests/test_deployment_optional_openclaw.py` — RI-07
- `tests/test_active_intake_platform.py` — RI-08
- `docs/audit/REMAINING-ISSUES-2026-09-17.md` — this note

## Test results after fixes

| Suite | Result |
| --- | --- |
| `tests/test_deployment_tools.py -m linux_deployment` | 152 passed, 1 skipped |
| Targeted deploy/receipt/BC/recovery bundle | **117 passed** |
| `tests/test_payload_archival.py` + `test_business_closure_bc.py` + prod regression + version | green earlier in pass |
| Broader `pytest tests/ --maxfail=30` | 30 failed / 1639 passed / 34 skipped (pre-existing + deferred RI-12); stopped early |

## Version

No version bump (residual bugs / test contracts after 1.13.15; no feature release).

## Ready to commit

Working tree has uncommitted fixes under `/workspace/eimemory` only. **Do not commit/push from this agent** (per task).
