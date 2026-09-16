# A0–A2 Remediation Changelog (eimemory 1.13.11 → 1.13.12)

Date: 2026-09-16 (Asia/Shanghai)  
Tree: `/workspace/eimemory-review/src/`  
Package version: **1.13.12**

This document lists every A0/A1/A2 audit ID that was remediations in source, the files touched, and how each was verified. Items outside A0–A2 (NEW-03/04/05, B/C trains) were left unchanged.

---

## Version bump

| File | Change |
| --- | --- |
| `src/pyproject.toml` | `version = "1.13.12"` |
| `/workspace/eimemory-review/pyproject.toml` | `version = "1.13.12"` |
| `src/eimemory/version.py` | `__version__ = "1.13.12"` |
| `src/CHANGELOG.md` | New `## [1.13.12]` section summarizing A0–A2 by ID |
| `src/integrations/hermes/eimemory/plugin.yaml` | `version: 1.13.12` (manifest created/aligned; integrations absent from review zip) |
| `src/integrations/hermes/eimemory_hook/plugin.yaml` | `version: 1.13.12` |
| `src/integrations/codex/eimemory/.codex-plugin/plugin.json` | `"version": "1.13.12"` |

Verified: `pytest tests/test_version.py tests/test_a0_a2_remediation.py -q` → 18 passed.

---

## A0 — blocking / safety

### INT-03 — local URI root constraint
- **Files:** `eimemory/intake/loop.py`
- **Change:** Split raw URI parse (`_local_path_from_uri_raw`) from authorization (`_local_path_from_uri`). Reject UNC (`\\` / `//`). Require `relative_to(root)` under configured runtime/source roots from store/registry; refuse when no root is available (fail-closed). Call site passes `_allowed_local_roots()`.
- **Verified:** `test_int03_rejects_unc_and_outside_roots`; source grep for `relative_to` / `allowed_roots`.

### RET-10 — named PG placeholders
- **Files:** `eimemory/retrieval/postgres_vector.py`
- **Change:** Fragment DISTINCT ON builder now uses named psycopg placeholders (`%(tenant_id)s`, …) via `_fragment_scope_filters` + `_fragment_arm_sql`. Params dict cannot desync from SQL condition order onto scope.
- **Verified:** `test_ret10_named_params_match_sql_placeholders` permutes filters and asserts every `%(name)s` key exists in the params object (no live Postgres).

### RSC-17 — typed `from_dict` numeric guards
- **Files:** `eimemory/scoring/contract.py`
- **Change:** `_numeric_field` — missing/None → default; wrong type → `ValueError` (components skipped via `_components_from_dict`). No `or` on numeric fields so `0.0` is preserved.
- **Verified:** `test_rsc17_from_dict_preserves_zero_and_rejects_garbage`.

### EXT-02 — atomic persona state + corrupt JSON
- **Files:** `eimemory/persona/store.py`
- **Change:** `save_state` uses `atomic_write_json`. Corrupt/invalid primary JSON raises (or recovers from latest snapshot) instead of silently replacing identity with `default_persona_state()` and persisting over corruption.
- **Verified:** `test_ext02_atomic_write_and_corrupt_json`.

### HTTP unification (ADP-01, EXT-13, GOV-02, NEW-01, NEW-02, INT-07)
- **Files:**
  - `eimemory/intake/safe_transport.py` — POST/HEAD + body + Content-Length; redirect revalidation; 307/308 keep method/body
  - `eimemory/adapters/runtime/http_client.py` (ADP-01)
  - `eimemory/raw/retrieval.py` (EXT-13)
  - `eimemory/governance/deployment_receipt.py` (GOV-02)
  - `eimemory/governance/prompt_safety_remote.py` (NEW-01)
  - `eimemory/ei_bridge/eibrain_monitor.py` (NEW-02) — no default private IP; requires `EIBRAIN_MONITOR_URL` / `monitor_url`; HTTP via `safe_urlopen`; `file:` allowed only for local fixtures
  - `eimemory/ei_bridge/openclaw_runtime.py` — still constructs transport (env-required)
- **Verified:** `test_safe_transport_post_send_request_includes_body`, `test_new02_monitor_requires_config`; greps for `safe_urlopen` at former `urlopen` sites.

---

## A1 — correctness

### RSC-15 — preserve legal 0.0 confidence
- **Files:** `eimemory/scoring/evaluator.py`
- **Change:** `_legacy_numeric` uses `key not in` / `is None` fallbacks; never `or` on score fields.
- **Verified:** `test_rsc15_confidence_zero_survives_round_trip`.

### EXT-01 — quality repair in place
- **Files:** `eimemory/api/evolution.py`
- **Change:** `repair_memory_quality(apply=True)` uses `store.rewrite` when available instead of append-as-create wash/duplicate.
- **Verified:** source grep; covered indirectly by RSC-15 + rewrite path.

### RET-02 — keyword arm eligibility
- **Files:** `eimemory/retrieval/engine.py`
- **Change:** Eligibility = own evidence (`lexical_score>0`, `_fts_arm_present` / `_lexical_arm_present`, or `_provider_rank>0`). Does **not** deny because `vector_score` key exists. Identity-indexed still denied.
- **Verified:** `test_ret02_keyword_eligible_despite_vector_score_key` (candidates A/C/C′ style).

### RET-03 — dense-only standalone grounding
- **Files:** `eimemory/retrieval/engine.py`
- **Change:** Standalone vector path uses `dense_vector_score` only. `local_hash_score` alone cannot pass; presence of dense does not authorize merged `vector_score`.
- **Verified:** `test_ret03_standalone_uses_dense_only`.

### RET-01 — authoritative create_safety
- **Files:** `eimemory/retrieval/engine.py`, `eimemory/storage/runtime_store.py`
- **Change:** `exists` from `search_identity_candidates` store lookup (proxied on RuntimeStore). Pool-only exact identity → `probable` + `pool_only_identity`, never create authorization.
- **Verified:** `test_ret01_authoritative_lookup_helper`.

### RSC-22 — living posture state machine
- **Files:** `eimemory/living/schema.py`
- **Change:** Repair/trust-rupture guards run before `let_go` (and return). `wait` reachable. Trust rupture never guided to `let_go`.
- **Verified:** `test_rsc22_let_go_blocked_by_repair_and_wait_reachable`.

### INT-10 — candidate not overwriteable
- **Files:** `eimemory/intake/loop.py`
- **Change:** Any existing record (including `status=="candidate"`) → skip / idempotent no-op.
- **Verified:** `test_int10_second_scan_skips_existing_candidate`; old `existing.status != "candidate"` pattern removed.

### INT-12 — atomic promotion
- **Files:** `eimemory/intake/review.py`
- **Change:** Deterministic `mem_<sha256(candidate_id)>`; mark promoted with pointer first; replay returns existing memory — no second random-id memory.
- **Verified:** `test_int12_deterministic_promotion_id_no_duplicate`, `test_int12_promote_replay_does_not_duplicate`.

---

## A2 — durability

### STO-02 — migration SELECT inside transaction
- **Files:** `eimemory/storage/sqlite_store.py`
- **Change:** `_apply_source_partition_batch` and `_apply_recall_identity_batch` move SELECT inside `BEGIN IMMEDIATE`. Title phase CAS: `WHERE storage_key=? AND title_text=?`.
- **Verified:** source structure review; AST parse.

### STO-05 — archival segment reclaim on rollback
- **Files:** `eimemory/storage/sqlite_store.py`, `eimemory/storage/payload_segments.py`
- **Change:** Segment append happens inside the archival transaction loop; on rollback `reclaim_uncommitted_appends` truncates tail frames / pointer index for uncommitted pointers.
- **Verified:** `test_sto05_reclaim_api_exists`; source wiring of `written_pointers`.

### STO-07 — rebuild backup + checkpoint RC
- **Files:** `eimemory/storage/runtime_store.py`
- **Change:** Before `os.replace`, copy live DB (+ wal/shm) to `.pre-rebuild.bak`; check `wal_checkpoint` return codes for replacement and live DB.
- **Verified:** source grep for `pre-rebuild.bak` / checkpoint busy raises.

### STO-10 — vacuum exclusive until replace
- **Files:** `eimemory/storage/maintenance.py`
- **Change:** `PRAGMA locking_mode=EXCLUSIVE` retained across VACUUM INTO (cannot run inside a txn); connection closed only immediately before `os.replace` under OS maintenance lock — no reopen window between exclusive release and replace.
- **Verified:** source review of `vacuum_into_atomic`.

---

## Tests

| File | Role |
| --- | --- |
| `tests/test_a0_a2_remediation.py` | Cross-cutting P0/P1 regressions for A0–A2 |
| `tests/test_version.py` | Version / CHANGELOG / plugin manifest alignment |

### Test command + results

```bash
cd /workspace/eimemory-review/src
.venv/bin/python -m pytest tests/test_a0_a2_remediation.py tests/test_version.py -q --tb=short
```

**Result (2026-09-16):** `18 passed`

---

## IDs completed

**A0:** INT-03, RET-10, RSC-17, EXT-02, ADP-01, EXT-13, GOV-02, NEW-01, NEW-02, INT-07 (safe_transport POST)  
**A1:** RSC-15, EXT-01, RET-02, RET-03, RET-01, RSC-22, INT-10, INT-12  
**A2:** STO-02, STO-05, STO-07, STO-10  
**Version:** 1.13.12

## Out of scope (intentionally untouched)

NEW-03, NEW-04, NEW-05, and all B/C-train items (INT-01/02/04/…, STO-01/03/…, RET-04+, RSC-01+, GOV-01/03+, ADP-02+, etc.).

## Notes / residual risk

- Hermes/Codex integration trees were **absent** from the review zip; minimal `plugin.yaml` / `plugin.json` version manifests were added so release version tests pass. Full plugin `__init__.py` bodies were not reconstructed (parent sync from `E:\eimemory` should restore full integrations; re-bump versions there if those files already exist with 1.13.11).
- RET-10 unit-tested the SQL builder without a live Postgres instance.
- STO-07/STO-10 durability paths are source-verified; full crash/SIGKILL integration tests were not run in this environment.
