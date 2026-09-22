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
