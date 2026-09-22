# Round-3 full-project audit remediation (2026-09-22)

Audit baseline HTML HEAD `6b5026a` (1.13.18). Remediation verified and applied on working HEAD starting at `2e57f59` (1.13.19), released as **1.13.20**.

Convention: every finding is **closed** or **closed-by-design** with a fix SHA. No open rows.

| Finding | Verified on HEAD `2e57f59` | Status | Fix SHA | Tests |
|---|---|---|---|---|
| S1 Nightly TimeoutStartSec | Confirmed: nightly + 9 oneshots lacked TimeoutStartSec (default 90s) | closed | `d34e67d` | unit file presence of TimeoutStartSec (manual/rg) |
| Recall budget injection (P0) | Confirmed: only OpenClaw injected 800ms; CLI/RPC/SDK left deadline unset | closed | `21de94b` (+ follow-up `8111818`) | `tests/test_recall_default_budget.py` |
| Assistance budget ≤3s | Confirmed: caller_assistance min(9s)/engine start+10s | closed | `920b841` | `tests/test_caller_assistance.py` |
| SEC-1 v1 deploy argv trusts patch | Confirmed: `_deployment_commands` preferred patch fields | closed | `dda8434` | `tests/test_promotion_manager.py::test_v1_local_commands_ignore_untrusted_patch_fields` |
| B1 reward/RL CLI-only | Confirmed: `post_experience_hook` only from CLI | closed | `18c2448` | `tests/test_b1_runtime_outcome_closed_loop.py` |
| ARCH-01 test blinds | Confirmed: DATA_DIRS/FORBIDDEN incomplete; top-level upward imports | closed | `0e69e02` | `tests/test_arch01_import_boundaries.py` |
| D1 jsonl.py duplicate | Confirmed: 1877-line dual JsonlLog | closed | `2ddab83` | `tests/test_jsonl_single_definition.py`, `tests/test_storage.py` |
| B2 memory_rule watch | Confirmed: watch loaded intent_patterns only; no `ok` | closed | `c66ae9a` | `tests/test_b2_memory_rule_watch.py` |
| B3 orphan check dead code | Confirmed: production zero call sites | closed | `0788321` | doctor check + nightly step wiring |
| S2 `_effects_unknown_after_timeout` dead | Confirmed: helper unused on timeout path | closed | `7da46d6` | `tests/test_s2_learning_timeout_lease_reread.py` |
| fcntl Windows crash | Confirmed: bare `import fcntl` | closed | `5bf0ac7` | fail-closed ValueError path |
| canonical_json forks | Confirmed: audit ensure_ascii=True; ledger missing separators | closed | `a02d524` | dump parameter alignment |
| SEC-2 fat unauth health | Confirmed: `/health` before auth returned fingerprints | closed | `79997e8` | `tests/test_eibrain_rpc_contract.py`, `tests/test_platform.py` |
| schema_migrations 18×/recall | Confirmed: `_schema_migration_applied` always queried | closed | `157207d` (+ `8111818`) | `tests/test_recall_perf_bounds.py` |
| memoization thread safety | Confirmed: OrderedDict LRU unlocked | closed | `559c8d4` | `tests/test_recall_perf_bounds.py` |

## Notes

- **3s p95 claim:** not measured in this remediation. Code now injects/honors a ≤3s deadline on the default MemoryAPI path and caps assistance at 3s; no e2e p95 numbers are asserted here.
- Pre-existing L2 promotion tests that fail with `health_identity_unbound` on HEAD `2e57f59` were observed before SEC-1 edits and were not reopened as Round-3 findings.
- Delayed imports for ARCH-01 remain counted as ARCH-01-Shadow with soft ceiling 80 (honest, not hidden).

## Release

- Version: **1.13.20**
- Parent baseline: `2e57f59` (1.13.19)
