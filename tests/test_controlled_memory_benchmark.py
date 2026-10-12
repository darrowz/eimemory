from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    MAX_CONTROLLED_CASES, build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef, TimeRef

SCOPE = ScopeRef(agent_id="benchmark-member", workspace_id="benchmark-workspace", user_id="benchmark-user")


def _memory(runtime, *, title="Atlas project notebooks", text="The Atlas project uses green notebooks.",
            scope=SCOPE, source_id="hermes", status="active", meta=None):
    record = RecordEnvelope.create(kind="memory", title=title, summary=text,
        content={"text": text, "memory_type": "fact"}, scope=scope, source="operator.memory",
        source_id=source_id, meta={"memory_type": "fact", "force_capture": True, **(meta or {})})
    record.status = status
    runtime.store.append(record)
    return record


def _build(runtime, **kwargs):
    return build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"], **kwargs)


def _run(runtime, dataset, **kwargs):
    return run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"], **kwargs)


def test_controlled_cases_bind_only_exact_authorized_current_records(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        expected = _memory(runtime)
        _memory(runtime, source_id="private")
        _memory(runtime, scope=ScopeRef(agent_id=SCOPE.agent_id, workspace_id=SCOPE.workspace_id, user_id="other-member"))
        _memory(runtime, scope=ScopeRef(agent_id=SCOPE.agent_id))  # shared visibility grants no exact authority
        _memory(runtime, status="superseded")
        _memory(runtime, meta={"valid_until": "2000-01-01T00:00:00Z"})
        _memory(runtime, meta={"memory_type": "run_log"})
        dataset = _build(runtime)
        assert len(dataset["cases"]) == 1
        case = dataset["cases"][0]
        assert case["expect_any_record_id"] == [expected.record_id]
        assert case["scope"] == asdict(SCOPE)
        assert case["task_context"] == {"source_ids": ["hermes"], "target_source_id": "hermes",
                                         "exact_scope_only": True}
        assert case["source_binding"]["record_id"] == expected.record_id
        assert case["source_binding"]["source_id"] == "hermes"
        assert dataset["dataset_generation"]["skipped_reason_counts"]["source_record_expired"] == 1


def test_nonempty_controlled_dataset_executes_actual_runtime_retrieval_and_survives_readback(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        expected = _memory(runtime)
        dataset = _build(runtime)
    with Runtime.create(root=tmp_path) as runtime:
        actual = runtime.memory
        calls = []

        def recall(**kwargs):
            calls.append(kwargs)
            return actual.recall(**kwargs)

        runtime.memory = SimpleNamespace(recall=recall)
        report = _run(runtime, dataset)
        assert len(calls) == 1
        assert report["evaluated_case_count"] == report["pass_count"] == 1
        assert report["passed_threshold"] is True
        assert report["samples"][0]["returned_refs"][0]["record_id"] == expected.record_id
        assert report["memory_benchmark_accepted"] is False
        assert report["formal_business_evidence"] is False
        assert report["certifies_natural_queries"] is False
        assert report["certifies_no_answer"] is False
        assert report["seeded_record_ids"] == []
        assert "Atlas project" not in json.dumps(report)
        assert "green notebooks" not in json.dumps(report)


def test_no_source_material_is_explicitly_not_run_and_not_passed(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        dataset = _build(runtime)
        report = _run(runtime, dataset)
        assert dataset["cases"] == []
        assert dataset["dataset_generation"]["reason"] == "controlled_memory_benchmark_empty"
        assert report["memory_benchmark_status"] == "not_run"
        assert report["passed_threshold"] is False
        assert report["memory_benchmark_accepted"] is False
        assert report["sample_count"] == 0
        assert report["pass_rate"] is None


def test_authorized_sources_are_required_and_not_inferred_from_data(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=[])
        assert dataset["cases"] == []
        assert dataset["dataset_generation"]["reason"] == "authorized_source_ids_missing"
        report = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=[])
        assert report["passed_threshold"] is False


@pytest.mark.parametrize("mutation", ["status", "expiry", "content", "missing"])
def test_source_revalidation_blocks_changed_expired_and_missing_records(tmp_path, monkeypatch, mutation):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        dataset = _build(runtime)
        if mutation == "status":
            source.status = "rejected"
        elif mutation == "expiry":
            source.meta["valid_until"] = "2000-01-01T00:00:00Z"
        elif mutation == "content":
            source.content["text"] = "Changed source expectation."
        if mutation == "missing":
            monkeypatch.setattr(runtime.store, "get_by_exact_ref", lambda *args, **kwargs: None)
        else:
            runtime.store.append(source)
        monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: pytest.fail("stale binding must not execute"))
        report = _run(runtime, dataset)
        assert report["not_evaluable_count"] == 1
        assert report["pass_count"] == 0
        assert report["passed_threshold"] is False
        assert report["samples"][0]["failure_reason"] in {
            "source_record_inactive", "source_record_expired", "source_case_binding_changed", "source_record_missing"}


@pytest.mark.parametrize("mutation", ["scope", "source", "no_answer", "expected", "query"])
def test_caller_authority_and_positive_expectation_cannot_be_changed_by_dataset(tmp_path, monkeypatch, mutation):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime)
        dataset = _build(runtime)
        case = dataset["cases"][0]
        if mutation == "scope":
            case["source_binding"]["scope"]["user_id"] = "other-member"
        elif mutation == "source":
            case["source_binding"]["source_id"] = "private"
        elif mutation == "no_answer":
            case["expected_empty"] = True
        elif mutation == "expected":
            case["expect_any_record_id"] = []
        else:
            case["query"] = "fabricated natural query"
        monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: pytest.fail("tampering must not execute"))
        report = _run(runtime, dataset)
        assert report["pass_count"] == 0
        assert report["not_evaluable_count"] == 1
        assert report["memory_benchmark_accepted"] is False


def test_known_item_empty_result_fails_without_creating_no_answer_or_gold(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime)
        dataset = _build(runtime)
        monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: RecallBundle(
            items=[], rules=[], reflections=[], confidence=0.0, next_action_hint="",
            explanation={"retrieval_status": "no_evidence"}))
        report = _run(runtime, dataset)
        assert report["evaluated_case_count"] == 1
        assert report["fail_count"] == 1
        assert report["samples"][0]["failure_reason"] == "known_item_missing"
        assert report["passed_threshold"] is False
        assert report["certifies_no_answer"] is False
        assert report["incident_record_ids"] == []
        assert runtime.store.count_records(kinds=["replay_result", "incident"]) == 0


def test_authority_changed_during_recall_is_not_evaluable(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        dataset = _build(runtime)
        old = deepcopy(source)

        def recall(**kwargs):
            source.status = "rejected"
            runtime.store.append(source)
            return RecallBundle(items=[old], rules=[], reflections=[], confidence=1.0, next_action_hint="")

        monkeypatch.setattr(runtime.memory, "recall", recall)
        report = _run(runtime, dataset)
        assert report["pass_count"] == 0
        assert report["samples"][0]["evaluable"] is False
        assert report["samples"][0]["failure_reason"] == "source_case_binding_changed"


def test_unauthorized_return_is_measured_as_failure(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        source = _memory(runtime)
        private = _memory(runtime, source_id="private")
        dataset = _build(runtime)
        monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: RecallBundle(
            items=[source, private], rules=[], reflections=[], confidence=1.0, next_action_hint=""))
        report = _run(runtime, dataset)
        assert report["evaluated_case_count"] == 1
        assert report["passed_threshold"] is False
        assert report["samples"][0]["failure_reason"] == "returned_source_not_authorized_or_current"


def test_controlled_generation_has_bounded_output_and_scan(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        for index in range(MAX_CONTROLLED_CASES + 3):
            _memory(runtime, title=f"Atlas project memory {index}")
        dataset = _build(runtime, limit=1000)
        assert len(dataset["cases"]) == MAX_CONTROLLED_CASES
        assert len(dataset["dataset_generation"]["source_refs"]) == MAX_CONTROLLED_CASES
        assert dataset["dataset_generation"]["scanned_count"] <= 512


def test_future_validity_and_unparseable_validity_cannot_supply_positive_expectations(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime, meta={"valid_from": "2100-01-01T00:00:00Z"})
        _memory(runtime, meta={"valid_from": "invalid-time"})
        dataset = _build(runtime)
        assert dataset["cases"] == []
        assert dataset["dataset_generation"]["skipped_reason_counts"] == {
            "source_record_invalid_temporal_binding": 1, "source_record_not_yet_valid": 1}


def test_runtime_unavailability_is_reported_as_execution_failure_without_private_error(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime)
        dataset = _build(runtime)

        def unavailable(**kwargs):
            raise RuntimeError("private-query-text-and-provider-credentials")

        monkeypatch.setattr(runtime.memory, "recall", unavailable)
        report = _run(runtime, dataset)
        assert report["execution_ok"] is report["ok"] is False
        assert report["memory_benchmark_status"] == "not_run"
        assert report["samples"][0]["error_type"] == "RuntimeError"
        assert "private-query" not in json.dumps(report)


def test_report_does_not_copy_extra_dataset_body_fields(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime)
        dataset = _build(runtime)
        dataset["dataset_generation"]["private_body"] = "private-query-body"
        dataset["dataset_generation"]["source_refs"][0]["private_body"] = "private-memory-body"
        dataset["cases"][0]["source_binding"]["private_body"] = "private-query-body"
        monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: pytest.fail("invalid binding cannot execute"))
        report = _run(runtime, dataset)
        assert report["passed_threshold"] is False
        assert "private-query-body" not in json.dumps(report)
        assert "private-memory-body" not in json.dumps(report)


def test_stable_cutoff_includes_boundary_and_skips_later_source_records(tmp_path):
    cutoff = "2020-10-12T03:00:00Z"
    with Runtime.create(root=tmp_path) as runtime:
        before = _memory(runtime, title="Atlas before cutoff")
        boundary = _memory(runtime, title="Atlas at cutoff")
        after = _memory(runtime, title="Atlas after cutoff")
        for record, created in ((before, "2020-10-12T02:59:59Z"),
                                (boundary, "2020-10-12T11:00:00+08:00"),
                                (after, "2020-10-12T03:00:01Z")):
            record.time = TimeRef(created_at=created, updated_at=created, occurred_at=created)
            runtime.store.append(record)
        dataset = _build(runtime, stable_before=cutoff)
        assert {case["source_binding"]["record_id"] for case in dataset["cases"]} == {
            before.record_id, boundary.record_id}
        generation = dataset["dataset_generation"]
        assert generation["stable_before"] == cutoff
        assert generation["same_run_skipped_count"] == 1
        assert generation["skipped_reason_counts"] == {"source_not_stable_before_cutoff": 1}
        report = _run(runtime, dataset)
        assert report["dataset_generation"]["stable_before"] == cutoff
        assert report["dataset_generation"]["same_run_skipped_count"] == 1


@pytest.mark.parametrize("cutoff", ["private-invalid-date", "2026-99-99T00:00:00Z", 12345])
def test_invalid_stable_cutoff_blocks_generation_without_reading_sources(tmp_path, monkeypatch, cutoff):
    with Runtime.create(root=tmp_path) as runtime:
        _memory(runtime)
        monkeypatch.setattr(runtime.store, "list_records", lambda **kwargs: pytest.fail("invalid cutoff must block reads"))
        dataset = _build(runtime, stable_before=cutoff)
        assert dataset["cases"] == []
        assert dataset["dataset_generation"]["ok"] is False
        assert dataset["dataset_generation"]["reason"] == "controlled_memory_cutoff_invalid"
        report = _run(runtime, dataset)
        assert report["passed_threshold"] is False
        assert report["blocked_reason"] == "controlled_memory_cutoff_invalid"
        assert report["ok"] is report["execution_ok"] is False
        assert "private-invalid-date" not in json.dumps(report)


def test_invalid_source_creation_time_is_skipped_under_cutoff(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        record = _memory(runtime)
        record.time.created_at = "private-invalid-source-time"
        runtime.store.append(record)
        dataset = _build(runtime, stable_before="2020-10-12T03:00:00Z")
        assert dataset["cases"] == []
        assert dataset["dataset_generation"]["skipped_reason_counts"] == {"source_created_at_invalid": 1}
        assert "private-invalid-source-time" not in json.dumps(dataset["dataset_generation"])


def test_case_budget_truncation_is_visible_separately_from_scan_truncation(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        for index in range(3):
            _memory(runtime, title=f"Atlas limit memory {index}")
        dataset = _build(runtime, limit=1)
        assert len(dataset["cases"]) == 1
        assert dataset["dataset_generation"]["selection_truncated"] is True
        assert dataset["dataset_generation"]["scan_truncated"] is False
