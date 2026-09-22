# Absorb note — 2e57f59 hardening audit pack (2026-09-23)

## Base / apply

- **Exact baseline:** `2e57f59bcab4a0e50aae8a9423c7440c4213ea03` / 1.13.19
- **Package:** `/workspace/audit-patch-20260923/eimemory_audit_2e57f59/`
- **Apply path:** detached worktree `/workspace/eimemory-audit-review` at exact baseline → `apply_review.py` check → `--apply` (never forced on master).
- **Worktree apply commit:** `b60cc44` (detached, based on `2e57f59`)
- **Master absorb commit:** cherry-pick → `52ba2bc` on `4a03c66` (1.13.20); jobs.py 3-way auto-merged (prefix 1–190 identical; B3/S2 body preserved).
- **Follow-up:** `promotion_watch_orphans` added to `NIGHTLY_NESTED_OK_ALLOWLIST` (B3 landed after pack baseline).

## Files (8)

| Path | Kind |
|------|------|
| `eimemory/adapters/runtime/circuit_breaker.py` | added |
| `eimemory/adapters/runtime/http_client.py` | modified |
| `eimemory/intake/safe_transport.py` | modified |
| `eimemory/scheduler/jobs.py` | prefix → import `result_contract` only |
| `eimemory/scheduler/result_contract.py` | added |
| `eimemory/storage/atomic_file.py` | modified |
| `eimemory/storage/bounded_jsonl.py` | added |
| `tests/test_audit_20260922_hardening.py` | added |

`http_client` / `safe_transport` / `atomic_file` blobs were unchanged on master vs baseline; jobs.py was **not** overwritten wholesale.

## Verification

| Check | Result |
|-------|--------|
| Worktree `pytest -q tests/test_audit_20260922_hardening.py` | **104 passed** |
| Master after cherry-pick: hardening + `test_sch01_nightly_ok_semantics` | **116 passed** |
| `tools/validate_isolated.py` | **informational FAIL** — isolated evidence tree lacks full package layout (`ModuleNotFoundError: eimemory.storage.bounded_jsonl` after patch apply). Full-repo worktree apply + pytest remain the authoritative gate. |
| B3 `promotion_watch_orphans` / S2 lease reread still present in `jobs.py` | yes |

## Docs

- `docs/audit/HARDENING-AUDIT-2e57f59-2026-09-22.md` — package `AUDIT.zh-CN.md`
- `docs/audit/ABSORB-HARDENING-2e57f59-2026-09-23.md` — this note

## Not claimed

- Full-suite green, production deploy, or end-to-end recall p95 ≤3s.
- Hostile-parent-dir / Windows ACL equivalence for atomic_file hardening.
