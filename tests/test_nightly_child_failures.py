from copy import deepcopy
import pytest
from eimemory.scheduler.result_contract import _nightly_step, _aggregate_nightly_ok


@pytest.mark.parametrize("result", [
    [{"ok": False}], [{"ok": "true"}], [{"ok": 1}], [{"error": "failed"}],
    [{"ok": True, "errors": ["failed"]}],
    {"ok": True, "reports": [{"ok": False}]},
    {"ok": True, "reports": [{"ok": True, "items": [{"ok": False}]}]},
    {"ok": True, "error": "failed"}, {"ok": True, "blocking_metrics": {"leakage": 1}},
])
def test_explicit_child_failure_is_not_wrapped_into_success(result):
    snapshot = deepcopy(result)
    steps = []
    report = _nightly_step(steps, "memory_eval_ci", lambda: result)
    assert report["ok"] is False and steps[0]["ok"] is False
    assert _aggregate_nightly_ok({"memory_eval_ci": report}, steps) is False
    assert result == snapshot


@pytest.mark.parametrize("result", [[], ["record-1"], [{"record_id": "one"}], [{"ok": True}], {"ok": True}])
def test_successful_data_producers_are_not_misclassified(result):
    steps = []
    report = _nightly_step(steps, "memory_eval_ci", lambda: result)
    assert report["ok"] is True and steps[0]["ok"] is True
    assert _aggregate_nightly_ok({"memory_eval_ci": report}, steps) is True


def test_aggregate_independently_rechecks_wrapped_errors():
    assert not _aggregate_nightly_ok({"memory_eval_ci": {"ok": True, "reports": [{"ok": False}]}}, [])


def test_recursive_report_does_not_hang():
    result = {"ok": True, "reports": []}
    result["reports"].append(result)
    steps = []
    assert _nightly_step(steps, "cycle", lambda: result)["ok"] is False


def test_legitimate_evidence_wait_remains_distinct_from_execution_failure():
    assert _aggregate_nightly_ok({"recall_quality_gate": {
        "ok": False, "blocked_reason": "recall_quality_evidence_incomplete"}}, [])
    assert not _aggregate_nightly_ok({"recall_quality_gate": {
        "ok": False, "blocked_reason": "recall_quality_evidence_incomplete", "error": "storage_failed"}}, [])
