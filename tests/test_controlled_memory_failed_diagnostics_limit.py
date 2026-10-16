"""The ordinary ten-case controlled run retains every bounded safe failure."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from eimemory.scheduler.result_contract import nightly_result_diagnostics


PRIVATE = "private-source-and-query"


def _sample(index, *, passed=False):
    return {"passed": passed, "evaluable": True, "case_id": "controlled-memory-" + f"{index:024x}",
        "failure_reason": "known_item_missing", "returned_record_count": 0,
        "source_binding": {"record_id": "mem_" + f"{index:032x}", "source_id": PRIVATE,
            "payload_digest": f"{index:064x}", "scope": {"user_id": PRIVATE}, "title": PRIVATE},
        "query_generation": {"material_field": "title", "material_chars": 42, "query_chars": 42,
            "cleaned_query_chars": 42, "truncated": False, "query": PRIVATE},
        "retrieval_diagnostics": {"target_trace": {"schema": "controlled_target_stage_trace.v1",
            "status": "completed", "query": PRIVATE, "stages": [
                {"stage": "filtered", "target_presence": "absent_in_observed_window",
                 "drop_reasons": ["hard_filter", PRIVATE], "query": PRIVATE}]}}}


def _diagnostics(samples):
    return nightly_result_diagnostics({"memory_eval_ci": {
        "dataset_source": "controlled_records", "samples": samples}}, [])["memory_benchmark"]


@pytest.mark.parametrize("count", [6, 10, 11])
def test_failure_detail_limit_covers_six_and_ten_but_truthfully_truncates_eleven(count):
    samples = [_sample(index) for index in range(count)]
    snapshot = deepcopy(samples)
    result = _diagnostics(samples)
    failures = result["failed_case_refs"]
    assert len(failures) == min(count, 10)
    assert [item["case_id"] for item in failures] == [item["case_id"] for item in samples[:10]]
    assert result["failed_case_ref_count"] == count
    assert result["failed_case_refs_truncated"] is (count > 10)
    assert len(result["case_results"]) == count
    assert result["case_results_truncated"] is False
    for index, failure in enumerate(failures):
        assert failure["source_binding"] == {
            "record_id": "mem_" + f"{index:032x}", "payload_digest": f"{index:064x}",
            "source_id_digest": sha256(PRIVATE.encode()).hexdigest()}
        assert failure["failure_reason"] == "known_item_missing"
        assert failure["query_generation"]["cleaned_query_chars"] == 42
        filtered = next(row for row in failure["retrieval_diagnostics"]["target_trace"]["stages"]
                        if row["stage"] == "filtered")
        assert filtered["drop_reasons"] == ["hard_filter"]
        assert "scope" not in failure["source_binding"] and "query" not in failure["query_generation"]
    assert PRIVATE not in json.dumps(result)
    assert samples == snapshot


def test_scan_and_case_result_limits_remain_five_hundred_and_twenty():
    samples = [_sample(0), *[_sample(index, passed=True) for index in range(1, 500)], _sample(500)]
    result = _diagnostics(samples)
    assert len(result["case_results"]) == 20
    assert result["case_results_truncated"] is True
    assert [item["case_id"] for item in result["failed_case_refs"]] == [samples[0]["case_id"]]
    assert result["failed_case_ref_count"] == 1
    assert result["failed_case_refs_truncated"] is True
    assert PRIVATE not in json.dumps(result)
