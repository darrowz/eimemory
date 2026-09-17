# Regression fix notes — eimemory 1.13.14 (prod blockers)

Date: 2026-09-17 (Asia/Shanghai). No version bump, no commit, no deploy.

## Bug 1 — Deploy health probe blocked on 127.0.0.1:8091

**Cause:** `DEFAULT_DEPLOYMENT_HEALTH_URL = "http://127.0.0.1:8091/health"` is fetched via `_fetch_health` → `safe_urlopen`. Address policy `_is_disallowed_address` rejects `is_loopback` (and loopback is also `is_private` / non-`is_global`), so the default deploy probe always failed after the SSRF harden.

**Fix:**
- `eimemory/intake/safe_transport.py`: add explicit `allow_loopback: bool = False` on `safe_urlopen` and thread it through parse / DNS pin / peer verify / `_is_disallowed_address`. When True, loopback (127.0.0.0/8, ::1) is allowed; private non-loopback, link-local, metadata, etc. stay rejected. Default remains False (intake SSRF closed).
- `eimemory/governance/deployment_receipt.py`: `_fetch_health` is the **only** caller that passes `allow_loopback=True` (still `max_redirects=0`).

## Bug 2 — `eimemory quality stats` NameError: business_metadata

**Cause:** EXT-14 comment claimed `memory_quality_report` reads `business_metadata.quality`, but `from eimemory.metadata import business_metadata` was never added → NameError at runtime.

**Fix:** import `business_metadata` in `eimemory/api/evolution.py` and use it consistently in `memory_quality_report` (quality + memory_type).

## Bug 3 — Diagnostic recall misses evolution artifact records

**Symptom:** 诊断召回漏掉演化工件记录 (`replay_result` / operational report_only kinds).

**Hypotheses checked:**
1. `recall/loadout.py` exact-title drops — `_DROP_TITLE_EXACT` is arxiv/locomo/bfcl tokens only; evolution titles not wrongly dropped. Not the bug.
2. Hard lane block `evolution_artifact` — diagnostic sets `operational_recall_allowed` and clears default blocked lanes; memory_type=evolution_artifact already returned. Not sufficient alone.
3. Indexing / preferred_kinds — `replay_result` indexes as `lane=operational`, `visibility=report_only`. Learning_eval is primary/default (found). Key asymmetry.
4. **Root cause:** `SqliteStore._allowed_recall_lanes` when `include_evidence_only=True` returned `("primary", "knowledge", "news", "raw")` **without `operational`**. Diagnostic recall sets both `include_evidence_only` and `include_report_records`; visibility allowed `report_only`, but the lane SQL filter still excluded operational → `replay_result` / `incident` / `reflection` / `feedback` / `recall_view` dropped. Confirmed by failing `test_runtime_recall_diagnostic_mode_searches_evolution_artifact_records`.

**Fix:** In `_allowed_recall_lanes`, when `include_report_records` is True, also allow the `operational` lane (with or without evidence_only). Evidence-only alone still excludes operational so chat recall stays clean.

## Files changed
- `src/eimemory/intake/safe_transport.py`
- `src/eimemory/governance/deployment_receipt.py`
- `src/eimemory/api/evolution.py`
- `src/eimemory/storage/sqlite_store.py`
- `src/tests/test_prod_regression_1_13_14.py` (new)
- `REGRESSION-FIX-NOTES.md` (this file)

## Not done (per instructions)
- Version bump
- Git commit
- Deploy
