# Absorb note — recall authority/boundary pack (2026-09-23)

## Base / apply

- **Base commit (exact):** `09afdef3bb706ced01cd1f29e65e25678426f488` (`1.13.21`)
- **Package:** `/workspace/audit-pack-20260923b/eimemory_recall_09afdef3/`
- **Master at absorb start:** `c5cd2ece8bad0053344d2dde0914e10d939bb6e9` (`1.13.23`)
- **Apply path:** isolated worktree + `verify_and_apply.py` (check → `--apply`). Review `.patch` was **not** applied blindly.
- **Preflight:** HEAD matched base; four original Git blobs matched `manifest.json`; unique anchors + syntax + `git apply --check` OK.
- **Worktree apply SHA:** `b14d5afb81a6b6314e79baf9c37315c697d058ce`
- **Master absorb SHA:** `61dc50f32ec448a129bd5d297263abd315601482`

## Why reconstruction, not blind patch on master

`verify_and_apply.py` rebuilds full-context diffs from real worktree bytes and refuses missing/ambiguous anchors. The four product files (`engine.py`, `contracts.py`, `sqlite_source.py`, `raw/retrieval.py`) were **byte-identical** between `09afdef3` and `c5cd2ec`, so cherry-pick of the worktree apply landed without content conflict. 1.13.22–23 portability / budgets / storage-facade work did not rewrite these blobs; remaining integration risk is call-site wiring (e.g. engine now routes raw via `guarded_raw_search`).

## Files (8)

| Path | Operation |
|------|-----------|
| `eimemory/retrieval/engine.py` | anchors (authority gate, raw boundary, gap predicate, kinds intersect, finite scores) |
| `eimemory/retrieval/contracts.py` | anchors (`finite_float`) |
| `eimemory/retrieval/sqlite_source.py` | anchors (`bind_score_entries`) |
| `eimemory/raw/retrieval.py` | anchors (boundary decorator, rerank budgets, callback bind) |
| `eimemory/contracts/recall_boundary.py` | add |
| `eimemory/raw/boundary.py` | add |
| `eimemory/retrieval/authority_gate.py` | add |
| `tests/test_recall_authority_audit_20260923.py` | add |

## Tests run (this absorb)

```text
# isolated worktree @ 09afdef3 + apply
pytest -q tests/test_recall_authority_audit_20260923.py
→ 108 passed

# master after cherry-pick
pytest -q tests/test_recall_authority_audit_20260923.py
→ 108 passed

pytest -q tests/test_recall_engine.py tests/test_source_partition.py
→ 92 passed, 3 failed initially
```

### Absorb adaptations

- `tests/test_recall_engine.py`: two monkeypatches retargeted from
  `eimemory.retrieval.engine.search_raw_chunks` → `eimemory.raw.retrieval.search_raw_chunks`
  because engine raw collection now goes through `guarded_raw_search` (late-import). Same
  security assertions retained (`raw_source_not_allowed`, authoritative body rebuild).

### Known unrelated failure (not introduced by this absorb)

- `tests/test_source_partition.py::test_evaluation_framework_seed_preserves_explicit_source_partition`
  already failed on `c5cd2ec` before absorb (`get_by_id` → `None`). Tracked under evaluation
  remediation (Phase 2), not recall authority.

Full suite was **not** run in this absorb. No production deploy.

## Findings covered (pack AUDIT.zh-CN.md)

R-01…R-13 (authority validate on all select paths, remote revalidation without cache reuse,
exact-ref identity, raw boundary before rerank, cross-source score binding, finite floats,
gap write gating, auxiliary digest recheck, rerank budgets, callback bind-once, rule identity,
kinds intersection, confidence semantics label).

## Evidence boundary

Contract tests + SQLite fixture races ≠ full Runtime/FTS/Postgres/RPC production acceptance.
