from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_distiller import distill_capability_candidate
from eimemory.governance.regression_watch import run_regression_watch


def test_regression_watch_disables_l1_candidate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result={"verdict": "pass", "scores": {"safety": 1.0, "regression": 1.0}},
        promotion_target="tool_route",
        summary="Tool route",
    )

    result = run_regression_watch(runtime, candidate_id=candidate_id, scope=scope, loop_id="learn_test", eval_result={"verdict": "fail", "scores": {"regression": 0.2}})

    assert result["regressed"] is True
    assert result["action"] == "disabled"
    assert runtime.store.get_by_id(candidate_id).status == "disabled"


def test_regression_watch_persists_pass_observation(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    candidate_id = distill_capability_candidate(
        runtime,
        scope=scope,
        loop_id="learn_test",
        experiment_id="exp_1",
        eval_result={"verdict": "pass", "scores": {"safety": 1.0, "regression": 1.0}},
        promotion_target="tool_route",
        summary="Tool route",
    )

    result = run_regression_watch(
        runtime,
        candidate_id=candidate_id,
        scope=scope,
        loop_id="learn_test",
        eval_result={"verdict": "pass", "scores": {"regression": 1.0}},
    )
    record = runtime.store.get_by_id(result["record_id"])

    assert result["ok"] is True
    assert result["regressed"] is False
    assert result["action"] == "observed"
    assert record is not None
    assert record.kind == "regression_watch"
    assert record.content["regressed"] is False
    assert runtime.store.get_by_id(candidate_id).status == "candidate"
