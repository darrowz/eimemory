# Remaining-modules audit remediation (2026-09-23)

Audit artifacts: `REMAINING-MODULES-AUDIT-2026-09-23.html` / `.txt`  
Baseline HEAD: `0aefc4a` (1.13.27) → release **1.13.28**

Convention: every audit ID is **closed**, **already-fixed** (with prior SHA evidence), or **deferred** (with reason).

## P0

| ID | Status | Evidence |
|---|---|---|
| **REC-1** loadout untrusted wrap | **closed** | `eimemory/core/untrusted.py` + `recall/loadout.py` uses `wrap_untrusted_block`; `tests/test_remaining_modules_remediation_20260923.py::test_rec1_*` |
| **STO-1** record_id whitelist + export path anchor | **closed** | `eimemory/core/record_ids.py`; validate on `from_dict` / `append` / `rewrite` / `upsert`; `record_export._safe_export_path` resolve-under-root; malicious-id tests |
| **INT-1** adapter `record_terminal` → Runtime | **closed** | `Runtime.record_terminal_bundle` wraps store bundle + `record_outcome_observations`; adapter calls Runtime API; AST guard forbids `store.record_terminal_bundle` in adapters |
| **MIS-1** closure_review triple break | **closed** | Status constants in `intake/closure.py`; retry writes meta+content with `pending_model_review`; nightly scheduler steps `research_closure_retry` / `research_closure_review`; e2e-ish test retry→review |

## P1

| ID | Status | Evidence |
|---|---|---|
| **STO-3** FTS trigram + drop empty requery | **closed** | Prefer `tokenize='trigram'` (in-memory capability probe); migration `recall.fts_trigram.v1` rebuild path; removed empty-result recursive `search_with_diagnostics` re-query |
| **STO-2** + cross-round #1 search deadline | **closed** (this wave) / **already-fixed** (entries) | `RuntimeStore.search(deadline=...)` propagates via `recall_read_scope`; CLI/RPC/SDK budget injection already closed in 1.13.20 (`21de94b`, `tests/test_recall_default_budget.py`) |
| **MIS-3 + MIS-4** screening | **closed** | Public `eimemory/security_screening.py` (chunked 64KiB scan + Chinese patterns); packs + migration import screened + `origin=external`; private cross-package imports retargeted |
| **MIS-2** knowledge refresh lock | **closed** (partial) | Projection listing moved outside write mutation; compile/IO already outside; short upsert commit retained. Full cursor pagination of refresh inputs deferred. |
| **REC-2** subprocess env whitelist | **closed** | `llm/command_client._subprocess_env` — PATH + required; opt-in via `EIMEMORY_LLM_ENV_ALLOW` |

## Medium / low (this wave)

| ID | Status | Notes |
|---|---|---|
| **STO-4** order_by endswith | **closed** | Exact match or single table-prefix strip; no endswith |
| **REC-3** identity limit=1 short-circuit | **closed** | Only `identity_only` short-circuits; limit=1 keeps hybrid scoring |
| **REC-4** `_RECALL_DOC_CACHE` lock | **already-fixed** | `_RECALL_DOC_CACHE_LOCK` present on HEAD (`eimemory/recall/indexing.py`) |
| **INT-4** bare urlopen | **closed** | `ops/openclaw_loop.py` + `ops/timer_monitor.py` → `safe_urlopen`; grep guard test |
| **INT-5** timer-monitor lease | **deferred** | B01/B02 lease lives in code-evolution transaction manager; timer-monitor needs a dedicated ops lease — non-trivial, not safely shoehorned |
| **MIS-5** projector N+1 | **deferred** | Batch rewrite is multi-day; out of this wave |
| **MIS-6** argv 128KB prompt | **deferred** | Adapter stdin/file transport redesign |
| **MIS-7** capability_v3_backfill default | **deferred** | Product default / maintenance wiring |
| **MIS-8/9/10** ChatPaper / 鸿哥 / dead code | **deferred** | Low severity; hygiene follow-up |
| **INT-2** CLI @register 5/29 | **deferred** | Large migration |
| **INT-3** client/server timeout mismatch | **deferred** | Needs single config source design |
| **STO-5** 21 external `_lock`/conn | **deferred** | Facades (`run_locked`/`locked`/`read_consistent`) already exist; bulk caller migration is P2 |
| Cross-round #8 graph N+1 | **deferred** | Needs batch neighbor API |
| Cross-round #9 single-conn architecture | **deferred** | `readonly_recall.py` design exists (flag default OFF); enablement needs production evidence |
| Cross-round #10 dead-code pattern | **deferred** | Systemic process; MIS-1 instance closed |

## Cross-round survivors — re-verified on HEAD `0aefc4a` / 1.13.28

| # | Audit claim | Re-verify | Status |
|---|---|---|---|
| 1 | CLI/RPC/SDK no recall budget | `tests/test_recall_default_budget.py` green; ROUND3 `21de94b` | **already-fixed** (1.13.20) |
| 2 | caller-assisted 9–10s | `caller_assistance.py` caps `min(3.0, …)` | **already-fixed** (1.13.20 `920b841`) |
| 3 | nightly no TimeoutStartSec | `deploy/systemd/eimemory-nightly.service` has `TimeoutStartSec=7200` | **already-fixed** (`d34e67d`) |
| 4 | reward only CLI | `tests/test_b1_runtime_outcome_closed_loop.py` green; Runtime sinks hook | **already-fixed** (`18c2448`) |
| 5 | v1 LLM patch trust | `test_v1_local_commands_ignore_untrusted_patch_fields` green | **already-fixed** (`dda8434`) |
| 6 | jsonl duplicate ~920 lines | `tests/test_jsonl_single_definition.py` green | **already-fixed** (`2ddab83`) |
| 7 | health unauth fingerprints | `tests/test_eibrain_rpc_contract.py` SEC-2 compact health | **already-fixed** (`79997e8`) |
| 8 | graph N+1 | Still present | **deferred** |
| 9 | single-conn RLock | Still default; readonly path gated | **deferred** |
| 10 | written-not-wired pattern | MIS-1 instance closed; others remain | **partial** |

## Tests

Focused suite: `tests/test_remaining_modules_remediation_20260923.py` + related terminal/storage/arch01/l1 — **48 passed** in the release verification set.

Pre-existing failures on clean `0aefc4a` (not regressions): `test_unanswerable_high_similarity_is_no_evidence` (status `unavailable` vs `no_evidence`), `test_int08_model_allowlist` (empty `ALLOWED_REVIEW_MODELS`), one `test_project_context_through_engine` bridge case.

## Release

- Version: **1.13.28**
- Do **not** deploy Hongxin in this wave.
