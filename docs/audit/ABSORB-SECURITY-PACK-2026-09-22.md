# Absorb note — security/boundary pack (2026-09-22)

## Base / apply

- **Base commit (exact):** `28fecb994eb2067804f4f0db7aff392b9cd903bb`
- **Package:** `/workspace/audit-absorb-20260922/eimemory_audit_28fecb9/`
- **Apply path:** full package via `apply_review.py` (emit canonical patch → `git apply --check` → `--apply`). Network-subset patch was **not** stacked.
- **Preflight:** all five original blob SHAs matched; working tree clean at base.
- **Outcome:** applied cleanly; `compileall` OK; `git diff --check` OK.

## Files changed (8 — matches planned_changes / manifest)

| Path | Operation |
|------|-----------|
| `eimemory/adapters/eibrain/rpc_server.py` | anchors |
| `eimemory/adapters/runtime/http_boundary.py` | add |
| `eimemory/adapters/runtime/http_client.py` | replace |
| `eimemory/core/strict_json.py` | add |
| `eimemory/governance/promotion_manager.py` | anchors |
| `eimemory/intake/safe_transport.py` | replace |
| `eimemory/storage/independent_evidence.py` | anchors |
| `tests/test_audit_security_boundaries.py` | add |

## Tests run (this absorb)

```text
pytest -q tests/test_audit_security_boundaries.py \
  tests/test_*transport* tests/test_*rpc* tests/test_*promotion* \
  tests/test_*rollback* tests/test_*catalog* tests/test_business_closure* \
  --maxfail=20
→ 238 passed

pytest -q <package>/tests   # informational isolated harness
→ 163 passed
```

Full suite was **not** run in this absorb.

## Docs absorbed here

- `docs/audit/ARCH-BC-AUDIT-2026-09-22.md` — ARCH-BC full audit
- `docs/audit/SECURITY-BOUNDARIES-AUDIT-2026-09-22.md` — package `AUDIT.zh-CN.md`
- `docs/audit/PERF-PLAN-2026-09-22.md` — performance plan
- `docs/audit/ABSORB-SECURITY-PACK-2026-09-22.md` — this note

## Cross-check ARCH-BC P1s vs this pack

| ID | Topic | Covered by pack? | Status after absorb |
|----|-------|------------------|---------------------|
| **GOV-01** | L0/L1 missing `safety`/`regression` default **1.0** | Partially — S05 hardened `_score_value` (NaN/out-of-range → 0.0) but **defaults for missing L0/L1 scores still 1.0** | Follow-up small fix committed separately if applied: missing → **0.0 for all tiers** |
| **GOV-02** | ledger write failure still advances state | **No** | **Open** — non-trivial; skipped this turn |
| **SCH-01** | nightly unavailable / missing → `ok:True` | **No** | **Open** — skipped this turn |
| **ARCH-01** | Data↔Control import cycle | **No** | **Open** — large refactor; out of scope |

## Remaining open from package AUDIT (not closed by this pack)

- **B02** — real artifact / pattern / rule / code rollback + reconciliation (pack only blocks false-success when `applied_artifact_ids` present)
- **B01** — cross-process atomic exclusive lock for active-surface scan (pack fails closed on incomplete/failed scan only)
- End-to-end items listed in AUDIT §4 (crash windows, orphan/watch reconciliation, outcome idempotency, health identity binding, scheduler lease reread)

## Performance plan

- PERF **P0** is equivalence safety-net tests (`tests/test_recall_perf_bounds.py`), not a speed change.
- **Not landed** in this absorb (needs carefully captured FTS top-N snapshot against real fixtures). Leave for a later PR.

## Version / CHANGELOG

- Package did not require a version bump; remain at **1.13.16**.
- No Unreleased section in current CHANGELOG practice; no version bump this turn.

## Deploy

- **Not deployed** to production (per standing workflow).
