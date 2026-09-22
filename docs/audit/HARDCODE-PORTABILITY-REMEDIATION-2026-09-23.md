# Hardcode / Portability Remediation Map (2026-09-23)

Baseline audit: `docs/audit/HARDCODE-PORTABILITY-AUDIT-2026-09-23.md` (round 4, baseline 6b5026a / 1.13.18).
Remediation release: **1.13.23** on current HEAD after `1c88e00` / 1.13.22.

| HC | Severity | Status | Primary change | Commit subject |
|----|----------|--------|----------------|----------------|
| HC-01 | P0 | closed | `EIMEMORY_TRUSTED_*` / settings resolve trust anchors; fail closed if root unset; branch accepts configured + main↔master | fix(hc-01): trusted repo root/remote/branch from config |
| HC-15 | P2/P0-adj | closed | Same resolver; remove hardcoded `master` / `/dev-project/eimemory` defaults in governance/CLI | (with HC-01) |
| HC-02 | P0 | closed | Replace Tailscale `100.105.189.120` → `127.0.0.1`; fix asserting tests | fix(hc-02): remove Tailscale IP from deploy defaults |
| HC-03 | P0 | closed | `SERVICE_USER=$(id -un)`, systemd `%h`/`%u`, scripts use `$HOME` / env | fix(hc-03): portable SERVICE_USER and home paths |
| HC-04 | P0 | closed | `identity.py` reads `EIMEMORY_AGENT_ID` / `WORKSPACE_ID` / `USER_ID` / operator; remove `FEISHU_DARROW_OPEN_ID` from source | fix(hc-04): env-driven identity, no Feishu open id hardcode |
| HC-05 | P0 | closed | `_hardware_node_from_record` derives honestly; hardware role/id/node from env | (with HC-04) |
| HC-06 | P1 | closed | Review model allowlist optional via `EIMEMORY_ALLOWED_REVIEW_MODELS`; vendor-neutral by default | fix(hc-06): optional review-model allowlist |
| HC-07 | P1 | closed | Consumers use `DEFAULT_*` / helpers from `deployment_receipt`; path mismatch raises diagnostic | fix(hc-07): deployment contract from configurable defaults |
| HC-08 | P1 | closed | `/var/lib/eimemory` silent defaults → `config.defaults.default_root()` | fix(hc-08): neutral data-root defaults |
| HC-09 | P1 | closed | `rollout_radius` default `single_scope` | fix(hc-09): neutral rollout_radius default |
| HC-10 | P1 | closed | Factory scopes from env; neutral `operator` / `main` / `default` | fix(hc-10): neutral factory scopes |
| HC-11 | P1 | closed | New reads via `eimemory.config` / `trusted` / `default_root` | (spread across HC-01/07/08) |
| HC-12 | P1 | closed | OpenClaw bridge user/path defaults under env / config root | fix(hc-12): neutral openclaw bridge defaults |
| HC-13 | P2 | closed | `docs/deployment.md` placeholders (`USER`, `${REPO_DIR}`) | fix(hc-13): deployment doc placeholders |
| HC-14 | P2 | closed | Deploy route id `deployment.primary` (+ optional legacy alias env) | fix(hc-14): configurable deploy route id |
| Guardrail | — | closed | `tests/test_no_author_hardcodes.py` bans author literals | test: guardrail against author hardcodes |

## Security intent preserved

Trusted repository root is still enforced for code-evolution effects. The anchor is now the **configured** deployment identity (`EIMEMORY_TRUSTED_REPOSITORY_ROOT`), not an author laptop path.

## Out of scope

No Hongxin production deploy in this remediation wave.

## Commit SHAs (1.13.23 wave)


| HC-01/15 | `76d9f14` |
| HC-02 | `a143422` |
| HC-03 | `063b85e` |
| HC-04/05 | `838a0d6` |
| HC-06 | `4f5765d` |
| HC-07 | `0beb275` |
| HC-08 | `bbddd10` |
| HC-09 | `9a0e48f` |
| HC-10 | `2d2a38d` |
| HC-12 | `e311a29` |
| HC-13/14 | `2206efa` |
| Guardrail | `86fb5b7` |
| Audit docs | `d1f4f89` |

