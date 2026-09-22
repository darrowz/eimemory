# Governance-module deep audit remediation (2026-09-22 / closed in 1.13.22)

Source: HTML deep audit plaintext `/workspace/audit-html-extra-20260923.txt`.
Baseline absorb: 2e57f59 hardening pack. Round-3 closed in 1.13.20. Residuals closed in **1.13.22**.

## Priority table

| ID | Claim | Status | Evidence |
|---|---|---|---|
| S8-1 / B2 | memory_rule watch empty-spin | **closed** (1.13.20) | `c66ae9a`; `tests/test_b2_memory_rule_watch.py` |
| S7-1 / B1 | reward/RL production path | **closed** (1.13.20) | `18c2448`; Runtime.record_outcome_trace sinks `post_experience_hook` |
| CE-1 | v1 verify inherits full parent env | **closed** | `_v1_verification_environment` allowlist; `tests/test_v1_verify_environment.py` |
| S6-1 | fcntl/geteuid/O_DIRECTORY Windows | **closed** | os.name dual-branch lease + geteuid/O_DIRECTORY guards; `tests/test_platform_guards_governance.py` |
| S7-3 / B3 | orphan reconciler unwired | **closed** (1.13.20) | nightly + doctor wiring; `0788321` (dead interface wired) |
| CE-2 / SEC-1 | v1 deploy argv trusts patch | **closed** (1.13.20) | `_deployment_commands` / rollback / canary / health ignore patch; verify allowlisted argv |
| A4 | l5_readiness six fail-open counts | **closed** | `_evidence_counts_with_health`; `tests/test_l5_evidence_count_health.py` |
| S1-1 | capability_ledger bare-swallow | **closed** | structured `attribution` + logger |
| S7-2 | observation path can explode outcome | **closed** | structured `watch_failed` on `record_outcome`; `tests/test_s7_2_outcome_observations_degrade.py` |
| A1 | GovernanceRuntime Protocol | **closed** | Protocol + hottest modules (`promotion_manager`, `promotion_watch`, `capability_probe_executor`) have **zero** `runtime: Any` / `_runtime: Any`; gate `tests/test_governance_runtime_any_allowlist.py` (empty allowlist). Remaining Any lives outside this gate (348 in other governance modules) — not claimed as fully eliminated. |
| A2 | bare conn / `_pattern_row_for_scope` | **closed** | `RuntimeStore.locked()` / `run_locked` / `execute_readonly`; inventory allowlist is **`storage/` only**; `tests/test_storage_conn_facade_inventory.py` green |
| Structure | extract `_git_ops` / `_gates` / `_code_apply_txn` | **closed** | `promotion_gates.py` (~109), `promotion_git_ops.py` (~282), `promotion_code_apply.py` (~704); `promotion_manager.py` ~**3764** lines (was ~4601) |
| S6 lease cover | lease excludes rollback/watch tail | **closed** | `promote_candidate` holds lease through apply/watch/rollback; `recover_incomplete_code_apply` holds the same active-surface lease (`a1e91f5`); `tests/test_s6_recover_holds_lease.py` |
| CE-3 | allowed_files wildcards as service user | **closed** | exact allowlist; deny `*?[` globs; prefer incident allowlist; `tests/test_ce3_allowed_files_no_globs.py` |
| Dead symbols | 6 dead defs + dead interface | **closed** | removed 6 zero-call defs (`baff50f`); S7-3 orphan interface already wired in 1.13.20 |

## Hardening pack absorb (related)

See `docs/audit/ABSORB-HARDENING-2e57f59-2026-09-23.md` and `docs/audit/HARDENING-AUDIT-2e57f59-2026-09-22.md`.

## Not claimed

- Hongxin production deploy
- Fake ≤3s p95 recall SLA
- Full-tree elimination of every `runtime: Any` outside the A1 hottest-module gate
- Optional RO SQLite recall flag remains default OFF (`EIMEMORY_SQLITE_READONLY_RECALL`)
