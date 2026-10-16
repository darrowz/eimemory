"""An invalid controlled probe stays failed without becoming a recall miss."""
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall.query_clean import clean_user_query
from eimemory.scheduler.jobs import _memory_eval_report_record
from eimemory.scheduler.result_contract import nightly_result_diagnostics


SCOPE = ScopeRef(agent_id="isolated-agent", workspace_id="isolated-workspace::channel::hermes",
                 user_id="isolated-user")
PRIVATE = "private controlled source body"


def _memory(runtime, title):
    return runtime.store.append(RecordEnvelope.create(kind="memory", title=title,
        summary=PRIVATE, content={"text": PRIVATE, "memory_type": "fact"}, scope=SCOPE,
        source="operator.memory", source_id="hermes", meta={"memory_type": "fact", "force_capture": True}))


def _build(runtime):
    return build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])


def _run(runtime, dataset):
    return run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])


@pytest.mark.parametrize("title", ["Assistant: " + PRIVATE, "System: " + PRIVATE, "User:"])
def test_cleaned_empty_probe_preserves_case_and_failure_without_alternate_recall(tmp_path, monkeypatch, title):
    with Runtime.create(root=tmp_path) as runtime:
        target = _memory(runtime, title)
        dataset = _build(runtime)
        snapshot = deepcopy(dataset)
        assert dataset["cases"][0]["query"] == title
        assert clean_user_query(title) == ""
        monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: pytest.fail("Invalid probe must not dispatch or substitute a query"))
        report = _run(runtime, dataset)
        assert dataset == snapshot
        assert report["sample_count"] == report["fail_count"] == report["not_evaluable_count"] == 1
        assert report["pass_count"] == report["evaluated_case_count"] == 0
        assert report["threshold"] == 1.0
        assert report["passed_threshold"] is report["memory_benchmark_accepted"] is False
        sample = report["samples"][0]
        assert sample["case_id"] == dataset["cases"][0]["case_id"]
        assert sample["source_binding"]["record_id"] == target.record_id
        assert sample["failure_reason"] == "controlled_query_empty_after_cleaning"
        assert sample["passed"] is sample["evaluable"] is False
        assert sample["query_generation"]["query_chars"] == len(title)
        assert sample["query_generation"]["cleaned_query_chars"] == 0
        assert "retrieval_diagnostics" not in sample


def test_mixed_sample_set_does_not_replace_invalid_case_or_change_valid_raw_query(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        invalid = _memory(runtime, "Assistant: " + PRIVATE)
        valid_title = "User: Atlas project uses green notebooks"
        valid = _memory(runtime, valid_title)
        dataset = _build(runtime)
        snapshot = deepcopy(dataset)
        actual = runtime.memory
        calls = []
        def recall(**kwargs):
            calls.append(kwargs)
            return actual.recall(**kwargs)
        runtime.memory = SimpleNamespace(recall=recall)
        report = _run(runtime, dataset)
        assert dataset == snapshot
        assert len(calls) == 1
        assert calls[0]["query"] == valid_title
        assert calls[0]["scope"] == asdict(SCOPE)
        assert calls[0]["task_context"]["source_ids"] == ["hermes"]
        assert report["sample_count"] == 2
        assert report["pass_count"] == report["fail_count"] == 1
        assert report["pass_rate"] == 0.5
        assert report["not_evaluable_count"] == report["evaluated_case_count"] == 1
        assert report["threshold"] == 1.0 and report["passed_threshold"] is False
        samples = {item["source_binding"]["record_id"]: item for item in report["samples"]}
        assert set(samples) == {invalid.record_id, valid.record_id}
        assert samples[invalid.record_id]["failure_reason"] == "controlled_query_empty_after_cleaning"
        assert samples[valid.record_id]["passed"] is True
        assert samples[valid.record_id]["query_generation"]["cleaned_query_chars"] == len(clean_user_query(valid_title))


def test_source_authority_still_precedes_invalid_query_classification(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = _memory(runtime, "Assistant: " + PRIVATE)
        dataset = _build(runtime)
        target.meta["quality"] = {"capture_decision": "reject"}
        runtime.store.append(target)
        monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: pytest.fail("Rejected source must not dispatch"))
        sample = _run(runtime, dataset)["samples"][0]
        assert sample["failure_reason"] == "source_record_capture_rejected"
        assert sample["passed"] is sample["evaluable"] is False
        assert "query_generation" not in sample


def test_invalid_probe_diagnostics_survive_both_durable_readbacks_without_query_text(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime, "Assistant: " + PRIVATE)
        report = _run(runtime, _build(runtime))
        report["dataset_source"] = "controlled_records"
        receipt = _memory_eval_report_record(report, scope=SCOPE)
        runtime.store.append(receipt)
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": report}, [])
        failure = diagnostics["memory_benchmark"]["failed_case_refs"][0]
        assert failure["failure_reason"] == "controlled_query_empty_after_cleaning"
        assert failure["evaluable"] is False
        assert failure["query_generation"]["cleaned_query_chars"] == 0
        assert "retrieval_diagnostics" not in failure
        assert diagnostics["memory_benchmark_accepted"] is False
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        summary_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(receipt.record_id, scope=SCOPE).content["report"] == report
        assert runtime.store.get_by_id(summary_id, scope=SCOPE).content["nightly_diagnostics"] == diagnostics
        assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
    assert PRIVATE not in json.dumps(report)
    assert PRIVATE not in json.dumps(diagnostics)


@pytest.mark.parametrize("count", [None, False, -1, "private", 1_000_001])
def test_missing_or_invalid_cleaned_query_length_stays_unknown(count):
    sample = {"passed": False, "case_id": "controlled-memory-" + "1" * 24,
              "failure_reason": "known_item_missing", "query_generation": {"cleaned_query_chars": count}}
    result = nightly_result_diagnostics({"memory_eval_ci": {
        "dataset_source": "controlled_records", "samples": [sample, {"passed": False, "query_generation": {}}]}}, [])
    for failure in result["memory_benchmark"]["failed_case_refs"]:
        assert failure["query_generation"]["cleaned_query_chars"] is None
