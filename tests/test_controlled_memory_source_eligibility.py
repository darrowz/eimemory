"""Controlled positive sources must actually qualify for default memory use."""
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    _case, build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall import classify_recall_visibility, is_episode_evidence_record


SCOPE = ScopeRef(tenant_id="synthetic", agent_id="source-test", workspace_id="isolated", user_id="owner")
REASONS = {
    "capture_reject": "source_record_capture_rejected",
    "episode": "source_record_episode_evidence",
    "evidence_only": "source_record_evidence_only",
    "deployment": "source_record_evidence_only",
}


@pytest.fixture(autouse=True)
def no_external_provider(monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    monkeypatch.setattr(CommandLLMClient, "complete", lambda *a, **k: pytest.fail("External model prohibited"))


def source(runtime, *, memory_type="fact", title="Orchid project notebooks"):
    record = RecordEnvelope.create(kind="memory", title=title,
        summary="The Orchid project uses green notebooks.",
        content={"text":"The Orchid project uses green notebooks.", "memory_type":memory_type},
        scope=SCOPE, source="operator.memory", source_id="hermes",
        meta={"memory_type":memory_type, "force_capture":True, "memory_layer":"l1"})
    return runtime.store.append(record)


def make_ineligible(runtime, record, mode):
    def mutation(sqlite):
        current = sqlite.get_by_exact_ref(record.record_id, scope=SCOPE, source_id="hermes")
        if mode == "capture_reject":
            current.meta = {**current.meta, "quality":{**business_metadata(current.meta)["quality"], "capture_decision":"reject"}}
        elif mode == "episode":
            # Legacy raw turns can retain a fact type; the shared provenance
            # predicate must still recognize this as L0 episode evidence.
            current.meta = {**current.meta, "capture_origin":"turn_sync", "memory_layer":"l0"}
        elif mode == "deployment":
            current.meta = {**current.meta, "memory_type":"deployment_evidence"}
            current.content = {**current.content, "memory_type":"deployment_evidence"}
        else:
            current.source = "operator.diagnostic"
        sqlite.upsert(current, commit=False)
        return current, [current], []
    return runtime.store.mutate_records_atomically(mutation)


def build(runtime):
    return build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])


def run(runtime, dataset):
    return run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])


@pytest.mark.parametrize("mode", REASONS)
def test_default_recall_refuses_ineligible_source_and_generation_records_exact_skip(tmp_path, mode):
    with Runtime.create(root=tmp_path) as runtime:
        record = make_ineligible(runtime, source(runtime), mode)
        assert record.status == "active"
        if mode == "capture_reject":
            assert business_metadata(record.meta)["quality"]["capture_decision"] == "reject"
        elif mode == "episode":
            assert is_episode_evidence_record(record)
        else:
            assert classify_recall_visibility(record) == "evidence_only"
        # This is the real configured SQLite/default-use path, not a fixture
        # that merely mirrors the benchmark's eligibility implementation.
        bundle = runtime.memory.recall(query=record.title, scope=asdict(SCOPE), limit=5,
            task_context={"source_ids":["hermes"], "target_source_id":"hermes", "exact_scope_only":True})
        assert record.record_id not in [item.record_id for item in bundle.items]
        dataset = build(runtime)
        assert dataset["cases"] == []
        # Raw and evidence lanes are excluded before the bounded native
        # candidate hydration. Capture quality is still checked on the actual
        # payload; scan counters must not invent rows removed by SQL.
        expected = {REASONS[mode]:1} if mode == "capture_reject" else {}
        assert dataset["dataset_generation"]["skipped_reason_counts"] == expected
        assert dataset["dataset_generation"]["scanned_count"] == (1 if mode == "capture_reject" else 0)
        report = run(runtime, dataset)
        assert report["memory_benchmark_status"] == "not_run"
        assert report["passed_threshold"] is False
        assert report["memory_benchmark_accepted"] is False
        assert report["formal_business_evidence"] is False


@pytest.mark.parametrize("mode", REASONS)
def test_provided_case_is_retained_as_not_evaluable_when_source_no_longer_qualifies(tmp_path, mode):
    with Runtime.create(root=tmp_path) as runtime:
        record = source(runtime)
        dataset = build(runtime)
        assert len(dataset["cases"]) == 1
        make_ineligible(runtime, record, mode)
        runtime.memory = SimpleNamespace(recall=lambda **k: pytest.fail("Ineligible source must not execute retrieval"))
        report = run(runtime, dataset)
        assert report["sample_count"] == report["fail_count"] == 1
        assert report["evaluated_case_count"] == report["pass_count"] == 0
        assert report["not_evaluable_count"] == 1
        sample = report["samples"][0]
        assert sample["case_id"] == dataset["cases"][0]["case_id"]
        assert sample["source_binding"]["record_id"] == record.record_id
        assert sample["failure_reason"] == REASONS[mode]
        assert sample["passed"] is False and sample["evaluable"] is False
        assert "retrieval_diagnostics" not in sample
        assert report["passed_threshold"] is False
        assert report["memory_benchmark_accepted"] is False
        assert report["formal_business_evidence"] is False


@pytest.mark.parametrize("mode", REASONS)
def test_legacy_positive_case_bound_to_current_ineligible_payload_remains_failed(tmp_path, mode):
    with Runtime.create(root=tmp_path) as runtime:
        record = source(runtime)
        dataset = build(runtime)
        record = make_ineligible(runtime, record, mode)
        # The old generator could produce a positive expectation for this
        # exact current payload. Preserve that configured case rather than
        # deleting it or relying on an unrelated payload-drift rejection.
        legacy_case = _case(record)
        dataset["cases"] = [legacy_case]
        dataset["dataset_generation"]["source_refs"] = [legacy_case["source_binding"]]
        runtime.memory = SimpleNamespace(recall=lambda **k: pytest.fail("Current ineligible source must not retrieve"))
        report = run(runtime, dataset)
        assert report["sample_count"] == report["fail_count"] == 1
        assert report["pass_count"] == report["evaluated_case_count"] == 0
        assert report["samples"][0]["case_id"] == legacy_case["case_id"]
        assert report["samples"][0]["failure_reason"] == REASONS[mode]
        assert report["samples"][0]["evaluable"] is False
        assert report["passed_threshold"] is False
        assert report["memory_benchmark_accepted"] is False


@pytest.mark.parametrize("memory_type", ["fact", "episodic"])
def test_ordinary_accepted_l1_fact_and_episodic_sources_are_really_recalled(tmp_path, memory_type):
    with Runtime.create(root=tmp_path) as runtime:
        record = source(runtime, memory_type=memory_type)
        assert not is_episode_evidence_record(record)
        assert classify_recall_visibility(record) == "default"
        dataset = build(runtime)
        assert len(dataset["cases"]) == 1
        assert dataset["dataset_generation"]["skipped_reason_counts"] == {}
        report = run(runtime, dataset)
        assert report["evaluated_case_count"] == report["pass_count"] == 1
        assert report["samples"][0]["returned_refs"][0]["record_id"] == record.record_id
        assert report["passed_threshold"] is True
        assert report["memory_benchmark_accepted"] is False
        assert report["formal_business_evidence"] is False


@pytest.mark.parametrize("mode", REASONS)
def test_after_retrieval_source_requalification_cannot_certify_the_returned_candidate(tmp_path, mode):
    with Runtime.create(root=tmp_path) as runtime:
        record = source(runtime)
        dataset = build(runtime)
        native_recall = runtime.memory.recall
        def recall(**kwargs):
            bundle = native_recall(**kwargs)
            assert record.record_id in [item.record_id for item in bundle.items]
            make_ineligible(runtime, record, mode)
            return bundle
        runtime.memory = SimpleNamespace(recall=recall)
        report = run(runtime, dataset)
        sample = report["samples"][0]
        assert sample["passed"] is False and sample["evaluable"] is False
        assert sample["failure_reason"] == "source_case_binding_changed"
        assert report["pass_count"] == 0 and report["fail_count"] == 1
        assert report["passed_threshold"] is False
        assert report["memory_benchmark_accepted"] is False


def test_new_generation_does_not_rewrite_an_existing_failed_receipt(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        record = source(runtime)
        historical_report = {"samples":[{"case_id":"historical-case", "passed":False,
                             "failure_reason":"known_item_missing"}], "passed_threshold":False}
        receipt = RecordEnvelope.create(kind="replay_result", title="Historical memory receipt",
            scope=SCOPE, source="eimemory.memory_eval_ci", content={"report":historical_report})
        runtime.store.append(receipt)
        before = runtime.store.get_by_id(receipt.record_id, scope=SCOPE, exact_scope=True).to_dict()
        make_ineligible(runtime, record, "capture_reject")
        assert build(runtime)["cases"] == []
        assert runtime.store.get_by_id(receipt.record_id, scope=SCOPE, exact_scope=True).to_dict() == before
