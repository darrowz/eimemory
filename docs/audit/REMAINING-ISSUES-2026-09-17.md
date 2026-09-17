# Remaining issues after 1.13.15 / 2c90106 — 2026-09-17

| Field | Value |
| --- | --- |
| Date | 2026-09-17 (Asia/Shanghai / CST+8) |
| Base HEAD | `33b1182` (RI-01..08 already committed) |
| Scope | `/workspace/eimemory` only; no commit/push/deploy |

## Context

- BC-01..BC-11 closed in `d1228e6` (1.13.15)
- Release contracts fixed in `2c90106` (EXIT-trap `return 0`, systemd status 4, `pending_archival`)
- RI-01..08 closed in `33b1182`
- `linux_deployment` (`tests/test_deployment_tools.py -m linux_deployment`): **152 passed / 1 skipped** (verified after RI-12 work)
- Production remains on **1.13.11** — ops-only upgrade; not done here
- Env note: `pip install -e /workspace/eimemory` into review venv so catalog entry-points resolve (`hongtu` bootstrap)

## Issues

| ID | Severity | Status | Evidence | Notes |
| --- | --- | --- | --- | --- |
| RI-01 | **High** | **fixed** (33b1182) | BC-11 early-return left `append_learning_record_once` unreachable | `deployment_receipt.py` |
| RI-02 | **Medium** | **fixed** (33b1182) | health redirect mapped to dedicated error | `deployment_receipt.py` |
| RI-03 | **Medium** | **fixed** (33b1182) | restart-order harness without systemctl | tests |
| RI-04 | **Medium** | **fixed** (33b1182) | rollback-order assertion | tests |
| RI-05 | **Medium** | **fixed** (33b1182) | migration resume harness | tests |
| RI-06 | **Medium** | **fixed** (33b1182) | quality-loop bindings + doctor `--no-systemd` | tests |
| RI-07 | **Medium** | **fixed** (33b1182) | installer recovery / OpenClaw harness | tests |
| RI-08 | **Medium** | **fixed** (33b1182) | nightly nested ok aggregation | tests |
| RI-09 | Low / ops | **ops-only** | Prod still on 1.13.11 | out of scope |
| RI-10 | Low / deferred | **deferred** | Hermes create_safety / SQLite busy UX / outcome→rule | still open; see adapter residuals below |
| RI-11 | Low / deferred | **deferred** | unused `_latest_recall_audit_for_session` | intentional fail-closed |
| RI-12 | Medium | **mostly fixed (WIP tree)** | Broader pytest triage + product/test fixes | See resolution below |

## RI-12 resolution (this pass, uncommitted)

### Failure inventory (original lastfailed / RI-12 examples)

Grouped from timed individual runs of the deferred lastfailed set:

| Group | Nodes | Disposition |
| --- | --- | --- |
| **(A) product bugs** | L5 seed lock; embed oversized cache; intake fingerprint `last_scan`; GOV-07 wiping legacy capability overlay; nightly self-pollution (recall after evolution); empty digest / unconfigured recall_quality_gate failing aggregate; late prompt-injection INT-09 window; codex plugin version/mcpServers drift | **fixed** |
| **(B) obsolete tests** | mixed DNS vs INT-01 filter-public; failed-verifier verdict `fail` not `not_run`; digest description fixtures vs current `plugin.yaml`; identity paging mock missing `scope=`; intake local reads need allowed_roots; doctor without systemd; version pin 1.13.14 | **aligned** |
| **(C) env** | sealed catalog requires installed entry-points (`pip install -e .`); doctor systemd unit FileNotFoundError | **mitigated** (editable install; `--no-systemd` in tests) |
| **(D) out-of-scope** | Codex/Hermes adapter receipt lifecycle | **still open** (ties to RI-10) |

### What was fixed

**Product**

- `benchmarks/l5_v3_baseline.py` — hold `runtime.store._lock` during STO-18 seeded upserts
- `eimemory/embeddings/local.py` — measure pre-truncation length so oversized docs never enter LRU
- `eimemory/intake/loop.py` — ignore volatile `last_scan` in metadata fingerprints; INT-09 leading+trailing 2048 windows for injection screening
- `eimemory/governance/autonomous_learning.py` — re-apply legacy capability overlay after GOV-07 self-model persist rebuild (restores `goal_count`)
- `eimemory/scheduler/jobs.py` — run `production_recall` before `rule_evolution`; vacuous ok for empty research digest; dashboard no-active-cases / legacy env; unconfigured `recall_quality_gate` no longer fails aggregate
- `integrations/codex/eimemory/.codex-plugin/plugin.json` — version `1.13.15` + `mcpServers`

**Tests**

- INT-01 mixed-DNS: assert private never dialed; public may connect
- Verifier mismatch → `fail` / `outcome_verifier_probe_mismatch`
- Digest fixtures synced to current Hermes `plugin.yaml` description
- Intake tests use allowed local roots helper
- Identity paging mock accepts `scope=`
- Doctor CLI tests use `--no-systemd`
- Version contract pin → 1.13.15
- Reviewed-candidate / identity nightly docs placed under runtime root

### Still open (evidence)

| Item | Evidence | Group |
| --- | --- | --- |
| `test_codex_post_tool_and_stop_separate_processes_preserve_exact_receipts` | `adapter_tool_receipts` row is `None` after PostToolUse/Stop subprocesses | D / RI-10 |
| `test_hermes_official_terminal_lifecycle_binds_verified_host_turn` | subprocess `good['ok'] is True` fails (`eimemory_verify_outcome`) | D / RI-10 |
| Broader suite residual (beyond RI-12 examples) | `pytest tests/ --maxfail=60` (adapters ignored): **60 failed / 3705 passed / 41 skipped** (~8m); clusters include `test_recall_fusion` (many), postgres vector, paper PDF, platform/export, runtime ingest idempotency | deferred follow-up |

### Files changed (WIP tree for RI-12; do not commit from this agent)

- `benchmarks/l5_v3_baseline.py`
- `eimemory/embeddings/local.py`
- `eimemory/governance/autonomous_learning.py`
- `eimemory/intake/loop.py`
- `eimemory/scheduler/jobs.py`
- `integrations/codex/eimemory/.codex-plugin/plugin.json`
- `tests/safety/test_intake_safe_transport.py`
- `tests/test_active_intake_platform.py`
- `tests/test_capability_replay_packs.py`
- `tests/test_cli_autonomous_learning.py`
- `tests/test_code_implementation_provider.py`
- `tests/test_identity_ops.py`
- `tests/test_intake_loop_core.py`
- `tests/test_partial_close.py`
- `tests/test_platform.py`
- `docs/audit/REMAINING-ISSUES-2026-09-17.md`

## Test results after RI-12 WIP

| Suite | Result |
| --- | --- |
| Original RI-12 lastfailed set (21 nodes) | **19 passed / 2 failed** (Codex+Hermes adapters only) |
| `tests/test_deployment_tools.py -m linux_deployment` | **152 passed, 1 skipped** |
| Broader `pytest tests/ --maxfail=60` (ignore adapter_receipt_review_gaps) | 60 failed / 3705 passed / 41 skipped (stopped early; residual clusters above) |

## Version

No version bump (remediation / contract alignment after 1.13.15).

## Ready to commit

Working tree has uncommitted RI-12 fixes under `/workspace/eimemory` only. **Do not commit/push from this agent** (per task).
