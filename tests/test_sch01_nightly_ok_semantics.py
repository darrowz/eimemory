"""SCH-01: nightly missing/unknown ok must not coerce to success."""
from __future__ import annotations

from eimemory.scheduler.jobs import _aggregate_nightly_ok, _nightly_step, _quality_wait_is_non_actionable


def test_nightly_step_missing_ok_is_failure() -> None:
    steps = []
    result = _nightly_step(steps, "no_ok", lambda: {"memory_count": 1})
    assert result["ok"] is False
    assert steps[-1]["ok"] is False
    assert steps[-1]["error"] == "step_ok_missing"


def test_nightly_step_non_dict_is_failure() -> None:
    steps = []
    result = _nightly_step(steps, "bare", lambda: "done")
    assert result["ok"] is False
    assert steps[-1]["ok"] is False
    assert steps[-1]["error"] == "step_result_not_dict"


def test_nightly_step_explicit_true_passes() -> None:
    steps = []
    result = _nightly_step(steps, "ok", lambda: {"ok": True, "n": 1})
    assert result["ok"] is True
    assert steps[-1]["ok"] is True


def test_aggregate_missing_step_ok_fails() -> None:
    assert _aggregate_nightly_ok({"roi": {"ok": True}}, [{"step": "roi"}]) is False
    assert _aggregate_nightly_ok({"roi": {"ok": True}}, [{"step": "roi", "ok": True}]) is True


def test_missing_recall_quality_gate_defaults_false_in_aggregate() -> None:
    gate = {
        "ok": False,
        "blocked_reason": "recall_quality_unavailable",
        "skipped_reason": "recall_quality_unavailable",
        "blocking_metrics": {},
    }
    assert _aggregate_nightly_ok({"recall_quality_gate": gate}, [{"step": "x", "ok": True}]) is False


def test_non_actionable_quality_wait_still_allowed() -> None:
    gate = {
        "ok": False,
        "blocked_reason": "recall_quality_evidence_incomplete",
        "blocking_metrics": {},
    }
    assert _quality_wait_is_non_actionable(gate) is True
    assert (
        _aggregate_nightly_ok({"recall_quality_gate": gate}, [{"step": "x", "ok": True}])
        is True
    )


def test_nightly_step_empty_list_is_ok_not_step_result_not_dict() -> None:
    """Empty successful producer list must not fail nightly aggregation."""
    steps = []
    result = _nightly_step(steps, "replay_rules", lambda: [])
    assert result["ok"] is True
    assert result["items"] == []
    assert result["count"] == 0
    assert steps[-1]["ok"] is True
    assert steps[-1]["error"] == ""


def test_nightly_step_nonempty_list_normalizes_to_ok_dict() -> None:
    steps = []
    result = _nightly_step(steps, "replay_rules", lambda: [{"rule": "r1"}])
    assert result["ok"] is True
    assert result["count"] == 1
    assert steps[-1]["ok"] is True


def test_nightly_step_non_dict_non_list_still_fail_closed() -> None:
    steps = []
    result = _nightly_step(steps, "bare", lambda: "done")
    assert result["ok"] is False
    assert steps[-1]["error"] == "step_result_not_dict"


def test_aggregate_empty_replay_dict_does_not_fail() -> None:
    report = {
        "replay_rules": {"ok": True, "reports": [], "items": [], "count": 0},
        "recall_quality_gate": {
            "ok": False,
            "blocked_reason": "recall_quality_evidence_incomplete",
            "blocking_metrics": {},
        },
    }
    steps = [{"step": "replay_rules", "ok": True, "error": ""}]
    assert _aggregate_nightly_ok(report, steps) is True


def test_l5_tip_safety_not_ready_does_not_fail_nightly_aggregate() -> None:
    from eimemory.scheduler.jobs import _aggregate_nightly_ok

    report = {
        "l5_loop": {
            "ok": False,
            "awaiting_evidence": True,
            "blocked_reason": "tip_safety_not_ready",
            "prompt_safety": {"ok": True, "status": "not_ready", "awaiting_evidence": True},
            "assessment": {"ok": True, "missing_evidence": ["prompt_safety:awaiting_evidence"]},
        }
    }
    assert _aggregate_nightly_ok(report, [{"step": "l5_loop", "ok": True}]) is True


def test_l5_real_failure_still_fails_nightly_aggregate() -> None:
    from eimemory.scheduler.jobs import _aggregate_nightly_ok

    report = {
        "l5_loop": {
            "ok": False,
            "blocked_reason": "l5_loop_timeout_exceeded",
            "assessment": {"ok": False, "missing_evidence": ["world_model:not_recorded"]},
        }
    }
    assert _aggregate_nightly_ok(report, [{"step": "l5_loop", "ok": True}]) is False
