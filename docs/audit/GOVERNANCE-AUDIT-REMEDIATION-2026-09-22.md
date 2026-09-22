# Governance-module deep audit remediation (2026-09-22 / released 1.13.21)

Source: HTML deep audit plaintext `/workspace/audit-html-extra-20260923.txt`.
Re-verified on HEAD after 2e57f59 hardening absorb + 1.13.20 Round-3 fixes.

## Priority table

| ID | Claim | Status on 1.13.21 | Evidence |
|---|---|---|---|
| S8-1 / B2 | memory_rule watch empty-spin | **closed** (1.13.20) | `c66ae9a`; `tests/test_b2_memory_rule_watch.py` |
| S7-1 / B1 | reward/RL production path | **closed** (1.13.20) | `18c2448`; Runtime.record_outcome_trace sinks `post_experience_hook` |
| CE-1 | v1 verify inherits full parent env | **closed** | `_v1_verification_environment` allowlist; `tests/test_v1_verify_environment.py` |
| S6-1 | fcntl/geteuid/O_DIRECTORY Windows | **closed** | os.name dual-branch lease + geteuid/O_DIRECTORY guards; `tests/test_platform_guards_governance.py` |
| S7-3 / B3 | orphan reconciler unwired | **closed** (1.13.20) | nightly + doctor wiring; `0788321` |
| CE-2 / SEC-1 | v1 deploy argv trusts patch | **closed** (1.13.20) | `_deployment_commands` / rollback / canary / health ignore patch; verify still allowlisted argv via `code_patch_verification_command_error` |
| A4 | l5_readiness six fail-open counts | **closed** | `_evidence_counts_with_health`; `evidence_count_health` on report; `tests/test_l5_evidence_count_health.py` |
| S1-1 | capability_ledger bare-swallow | **closed** | structured `attribution` + logger; digest test aligned to canonical separators |
| S7-2 | observation path can explode outcome | **closed** | structured `watch_failed` on `record_outcome`; `tests/test_s7_2_outcome_observations_degrade.py` |
| A1 | GovernanceRuntime Protocol | **partial** | Protocol added; hottest entrypoints annotated (`promote_candidate`, watch init/orphans/observations). Remaining ~422 Any sites deferred |
| A2 | bare conn / `_pattern_row_for_scope` | **partial** | RuntimeStore facade + promotion_watch / sqlite_source / memory_projection_authority migrated; inventory allowlist shrinks residuals (evaluation/, postgres_sync, incremental_sync) |
| Structure | extract `_git_ops` / `_gates` / `_code_apply_txn` | **partial** | `promotion_gates.py` extracted (~109 lines). `_code_apply_txn` / `_git_ops` still in promotion_manager (4555 lines after extract; was 4601) |
| S6 lease cover | lease excludes rollback/watch tail | **partial / closed-by-design on promote path** | `promote_candidate` holds lease through watch init + apply/rollback inside the same `try/finally`. Cross-process observation decisions intentionally outside promote lease |
| CE-3 / dead symbols / S3-1 | allowed_files / quarantine / correction completer | **open** (deferred) | Not in this wave; default machine apply remains gated |

## Hardening pack absorb (related)

See `docs/audit/ABSORB-HARDENING-2e57f59-2026-09-23.md` and `docs/audit/HARDENING-AUDIT-2e57f59-2026-09-22.md`.

## Not claimed

- Hongxin production deploy
- Fake ≤3s p95 recall SLA
- Full evaluation/ dual-backend SQL migration
- Complete replacement of all `runtime: Any` annotations
