# Code-evolution directory bounds (建议1) — 1.13.27

- **Date**: 2026-09-23 (Asia/Shanghai)
- **Status**: **closed**
- **Source**: `docs/audit/CODE-EVOLUTION-FEASIBILITY-2026-09-23.md` §3.1 建议1

## What changed

Replaced the effective “only 4 production files” radius with:

1. **Directory-level allow** via `allowed_path_globs`
2. **Invariant-level deny** (deny-self evolution plane + hard denylist)

Shared matcher: `eimemory/governance/code_evolution_path_policy.py` → `path_allowed_for_evolution`.

## Defaults

| Kind | Values |
|---|---|
| Allow globs | `eimemory/governance/**`, `eimemory/ops/**` |
| Exact pins | `deploy/runtime_identity_policy.py`, `tests/test_runtime_identity_policy.py` |
| Deny globs | `deploy/**`, `integrations/**`, `.github/**`, secrets-ish |
| Deny-self | `eimemory/governance/code_evolution*`, `code_automation_policy*`, Hermes `code_implementation.py` |

## Preserved

Proposal-only, size limits, AST authority, bwrap, peercred, kill switch, protected verification (`full_suite_required` unchanged).
