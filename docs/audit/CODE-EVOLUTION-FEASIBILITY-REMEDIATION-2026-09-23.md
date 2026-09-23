# Code-evolution feasibility remediation (1.13.26)

- **Date**: 2026-09-23 (Asia/Shanghai)
- **Baseline**: 1.13.25 / `ce4b9df` (and local remediation commits below)
- **Source assessment**: `docs/audit/CODE-EVOLUTION-FEASIBILITY-2026-09-23.md`
- **User override**: observation window fixed at **8 hours** (not 48h; not risk-tier graded)

## Mapping (建议 → status)

| 建议 | Title | Status | Notes / commits |
|---|---|---|---|
| 2 | Automated policy issue | **closed** | `issue_code_automation_policy` + CLI `eimemory learn code-evolution-policy-issue`; effects modes `all-disabled` / `commit-push-only` / `full`; write only with `--install-path`; opt-in auto-issue via `EIMEMORY_CODE_EVOLUTION_AUTO_ISSUE=1` after deploy+health |
| 6 | AST execution-authority for all incidents | **closed** | Generic AST bans importlib/os.exec*/spawn*/fork/socket/urllib/http/requests/ctypes + obvious getattr; wired for all classes; release-closure evidence gate retained |
| 7 | Allowlist vs test-plan consistency | **closed** | `_V2_ALLOWED_BUT_UNREACHABLE` + regression test; unreachable documented rather than expanding plans |
| 8 | bwrap missing degrade | **closed** | Exit 126 → structured `verification_sandbox_unavailable`; does not leave an unmarked wedge |
| 3 | max_transactions 1..N | **closed (validator)** | Loader accepts `1..8`. Store consumption remains one-shot per `policy_digest` (multi-consume deferred) |
| 5 | Effects middle state | **closed** | Per-effect gating: commit required to enter; push/deployment gated independently (`commit-push-only` stops at PUSHED) |
| 4 | Observation grading | **superseded** | User override: fixed **8 hours** / `observation_seconds=28800`; offsets `0/15m/1h/2h/4h/6h/8h`. Risk-tier map deferred |
| 1 | Directory-level boundaries | **closed** (1.13.27) | `code_evolution_path_policy` + plan/policy globs; deny-self plane; deploy/tests closed except exact runtime-identity pins |
| Portability | bwrap `--tmpfs` home | **closed** | `str(Path.home())`; keep `/etc/eimemory` + `/var/lib/eimemory` |
| Portability | Example policy v10 | **closed** | Bootstrap + full + commit-push-only examples; digests are placeholders |


## Commits (1.13.26)

| SHA | Message |
|---|---|
| `a85b232` | fix(code-evolution): shorten observation window to 8 hours |
| `178291c` | feat(code-evolution): AST execution-authority for all incidents |
| `0769597` | feat(code-evolution): issue next-round automation policy from HEAD |
| `ab74a07` | fix(code-evolution): per-effect gating, bwrap degrade, Path.home tmpfs |
| `11eb6ea` | feat(code-evolution): max_transactions 1..8 and allowlist consistency |
| `c9df502` | release: 1.13.26 code-evolution feasibility remediation |

Release HEAD: `9b78839` (includes this SHA table).

## Explicitly deferred

1. **Store multi-consume for `max_transactions>1`** — validator widened; ledger still one consumption per policy digest.
2. **risk_tier → observation hours map** — replaced by fixed 8h user override.
3. **Hongxin `/opt/eimemory` deploy** — not performed (path absent on remediation box).

## 1.13.27 follow-up (建议1 closed)

- Module: `eimemory/governance/code_evolution_path_policy.py`
- Authorization = (exact pin OR allowed globs) AND NOT hard-deny AND NOT deny-self
- Reachable under governance plans: e.g. `eimemory/governance/l5_reader.py`, `release_closure.py`, `system_code_repair.py`, `eimemory/ops/release_closure_failure.py`
- Still denied: evolution plane (`code_evolution*`, `code_automation_policy*`, Hermes adapter), `deploy/**` (except exact `deploy/runtime_identity_policy.py`), `tests/**` (except exact runtime-identity test), `.github/**`, integrations, secrets-ish paths

## Safety invariants preserved

- Proposal-only / no shell keys in proposals
- SO_PEERCRED peer uid check
- bwrap `--unshare-net` + `--ro-bind /`
- Kill switch `/etc/eimemory/code-evolution.disabled`

## Tests run (focused band)

```text
pytest tests/test_code_automation_policy_issue.py \
  tests/test_code_evolution_ast_authority.py \
  tests/test_code_evolution_allowlist_consistency.py \
  tests/test_code_evolution_semantic_validation.py \
  tests/test_code_automation_policy_v2.py \
  tests/test_code_evolution_effects.py \
  tests/test_code_evolution_security.py -q
```

Focused band result: **96 passed** (policy issue/AST/allowlist/semantic/policy_v2/effects/security).
