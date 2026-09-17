# Post-deploy open items — 2026-09-18 (Asia/Shanghai / CST+8)

| Field | Value |
| --- | --- |
| Date | 2026-09-18 |
| Base HEAD (work start) | `0e4171e` (prod live 1.13.15) |
| Scope | `/workspace/eimemory` only; no commit/push/deploy; no E:/prod edits |

## Production context (user report)

- Deploy succeeded: `technical_commit_complete=1`, RPC + 4 Gateways active, storage healthy
- Real status/recall/Hermes replay/receipt loop OK
- `code.implementation:v10` + hermes binding v10 live, advertisement fresh

## Issue table

| ID | Severity | Status | Disposition |
| --- | --- | --- | --- |
| **P1** | High | **fixed** (this pass) | Worker/outer health-collect used `curl` (exit **23** write/pipe) and Tailscale primary health, mapping ancillary non-zero into outer `ok=false` despite technical commit + identity OK. |
| **P2** | Medium | **fixed** (this pass) | Multi-profile/Hongrui processes kept stale `EIMEMORY_RUNTIME_RELEASE_DIR` and Tailscale `EIMEMORY_RPC_URL` across switch; managed drop-ins did not rewrite them. |
| **P3** | Medium | **fixed** (this pass) + ops note | Diagnostic/sample-starved `recall_quality_gate` failed closed on `sample_count<=0` (`recall_quality_gate_failed`) while L5 waited for samples. |
| **P4** | Low | **clarified** (intentional) + reason fix | Catalog lifecycle `waiting` until **2** sealed incubation preflight passes for the current binding; auto-effect remains fail-closed. |

## P1 — Worker receipt false failure

**Root cause**

- Default post-deploy health in `promotion_manager` used `curl -fsS`, whose write/pipe failures return exit **23**.
- `deploy/check_user_systemd_owner.sh` also used `curl` and required a Tailscale primary health URL in addition to loopback.
- Outer orchestration treated any non-zero collect exit as overall `ok=false`, mislabeling a successful technical commit.

**Fix**

- New `deploy/collect_release_health.py`: urllib-based collect; exits **only 0/1/2**.
- Owner check + default post-deploy health use the collector (loopback / probe-only).
- Curl write/pipe codes are no longer propagated into outer ok.

**Keep fail-closed**: identity/service health failures still exit 1.

## P2 — Multi-profile stale env

**Root cause**

- Managed `zzzz-eimemory-python-runtime.conf` only set `EIMEMORY_RUNTIME_COMMIT`.
- OpenClaw managed drop-in still shipped Tailscale `EIMEMORY_RPC_URL=http://100.105.189.120:8091/`.
- Profile-specific absolute `EIMEMORY_RUNTIME_RELEASE_DIR` values were never overwritten on switch.

**Fix**

- Final-authority python-runtime drop-in now sets:
  - `EIMEMORY_RUNTIME_RELEASE_DIR=/opt/eimemory/current`
  - `EIMEMORY_RPC_URL=http://127.0.0.1:8091/`
- Hermes + OpenClaw managed confs aligned to loopback RPC + current release dir.

**Ops**: restart/reload user units after next deploy so effective env picks up the rewritten drop-ins (installer already does daemon-reload + restarts on switch).

## P3 — recall_quality_gate diagnostic / observation loop

**Root cause**

- `evaluate_production_recall_quality_gate` treated `sample_count<=0` as `recall_quality_gate_failed`.
- RI-12 already made *missing* quality_gate vacuous in nightly aggregation, but an empty/sample-starved diagnostic report still produced a failing nested gate.

**Fix**

- Empty/sample-starved → vacuous `ok=True` with `skipped_reason=sample_starved_or_unconfigured`.
- Real pollution/latency failures with samples remain fail-closed.

**Ops-only remainder**: once diagnostic samples exist, real metric failures are still visible and must be remediated from production evidence (not weakened here).

## P4 — Code capability catalog lifecycle “waiting”

**Clarification (intentional)**

- v10 binding/provider/advertisement can be healthy while catalog lifecycle still requires **2** sealed incubation preflight receipts for the current binding (`catalog_passes`).
- Until those receipts exist, owner status stays `waiting` and auto-effects remain fail-closed. This is not a credit bug by itself.

**Fix**

- Clearer reason when structurally ready but passes incomplete: `catalog_lifecycle_passes_incomplete` (was misleading `sealed_catalog_unavailable`).

**Ops-only remainder**: run/wait for capability incubation to credit two valid passes for the v10 binding; do not bypass fail-closed safety.

## Files changed

- `deploy/collect_release_health.py` (new)
- `deploy/check_user_systemd_owner.sh`
- `deploy/systemd/eimemory-python-runtime.conf`
- `deploy/systemd/hermes-gateway-eimemory.conf`
- `deploy/systemd/openclaw-gateway-eimemory.conf`
- `eimemory/governance/promotion_manager.py`
- `eimemory/evaluation/production_recall.py`
- `eimemory/ops/code_implementation_owner.py`
- `tests/test_post_deploy_open_2026_09_18.py` (new)
- `tests/test_deployment_tools.py`
- `docs/audit/POST-DEPLOY-OPEN-2026-09-18.md` (this file)

## Test evidence (target)

- `tests/test_post_deploy_open_2026_09_18.py`
- `tests/test_deployment_tools.py -m linux_deployment` (keep green)

## Ready to commit

Working tree under `/workspace/eimemory` only. **Do not commit/push from this agent** (per task).
