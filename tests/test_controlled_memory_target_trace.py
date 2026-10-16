"""Single-target observations on the real engine, without query/ranking changes."""
import asyncio
from contextvars import copy_context
from copy import deepcopy
from dataclasses import asdict
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, CandidateRequest, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.sqlite_source import SQLiteCandidateSource
from eimemory.retrieval.target_trace import (
    MAX_INSPECTED_REFS, SCHEMA, STAGES, current_target_trace,
    native_target_trace_scope, safe_target_trace_report, target_trace_scope,
)


SCOPE = ScopeRef("synthetic", "target-audit", "isolated", "synthetic-owner")


@pytest.fixture(autouse=True)
def prohibit_models_and_pin_existing_selector(monkeypatch):
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    def prohibited(*args, **kwargs):
        raise AssertionError("Remote model prohibited")
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete", prohibited)


def memory(runtime, title, *, index=0, meta=None):
    record = RecordEnvelope.create(kind="memory", title=title, summary=title,
        content={"text": title, "memory_type": "fact"}, scope=SCOPE,
        source="operator.memory", source_id="hermes",
        meta={"memory_type": "fact", "force_capture": True, **(meta or {})})
    record.record_id = f"mem_synthetic_target_{index}"
    return runtime.store.append(record)


class NativeCandidates(SQLiteCandidateSource):
    def __init__(self, store, records=(), *, cap=False, fail=False, nested=None):
        super().__init__(store)
        self.records, self.cap, self.fail, self.nested = tuple(records), cap, fail, nested
        self.calls = 0

    def search(self, request):
        self.calls += 1
        if self.fail:
            raise TimeoutError("private synthetic provider body")
        if self.nested is not None:
            callback, self.nested = self.nested, None
            callback(request)
        records = [record for record in self.records if not request.kinds or record.kind in request.kinds]
        if self.cap and records:
            records = [records[0]] * request.limit + [records[-1]]
        hits = tuple(CandidateHit(ref=CandidateRef(record.record_id, ExactScope.from_scope(record.scope), record.source_id),
            source_rank=index, source_score=0.9,
            component_hints={"vector_score": 0.9, "_provider_rank_is_hybrid": True},
            evidence_hints=("vector_match",)) for index, record in enumerate(records, 1))
        return CandidateBatch(hits=hits, diagnostics={"source_name": "sqlite", "candidate_count": len(hits),
            "candidate_limit": request.limit, "elapsed_ms": 0.1, "fallback": False, "drops": {}})


def install(runtime, source):
    engine = GovernedRecallEngine(store=runtime.store, candidate_source=source)
    runtime.memory = MemoryAPI(runtime.store, recall_engine=engine)
    return engine


def request(target, query, *, source_ids=("hermes",), scope=SCOPE, kinds=()):
    return CandidateRequest.create(query=query, scope=scope, source_ids=source_ids,
        target_source_id="hermes", limit=5, kinds=kinds,
        task_context={"exact_scope_only": True})


def target_ref(target):
    return CandidateRef(target.record_id, ExactScope.from_scope(target.scope), target.source_id)


def stages(trace):
    return {row["stage"]: row for row in trace["stages"]}


def run(runtime, dataset):
    return run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])


@pytest.mark.parametrize("present", [True, False])
def test_same_native_fourteen_grounding_rejections_have_different_target_membership(tmp_path, present):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "a" * 350)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        before = deepcopy(dataset)
        distractors = [memory(runtime, f"Synthetic unrelated stellar location {i}", index=i + 1)
                       for i in range(13 if present else 14)]
        install(runtime, NativeCandidates(runtime.store, [target, *distractors] if present else distractors))
        sample = run(runtime, dataset)["samples"][0]
        assert dataset == before
        assert sample["failure_reason"] == "known_item_missing" and sample["returned_record_count"] == 0
        diagnostics = sample["retrieval_diagnostics"]
        assert diagnostics["engine"]["candidate_count"] == 14
        assert diagnostics["selector"]["input_count"] == 14
        assert diagnostics["selector"]["dropped_reasons"] == {"grounding": 14}
        trace = diagnostics["target_trace"]
        assert trace["schema"] == SCHEMA and trace["status"] == "completed"
        rows = stages(trace)
        presence = "present" if present else "absent_in_observed_window"
        assert rows["provider_returned"]["target_presence"] == rows["selector_input"]["target_presence"] == presence
        assert rows["selector_input"]["drop_reasons"] == (["grounding"] if present else [])
        assert rows["selector_input"]["grounding_score"] == (0.0 if present else None)
        assert rows["selector_input"]["grounding_threshold"] == (0.08 if present else None)
        assert rows["final_items"]["target_presence"] == "absent_in_observed_window"
        assert sample["query_generation"] == {"material_field": "title", "material_chars": 350,
                                                "query_chars": 300, "cleaned_query_chars": 300, "truncated": True}
        serialized = json.dumps({"trace": trace, "generation": sample["query_generation"]})
        assert all(value not in serialized for value in ("a" * 300, SCOPE.user_id, SCOPE.workspace_id, "query_digest"))


def test_observer_does_not_change_native_query_candidates_scores_or_selection(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid green notebook archive")
        distractor = memory(runtime, "Synthetic irrelevant stellar map", index=1)
        source = NativeCandidates(runtime.store, (target, distractor))
        engine = install(runtime, source)
        original = request(target, target.title)
        plain = engine.recall(original)
        with target_trace_scope(engine, target_ref(target), query=original.query, source_ids=original.source_ids) as trace:
            measured = engine.recall(original)
        assert source.calls == 2
        assert original.task_context_dict() == {"exact_scope_only": True}
        assert [item.to_dict() for item in plain.items] == [item.to_dict() for item in measured.items]
        assert plain.explanation["relevance_selector"] == measured.explanation["relevance_selector"]
        assert plain.explanation["fusion"] == measured.explanation["fusion"]
        rows = stages(trace.report())
        assert rows["selector_input"]["target_presence"] == rows["final_items"]["target_presence"] == "present"


def test_native_provider_cap_records_only_actual_target_cap_branch(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "a" * 350)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        distractor = memory(runtime, "Synthetic stellar map", index=1)
        install(runtime, NativeCandidates(runtime.store, (distractor, target), cap=True))
        rows = stages(run(runtime, dataset)["samples"][0]["retrieval_diagnostics"]["target_trace"])
        assert rows["provider_returned"]["target_presence"] == "present"
        assert rows["provider_bounded"]["target_presence"] == "absent_in_observed_window"
        assert rows["provider_bounded"]["truncated"] is True
        assert rows["provider_bounded"]["drop_reasons"] == ["provider_over_limit"]
        assert rows["selector_input"]["drop_reasons"] == []


def test_unavailable_native_source_keeps_unexecuted_stages_unknown(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime, "Synthetic authorized memory")
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        install(runtime, NativeCandidates(runtime.store, fail=True))
        sample = run(runtime, dataset)["samples"][0]
        trace = sample["retrieval_diagnostics"]["target_trace"]
        assert sample["failure_reason"] == "controlled_retrieval_unavailable" and trace["status"] == "aborted"
        assert all(row["target_presence"] == "unknown" and row["observed_count"] is None
                   for row in trace["stages"])
        assert "private synthetic provider body" not in json.dumps(trace)


@pytest.mark.parametrize("mismatch", ["query", "scope", "sources", "store"])
def test_temporary_binding_requires_exact_native_query_and_authorization(tmp_path, mismatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Synthetic current memory")
        engine = install(runtime, NativeCandidates(runtime.store, (target,)))
        req = request(target, target.title)
        if mismatch == "query":
            req = request(target, "Different synthetic query")
        elif mismatch == "scope":
            req = request(target, target.title, scope=ScopeRef("synthetic", "target-audit", "isolated", "other"))
        elif mismatch == "sources":
            req = request(target, target.title, source_ids=("hermes", "private"))
        with target_trace_scope(engine, target_ref(target), query=target.title, source_ids=("hermes",)) as trace:
            if mismatch == "store":
                with native_target_trace_scope(object(), req):
                    assert current_target_trace(engine) is None
            else:
                engine.recall(req)
        assert trace.report()["status"] == "binding_mismatch"
        assert all(row["target_presence"] == "unknown" for row in trace.report()["stages"])


def test_native_nested_same_request_cannot_write_or_reclaim_parent_trace(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Synthetic notebook preference")
        source = NativeCandidates(runtime.store, (target,))
        engine = install(runtime, source)
        req = request(target, target.title)
        source.nested = lambda _request: engine.recall(req)
        with target_trace_scope(engine, target_ref(target), query=req.query, source_ids=req.source_ids) as trace:
            engine.recall(req)
            first = deepcopy(trace.report())
            engine.recall(req)
        assert source.calls == 3
        assert trace.report() == first
        assert stages(first)["provider_returned"]["observed_count"] == 1


@pytest.mark.parametrize("copied", ["thread", "task"])
def test_inherited_context_cannot_steal_parent_invocation(copied):
    engine = type("Engine", (), {"store": object()})()
    ref = CandidateRef("mem_synthetic_target", ExactScope.from_scope(SCOPE), "hermes")
    req = CandidateRequest.create(query="Synthetic query", scope=SCOPE, source_ids=["hermes"],
        target_source_id="hermes", task_context={"exact_scope_only": True})

    def invocation():
        with native_target_trace_scope(engine, req):
            active = current_target_trace(engine)
            if active is not None:
                active.observe("provider_returned", [ref])
            return active is not None

    async def exercise():
        with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids) as trace:
            if copied == "task":
                assert await asyncio.create_task(async_invocation()) is False
            else:
                context = copy_context()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    assert pool.submit(context.run, invocation).result(timeout=5) is False
            assert trace.report()["status"] == "not_observed"
            assert invocation() is True
            assert trace.report()["status"] == "completed"
            assert stages(trace.report())["provider_returned"]["observed_count"] == 1

    async def async_invocation():
        return invocation()
    asyncio.run(exercise())


def test_observation_is_bounded_does_not_consume_iterators_and_unknown_is_sticky():
    engine = type("Engine", (), {"store": object()})()
    ref = CandidateRef("mem_synthetic_target", ExactScope.from_scope(SCOPE), "hermes")
    other = CandidateRef("mem_synthetic_other", ref.scope, ref.source_id)
    with target_trace_scope(engine, ref, query="Synthetic query", source_ids=["hermes"]) as trace:
        trace.observe("provider_returned", [other] * MAX_INSPECTED_REFS + [ref])
        row = stages(trace.report())["provider_returned"]
        assert row["target_presence"] == "unknown" and row["truncated"] is True
        assert row["inspected_count"] == MAX_INSPECTED_REFS
        consumed = []
        def unconsumed():
            consumed.append(True)
            yield other
        trace.observe("provider_bounded", unconsumed(), append=True)
        trace.observe("provider_bounded", [other], append=True)
        row = stages(trace.report())["provider_bounded"]
        assert consumed == [] and row["target_presence"] == "unknown" and row["observed_count"] is None
        trace.observe("provider_bounded", [ref], append=True)
        assert stages(trace.report())["provider_bounded"]["target_presence"] == "present"
        assert stages(trace.report())["provider_bounded"]["window_complete"] is None


def test_trace_sanitizer_rejects_huge_numbers_and_private_fields():
    raw = {"schema": SCHEMA, "status": "completed", "query": "private query body", "scope": asdict(SCOPE),
        "stages": [{"stage": "selector_input", "target_presence": "present",
            "observed_count": True, "inspected_count": "1", "grounding_score": 10 ** 10000,
            "grounding_threshold": float("nan"), "drop_reasons": ["grounding", "private provider body"]}]}
    report = safe_target_trace_report(raw)
    row = stages(report)["selector_input"]
    assert len(report["stages"]) == len(STAGES) == 8
    assert row["observed_count"] is row["inspected_count"] is row["grounding_score"] is row["grounding_threshold"] is None
    assert row["drop_reasons"] == ["grounding"]
    assert all(value not in json.dumps(report) for value in ("private query", SCOPE.user_id, "private provider"))


@pytest.mark.parametrize("proxy_kind", ["missing_engine", "broken_store"])
def test_optional_adapter_introspection_failure_preserves_actual_native_recall(tmp_path, proxy_kind):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid green notebook archive")
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        source = NativeCandidates(runtime.store, (target,))
        install(runtime, source)
        original = runtime.memory

        class UnknownStore:
            @property
            def store(self):
                raise RuntimeError("private custom adapter store error")

        class RecallProxy:
            calls = 0
            def recall(self, **kwargs):
                self.calls += 1
                return original.recall(**kwargs)
            def __getattr__(self, name):
                if name == "recall_engine" and proxy_kind == "broken_store":
                    return UnknownStore()
                raise RuntimeError("private custom adapter attribute error")

        runtime.memory = proxy = RecallProxy()
        sample = run(runtime, dataset)["samples"][0]
        assert sample["passed"] is True and proxy.calls == source.calls == 1
        trace = sample["retrieval_diagnostics"]["target_trace"]
        assert trace["status"] == "not_observed"
        assert all(row["target_presence"] == "unknown" and row["observed_count"] is None
                   for row in trace["stages"])
        assert "private custom adapter" not in json.dumps(trace)


def test_all_native_early_return_items_are_observed_without_inventing_other_stages(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Synthetic authorized memory")
        engine = install(runtime, NativeCandidates(runtime.store, (target,)))
        req = CandidateRequest.create(query=target.title, scope=SCOPE, source_ids=["hermes"],
            target_source_id="hermes", kinds=["memory"],
            task_context={"exact_scope_only": True, "kinds": ["claim_card"]})
        with target_trace_scope(engine, target_ref(target), query=req.query, source_ids=req.source_ids) as trace:
            result = engine.recall(req)
        assert result.items == []
        rows = stages(trace.report())
        assert rows["final_items"]["target_presence"] == "absent_in_observed_window"
        assert rows["final_items"]["observed_count"] == 0
        assert all(rows[stage]["target_presence"] == "unknown" and rows[stage]["observed_count"] is None
                   for stage in STAGES if stage != "final_items")


def test_unknown_total_after_count_overflow_is_sticky_across_append():
    engine = type("Engine", (), {"store": object()})()
    ref = CandidateRef("mem_synthetic_target", ExactScope.from_scope(SCOPE), "hermes")
    class OversizedConcreteWindow(list):
        def __len__(self):
            return 1_000_001
    with target_trace_scope(engine, ref, query="Synthetic query", source_ids=["hermes"]) as trace:
        trace.observe("provider_returned", OversizedConcreteWindow([ref]), append=True)
        trace.observe("provider_returned", [ref], append=True)
        row = stages(trace.report())["provider_returned"]
        assert row["observed_count"] is None and row["target_presence"] == "present"
        assert row["inspected_count"] == 2


def test_nested_arm_masks_parent_active_even_before_native_invocation():
    engine = type("Engine", (), {"store": object()})()
    ref = CandidateRef("mem_synthetic_target", ExactScope.from_scope(SCOPE), "hermes")
    req = CandidateRequest.create(query="Synthetic query", scope=SCOPE, source_ids=["hermes"],
        target_source_id="hermes", task_context={"exact_scope_only": True})
    with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids) as outer:
        with native_target_trace_scope(engine, req):
            assert current_target_trace(engine) is outer
            with target_trace_scope(None, ref, query=req.query, source_ids=req.source_ids) as inner:
                assert current_target_trace(engine) is None
                assert inner.report()["status"] == "not_observed"
            assert current_target_trace(engine) is outer


def test_optional_arm_preparation_failure_keeps_recall_result(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid green notebook archive")
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        source = NativeCandidates(runtime.store, (target,))
        install(runtime, source)
        def unavailable(*args, **kwargs):
            raise RuntimeError("private observer setup failure")
        monkeypatch.setattr("eimemory.retrieval.target_trace._TargetTrace.__init__", unavailable)
        sample = run(runtime, dataset)["samples"][0]
        assert sample["passed"] is True and source.calls == 1
        assert sample["retrieval_diagnostics"]["target_trace"]["status"] == "not_observed"
