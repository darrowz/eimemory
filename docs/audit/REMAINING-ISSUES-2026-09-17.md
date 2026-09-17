# Remaining issues after 1.13.15 / 2c90106 — 2026-09-17

| Field | Value |
| --- | --- |
| Date | 2026-09-17 (Asia/Shanghai / CST+8) |
| Base HEAD | `6d8ecd8` (RI-12 committed; second-pass WIP uncommitted) |
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
| RI-10 | Low / deferred | **fixed** (this pass) | Codex/Hermes adapter receipt lifecycle | `allow_loopback=True` on adapter RPC `safe_urlopen` |
| RI-11 | Low / deferred | **deferred** | unused `_latest_recall_audit_for_session` | intentional fail-closed |
| RI-12 | Medium | **fixed** (`6d8ecd8`) | Broader pytest triage + product/test fixes | committed |
| RI-13+ | see Second pass | **fixed / deferred** | Perf + residual remediations | See section below |

## RI-12 resolution (this pass, uncommitted)

### Failure inventory (original lastfailed / RI-12 examples)

Grouped from timed individual runs of the deferred lastfailed set:

| Group | Nodes | Disposition |
| --- | --- | --- |
| **(A) product bugs** | L5 seed lock; embed oversized cache; intake fingerprint `last_scan`; GOV-07 wiping legacy capability overlay; nightly self-pollution (recall after evolution); empty digest / unconfigured recall_quality_gate failing aggregate; late prompt-injection INT-09 window; codex plugin version/mcpServers drift | **fixed** |
| **(B) obsolete tests** | mixed DNS vs INT-01 filter-public; failed-verifier verdict `fail` not `not_run`; digest description fixtures vs current `plugin.yaml`; identity paging mock missing `scope=`; intake local reads need allowed_roots; doctor without systemd; version pin 1.13.14 | **aligned** |
| **(C) env** | sealed catalog requires installed entry-points (`pip install -e .`); doctor systemd unit FileNotFoundError | **mitigated** (editable install; `--no-systemd` in tests) |
| **(D) out-of-scope** | Codex/Hermes adapter receipt lifecycle | **fixed** (RI-10; adapter RPC loopback) |

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
| `test_codex_post_tool_and_stop_separate_processes_preserve_exact_receipts` | **fixed** — was `adapter_tool_receipts` None | D / RI-10 |
| `test_hermes_official_terminal_lifecycle_binds_verified_host_turn` | **fixed** — was `eimemory_verify_outcome` not ok | D / RI-10 |
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

## Second pass (perf + residual) — 2026-09-17 (Asia/Shanghai)

| Field | Value |
| --- | --- |
| Base | `6d8ecd8` / 1.13.15 |
| Scope | `/workspace/eimemory` only; no commit/push/deploy |
| linux_deployment | **152 passed / 1 skipped** (re-verified) |

### Issue table (this pass)

| ID | Severity | Area | Status | Notes |
| --- | --- | --- | --- | --- |
| RI-13 | **High** | recall / create_safety | **fixed** | `ExactScope` broke `_authoritative_identity_exists` (`ScopeRef.from_dict`); coerce via `to_scope_ref` / `_coerce_scope_ref`. Payload-verify aliases/titles; unique-only `exists`; reject quality/stale. |
| RI-14 | Medium | persona | **fixed** | Malformed numeric persona state now coerces instead of hard-failing when no snapshot. |
| RI-15 | Medium | perf / recall | **fixed** | Honor explicit `task_context.kinds`; skip always-on `list_records(kinds=["rule"])` fan-out when kinds exclude `rule`. Smoke kinds exclude rules so nightly quality gate is not polluted. |
| RI-16 | Medium | proactive | **fixed** | Persist-failure path keeps `mandatory_fallback` for hard policy. Unavailable stays retryable (diagnostics on response, empty `decision_id`). |
| RI-17 | Low | paper / intake | **fixed** | Preserve `hash_pdf_contents` through normalize so content-stable PDF identity works when opted in. |
| RI-18 | Low | tests | **aligned** | Lock wraps for STO-18 identity SQL; INT-15 collision skip; loadout persona split; `__ambiguous__` sentinel; EXT-03 rejected audit persist; prompt_safety `safe_urlopen`; postgres empty-partition / pool / `_identity_lookup` contracts. |
| RI-19 | Medium | perf | **fixed** | New `tests/test_recall_perf_bounds.py`: no rule fan-out under constrained kinds; covering alias index without TEMP B-TREE; oversized embed cache exclusion. |
| RI-09 | Low / ops | deploy | **deferred** | Prod still 1.13.11 |
| RI-10 | Low | adapters | **fixed** | Adapter RPC loopback blocked by `safe_urlopen`; receipts/verify now green |
| RI-20 | Low | perf | **fixed** (partial; see third pass) | Archival CTE+index, migration pending cache, vector sync page/batch, intake due scheduling, health probe TTL, raw scan bound |
| RI-21 | Low | env | **deferred** | Paper PDF pipeline needs `eimemory[pdf]` / `pypdf` in review venv (installed locally for verification; not a product code change) |

### Performance — fixed vs deferred

**Fixed**
- Skip active-rule fan-out when caller constrains `kinds` away from `rule` (hot path nightly smoke + identity-style recalls).
- Smoke dataset sets `kinds` so rules are not scored as noise (`p_at_3` / `noise_rate` gate).
- Exact alias identity plan still uses covering indexes (regression-tested).
- Oversized embedding inputs still bypass LRU (regression-tested; prior RI-12 cache policy preserved).
- Postgres empty-partition short-circuit (`sqlite_authority`) remains the intentional remote-call skip.

**Deferred (after third pass)**
- Broader suite residual clusters beyond RI-20 hotspots (runtime ingest idempotency / export 10k / recall_fusion / postgres vector if still red).
- Adapter-local SQLite busy UX polish (not required for RI-10 lifecycle green).

### Still open after this pass

- RI-09 ops deploy (prod 1.13.11 → 1.13.15+)
- RI-10 Codex/Hermes adapter lifecycle (`test_adapter_receipt_review_gaps`) — **closed in third pass**
- RI-20 broader residual suite — **hotspots closed in third pass**; remaining suite clusters still deferred
- RI-21 optional PDF extra in CI images

### Test evidence (CST+8)

| Suite | Result |
| --- | --- |
| Changed clusters (fusion/perf/persona/paper/platform/postgres/proactive/prompt_safety/…) | **99 passed** |
| `tests/test_deployment_tools.py -m linux_deployment` | **152 passed, 1 skipped** |
| New `tests/test_recall_perf_bounds.py` | **3 passed** |

### Files changed (uncommitted; ready to commit)

**Product**
- `eimemory/retrieval/engine.py` — ExactScope identity lookup; payload-verified unique `exists`; honor `kinds`; skip rule fan-out
- `eimemory/storage/runtime_store.py` — `_coerce_scope_ref`
- `eimemory/persona/schema.py` — numeric coerce helpers
- `eimemory/retrieval/proactive.py` — mandatory_fallback on persist failure; retryable unavailable diagnostics
- `eimemory/scheduler/jobs.py` — smoke `kinds` exclude rules
- `eimemory/intake/papers/normalize.py` — `hash_pdf_contents` pass-through

**Tests**
- `tests/test_recall_fusion.py`, `tests/test_recall_perf_bounds.py` (new)
- `tests/test_persona_state.py` (via schema), `tests/test_l1_extract.py`, `tests/test_intake_packs.py`, `tests/test_memory_core_v1_repair.py`
- `tests/test_paper_intake.py`, `tests/test_platform.py`, `tests/test_prompt_safety_executor.py`
- `tests/test_postgres_runtime_config.py`, `tests/test_postgres_vector_source.py`, `tests/test_postgres_vector_sync.py`
- `tests/test_proactive_capture_contract.py`

### Ready to commit

Working tree under `/workspace/eimemory` only. **Do not commit/push from this agent** (per task).

## Third pass (RI-10 + RI-20) — 2026-09-17 15:21 CST+8

| Field | Value |
| --- | --- |
| Base | `a5ad613` / 1.13.15 |
| Scope | `/workspace/eimemory` only; no commit/push/deploy |
| linux_deployment | **152 passed / 1 skipped** (re-verified) |

### RI-10 — adapter receipt lifecycle (**fixed**)

**Root cause:** `AgentRuntimeRPCClient` switched to `safe_urlopen` without `allow_loopback=True`, so Codex/Hermes subprocess hooks could not reach the in-process `127.0.0.1` RPC server. Attestation/sync/terminal calls fail-open as `adapter_unavailable`, leaving `adapter_tool_receipts` empty and Hermes `eimemory_verify_outcome` not ok.

**Fix:** `eimemory/adapters/runtime/http_client.py` — pass `allow_loopback=True` for adapter RPC (same pattern as deploy health probe).

**Evidence:**
- `test_codex_post_tool_and_stop_separate_processes_preserve_exact_receipts` **PASSED**
- `test_hermes_official_terminal_lifecycle_binds_verified_host_turn` **PASSED**

### RI-20 — deeper perf (**fixed** for targeted hotspots)

| Area | Change |
| --- | --- |
| SQLite archival | Covering index `idx_records_kind_updated_record`; CTE + LEFT JOIN anti-join replaces `NOT IN` hot-window subquery |
| Migration N+1 | 2s TTL cache for `pending_storage_migrations`; full-batch path skips duplicate pending re-scan |
| Vector sync | Worker `batch_size` 4→32, `max_pages` 25→8; incremental defaults 16/16 |
| Intake scheduling | `source_is_due` / `select_due_sources` skip not-due sources by frequency + `last_scanned_at` |
| RPC health probe | 5s in-process TTL cache for pending-migrations + candidate-source fragments |
| Raw backstop scan | Fan-out 32×→12×, cap 5000→1500 |

**Evidence:** `tests/test_recall_perf_bounds.py` (6 passed), `tests/test_intake_due_scheduling.py` (2 passed), vector-sync worker stale-sync assertion updated; combined RI-10/RI-20 node set **13 passed**.

### Still open / impossible here

- RI-09 production upgrade (ops-only; out of scope)
- Broader pytest residual clusters (recall_fusion / postgres vector / export / ingest) beyond these hotspots — not claimed closed
- `test_optional_deployment_wires_configs_and_dependency` still expects `EIMEMORY_INSTALL_POSTGRES_EXTRA:-0` in install script (pre-existing; not part of RI-10/20)

### Files changed (uncommitted; ready to commit)

- `eimemory/adapters/runtime/http_client.py`
- `eimemory/adapters/eibrain/rpc_server.py`
- `eimemory/storage/sqlite_store.py`
- `eimemory/retrieval/vector_sync_worker.py`
- `eimemory/retrieval/incremental_sync.py`
- `eimemory/intake/registry.py`
- `eimemory/intake/loop.py`
- `eimemory/raw/retrieval.py`
- `tests/test_recall_perf_bounds.py`
- `tests/test_intake_due_scheduling.py` (new)
- `tests/test_vector_sync_worker.py`
- `docs/audit/REMAINING-ISSUES-2026-09-17.md`

### Ready to commit

Working tree under `/workspace/eimemory` only. **Do not commit/push from this agent** (per task).


## Post-deploy open items (2026-09-18)

See `docs/audit/POST-DEPLOY-OPEN-2026-09-18.md` for P1–P4 status after prod 1.13.15 / `0e4171e`.
