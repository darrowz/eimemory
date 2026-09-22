# Ops acceptance wave — 2026-09-22 (Asia/Shanghai)

| Field | Value |
| --- | --- |
| Base | `a6d726e` (1.13.18) |
| Package | **1.13.19** |
| Scope | `/workspace/eimemory` only; pushed to origin/master; **no** production Hongxin deploy on this box |

## Issue → SHA → tests

| ID | Fix | SHA | Tests |
| --- | --- | --- | --- |
| A Nightly replay list→dict | `_run_replays` dict + list normalize | `ecaa42d` | `tests/test_sch01_nightly_ok_semantics.py` |
| B Empty eval expectation | expected_empty pass; threshold 0.0 consistent | `bb5d1b0` | `tests/test_memory_eval_ci.py` |
| C Colleague gateways | discover + install refresh | `3bd41f3` | `tests/test_deployment_tools.py` |
| D unknown_production | FAQ/docs ignored; contracts classified | `aa7278b` | `tests/test_release_impact.py` |
| E Evidence-wait tagging | `evidence_waiting` not `failure_detected` | `c55d3f0` | `tests/test_release_closure_failure.py` |
| F Identity repair scope | stamp on ingest; scoped nightly skip | `31068a0` | `tests/test_identity_ops.py` |
| G Auto-label backlog | propose/queue/promote pathway | `e0e9781` | `tests/test_auto_label_proposals.py` |
| H L5 tip/lineage wait | awaiting_evidence exemptions | `d3a1c13` | `tests/test_sch01_nightly_ok_semantics.py`, `tests/test_prompt_shadow_eval_l2_gate.py` |

## Fail-closed kept

- Non-dict nightly step results (other than lists) still `step_result_not_dict`.
- Missing `ok` on dict still fail-closed (SCH-01).
- Prompt-safety **failed** still fails; only **not_ready** awaits evidence.
- Auto-label proposals are never gold; promote still requires operator accept packet.
- Production Hongxin deploy **not** performed on this box.
