"""Preference observations preserve policy, old hooks and request isolation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.recall import RecallIntent, classify_recall_intent
from eimemory.retrieval.contracts import CandidateRef, CandidateRequest, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine, _native_preference_bool
from eimemory.retrieval.preference_diagnostics import (
    PREFERENCE_ROUTE_SCHEMA, safe_preference_route, safe_preference_rejections,
)
from eimemory.retrieval.target_trace import native_target_trace_scope, target_trace_scope
from eimemory.scheduler.jobs import _memory_eval_report_record
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_controlled_memory_target_trace import NativeCandidates

SCOPE = ScopeRef("synthetic", "route-observation", "isolated::channel::hermes", "owner")
CONTEXT = {"source_ids": ["hermes"], "target_source_id": "hermes", "exact_scope_only": True}
PRIVATE = "private-marker-and-query"


@pytest.fixture(autouse=True)
def prohibit_models(monkeypatch):
    for key in ("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "EIMEMORY_RELEVANCE_ADMISSION_ENABLED",
                "EIMEMORY_RERANKER_ENABLED"):
        monkeypatch.setenv(key, "0")
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete",
                        lambda *a, **kw: pytest.fail("Model call prohibited"))


def memory(runtime, title, text=None, memory_type="fact"):
    text = text or title
    return runtime.store.append(RecordEnvelope.create(kind="memory", title=title, summary=text,
        content={"text": text, "memory_type": memory_type}, scope=SCOPE, source="operator.memory",
        source_id="hermes", meta={"memory_type": memory_type, "force_capture": True}))


def intent(name="generic", confidence=0.0, terms=()):
    return RecallIntent(name, confidence, (), (), (), {}, "general", terms)


def controlled(runtime):
    dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
    before = deepcopy(dataset)
    result = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])
    assert dataset == before
    return result


@pytest.mark.parametrize("query,context,recall_intent,selected,entry", [
    ("What are my preferences", {"preference_query_markers": ["preferences"]}, intent("research", .8), True, "typed_request"),
    (PRIVATE, {"preference_query_markers": [PRIVATE]}, intent("research", .8), True, "custom_marker"),
    ("preference cache", {}, intent("research", .8), False, "research_exempt"),
    ("preference cache", {}, intent(), False, "no_match"),
    ("Atlas layout", {}, intent("operator_preference", .65, ("preference",)), True, "intent_fallback"),
    ("Atlas layout", {}, intent("operator_preference", .64, ("preference",)), False, "no_match"),
    ("Atlas layout", {}, intent("operator_preference", .9, ("layout",)), False, "no_match"),
])
def test_route_decision_retains_existing_priority_and_boundaries(tmp_path, query, context, recall_intent, selected, entry):
    with Runtime.create(root=tmp_path) as runtime:
        value, diagnostic = runtime.memory._preference_query_decision(query, context, recall_intent=recall_intent)
        assert value is selected
        assert diagnostic["entry"] == entry
        assert runtime.memory._is_preference_query(query, context, recall_intent=recall_intent) is selected
        safe = safe_preference_route(diagnostic)
        assert safe_preference_route(safe) == safe
        assert PRIVATE not in json.dumps(safe)


def test_native_technical_query_keeps_target_and_route_through_both_receipts(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid preference cache layout")
        report = controlled(runtime)
        sample = report["samples"][0]
        assert sample["passed"] is True and sample["expected_rank"] == 1
        assert report["pass_count"] == 1 and report["fail_count"] == 0
        diagnostic = sample["retrieval_diagnostics"]
        route = diagnostic["preference_route"]
        assert route["entry"] == "no_match" and route["builtin_marker"] == ""
        assert route["intent_name"] == "generic" and route["intent_confidence"] == 0.0
        assert route["selected"] is route["filter_applied"] is False
        assert diagnostic["target_trace"]["preference_route"] == route
        assert diagnostic["target_trace"].get("preference_rejections", []) == []
        report["dataset_source"] = "controlled_records"
        receipt = _memory_eval_report_record(report, scope=SCOPE)
        runtime.store.append(receipt)
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": report}, [])
        assert diagnostics["memory_benchmark"]["case_results"][0]["passed"] is True
        assert diagnostics["memory_benchmark"].get("failed_case_refs", []) == []
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        saved_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(receipt.record_id, scope=SCOPE).content["report"] == report
        assert runtime.store.get_by_id(saved_id, scope=SCOPE).content["nightly_diagnostics"] == diagnostics
        assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
    assert target.title not in json.dumps(report)
    assert target.title not in json.dumps(diagnostics)


@pytest.mark.parametrize("text,passed", [("用户希望回复简短。", True), ("用户希望用英文回复。", False)])
def test_typed_preference_and_attribute_gate_keep_actual_policy(tmp_path, text, passed):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime, "我的回复长度是什么", text, "preference")
        report = controlled(runtime)
        sample = report["samples"][0]
        assert sample["passed"] is passed
        assert report["threshold"] == 1.0 and report["memory_benchmark_accepted"] is False
        route = sample["retrieval_diagnostics"]["preference_route"]
        assert route["entry"] == "typed_request" and route["typed_attribute"] == "length"
        trace = sample["retrieval_diagnostics"]["target_trace"]
        if passed:
            assert "preference_rejections" not in trace
        else:
            assert trace["preference_rejections"] == [
                {"branch": "preference_attribute", "reason": "requested_assertion_not_proven"}]


@pytest.mark.parametrize("forced", [False, True])
def test_real_runtime_overridden_route_bool_is_not_bypassed(tmp_path, monkeypatch, forced):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid notebook archive")
        calls = []
        monkeypatch.setattr(runtime.memory, "_is_preference_query", lambda *a, **kw: calls.append(True) or forced)
        monkeypatch.setattr(runtime.memory, "_preference_query_decision", lambda *a, **kw: pytest.fail("Override bypassed"))
        bundle = runtime.memory.recall(query=target.title, scope=asdict(SCOPE), task_context=CONTEXT, limit=5)
        assert calls == [True]
        assert (target.record_id in [item.record_id for item in bundle.items]) is (not forced)
        route = bundle.explanation["preference_route"]
        assert route["entry"] == "unobserved" and route["selected"] is forced
        assert route["intent_name"] == "not_reported"


@pytest.mark.parametrize("kind_bypass,report_bypass", [(False, False), (True, False), (False, True), (True, True)])
def test_candidate_override_kind_short_circuit_and_report_order(tmp_path, monkeypatch, kind_bypass, report_bypass):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid notebook archive")
        calls = []
        monkeypatch.setattr(runtime.memory, "_is_preference_query", lambda *a, **kw: calls.append("query") or True)
        monkeypatch.setattr(runtime.memory, "_is_preference_recall_candidate", lambda *a, **kw: calls.append("candidate") or False)
        monkeypatch.setattr(runtime.memory, "_preference_candidate_decision", lambda *a, **kw: pytest.fail("Candidate override bypassed"))
        if report_bypass:
            monkeypatch.setattr(runtime.memory, "_is_report_query", lambda *a, **kw: True)
        context = {**CONTEXT, **({"kinds": ["memory"]} if kind_bypass else {})}
        req = CandidateRequest.create(query=target.title, scope=SCOPE, source_ids=["hermes"],
            target_source_id="hermes", limit=5, task_context=context)
        engine = runtime.memory.recall_engine
        ref = CandidateRef(target.record_id, ExactScope.from_scope(SCOPE), "hermes")
        with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids) as trace:
            bundle = runtime.memory.recall(query=target.title, scope=asdict(SCOPE), task_context=context, limit=5)
        assert calls == (["query"] if kind_bypass or report_bypass else ["query", "candidate"])
        route = bundle.explanation["preference_route"]
        assert route["report_bypassed"] is report_bypass
        assert route["filter_applied"] is (not report_bypass)
        if kind_bypass or report_bypass:
            assert "preference_rejections" not in trace.report()
        else:
            assert trace.report()["preference_rejections"] == [{"branch": "preference_query", "reason": "unobserved"}]


def test_subclasses_and_forwarding_proxy_keep_historical_bool_hook():
    class Derived(MemoryAPI):
        def _is_preference_query(self, *a, **kw):
            return False
    plain = object.__new__(MemoryAPI)
    derived = object.__new__(Derived)
    class Proxy:
        def __getattr__(self, name):
            return getattr(plain, name)
    proxy = Proxy()
    assert _native_preference_bool(plain, plain._is_preference_query, "_preference_query_bool_implementation")
    assert not _native_preference_bool(derived, derived._is_preference_query, "_preference_query_bool_implementation")
    assert not _native_preference_bool(proxy, proxy._is_preference_query, "_preference_query_bool_implementation")


def test_actual_engine_preserves_subclass_bool_overrides(tmp_path, monkeypatch):
    calls = []
    class Derived(MemoryAPI):
        def _is_preference_query(self, *a, **kw):
            calls.append("query")
            return True
        def _is_preference_recall_candidate(self, *a, **kw):
            calls.append("candidate")
            return False
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid notebook archive")
        engine = GovernedRecallEngine(store=runtime.store, candidate_source=NativeCandidates(runtime.store, [target]))
        runtime.memory = Derived(runtime.store, recall_engine=engine)
        for helper in ("_preference_query_decision", "_preference_candidate_decision"):
            monkeypatch.setattr(runtime.memory, helper, lambda *a, **kw: pytest.fail("Subclass bool override bypassed"))
        report = controlled(runtime)
        diagnostic = report["samples"][0]["retrieval_diagnostics"]
        assert calls == ["query", "candidate"]
        assert diagnostic["preference_route"]["entry"] == "unobserved"
        assert diagnostic["target_trace"]["preference_rejections"] == [
            {"branch": "preference_query", "reason": "unobserved"}]
        assert report["pass_count"] == 0


def test_class_monkeypatch_keeps_original_bool_override_and_call_count(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid preference cache layout")
        calls = []
        monkeypatch.setattr(MemoryAPI, "_is_preference_query", lambda self, *a, **kw: calls.append(True) or False)
        monkeypatch.setattr(runtime.memory, "_preference_query_decision", lambda *a, **kw: pytest.fail("Class override bypassed"))
        bundle = runtime.memory.recall(query=target.title, scope=asdict(SCOPE), task_context=CONTEXT, limit=5)
        assert calls == [True]
        assert target.record_id in [item.record_id for item in bundle.items]
        assert bundle.explanation["preference_route"]["entry"] == "unobserved"


def test_call_through_override_is_called_once_without_claiming_native_decision(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid preference cache layout")
        original = runtime.memory._is_preference_query
        calls = []
        def override(*args, **kwargs):
            calls.append(True)
            return original(*args, **kwargs)
        monkeypatch.setattr(runtime.memory, "_is_preference_query", override)
        bundle = runtime.memory.recall(query=target.title, scope=asdict(SCOPE), task_context=CONTEXT, limit=5)
        assert calls == [True]
        assert [item.record_id for item in bundle.items] == [target.record_id]
        assert bundle.explanation["preference_route"]["entry"] == "unobserved"


def test_candidate_hook_custom_truth_value_is_evaluated_once(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid notebook archive")
        truth_checks = []
        class OneCheck:
            def __bool__(self):
                truth_checks.append(True)
                return len(truth_checks) > 1
        monkeypatch.setattr(runtime.memory, "_is_preference_query", lambda *a, **kw: True)
        monkeypatch.setattr(runtime.memory, "_is_preference_recall_candidate", lambda *a, **kw: OneCheck())
        bundle = runtime.memory.recall(query=target.title, scope=asdict(SCOPE), task_context=CONTEXT, limit=5)
        assert truth_checks == [True]
        assert bundle.items == []


def test_native_decisions_are_evaluated_once_with_or_without_target_observation(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid preference cache layout")
        calls = []
        route = runtime.memory._preference_query_decision
        candidate = runtime.memory._preference_candidate_decision
        def route_once(*a, **kw):
            calls.append("route")
            return route(*a, **kw)
        def candidate_once(*a, **kw):
            calls.append("candidate")
            return candidate(*a, **kw)
        monkeypatch.setattr(runtime.memory, "_preference_query_decision", route_once)
        monkeypatch.setattr(runtime.memory, "_preference_candidate_decision", candidate_once)
        plain = runtime.memory.recall(query=target.title, scope=asdict(SCOPE), task_context=CONTEXT, limit=5)
        assert calls == ["route"]
        calls.clear()
        report = controlled(runtime)
        assert calls == ["route"]
        assert [item.record_id for item in plain.items] == [target.record_id]
        assert report["pass_count"] == 1


def test_custom_marker_value_is_not_stored_in_either_controlled_projection(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime, "Orchid notebook archive")
        native = runtime.memory.recall
        def configured_recall(**kwargs):
            return native(**{**kwargs, "task_context": {**kwargs["task_context"],
                "preference_query_markers": [PRIVATE], "goal": PRIVATE}})
        monkeypatch.setattr(runtime.memory, "recall", configured_recall)
        report = controlled(runtime)
        diagnostic = report["samples"][0]["retrieval_diagnostics"]
        assert diagnostic["preference_route"]["entry"] == "custom_marker"
        assert diagnostic["preference_route"]["custom_marker_matched"] is True
        assert PRIVATE not in json.dumps(report)
        report["dataset_source"] = "controlled_records"
        summary = nightly_result_diagnostics({"memory_eval_ci": report}, [])
        assert PRIVATE not in json.dumps(summary)


def test_adapter_self_report_cannot_supply_native_preference_provenance(tmp_path):
    claim = {"schema": PREFERENCE_ROUTE_SCHEMA, "entry": "typed_request", "selected": True,
             "filter_applied": True, "typed_mode": "response_style", "typed_attribute": "length"}
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime, "Orchid notebook archive")
        runtime.memory = SimpleNamespace(recall=lambda **kwargs: RecallBundle(items=[], rules=[], reflections=[],
            confidence=0, next_action_hint="", explanation={"preference_route": claim,
                "target_trace": {"schema": "controlled_target_stage_trace.v1", "status": "completed",
                                  "preference_route": claim, "stages": []}}))
        report = controlled(runtime)
        diagnostic = report["samples"][0]["retrieval_diagnostics"]
        assert diagnostic["target_trace"]["status"] == "not_observed"
        assert "preference_route" not in diagnostic
        assert "preference_route" not in diagnostic["target_trace"]
        # The summary also derives route details from the captured target
        # trace rather than trusting a standalone adapter-provided route.
        diagnostic["preference_route"] = claim
        report["dataset_source"] = "controlled_records"
        detail = nightly_result_diagnostics({"memory_eval_ci": report}, [])["memory_benchmark"]["failed_case_refs"][0]
        assert "preference_route" not in detail["retrieval_diagnostics"]


def test_route_and_target_rejection_are_isolated_from_nested_and_inherited_contexts():
    engine = type("Engine", (), {"store": object()})()
    ref = CandidateRef("mem_target", ExactScope.from_scope(SCOPE), "hermes")
    other = CandidateRef("mem_other", ref.scope, ref.source_id)
    req = CandidateRequest.create(query="Synthetic query", scope=SCOPE, source_ids=["hermes"],
        target_source_id="hermes", limit=5, task_context={"exact_scope_only": True})
    route = {"schema": PREFERENCE_ROUTE_SCHEMA, "entry": "default_marker", "selected": True}
    with target_trace_scope(engine, ref, query=req.query, source_ids=req.source_ids) as trace:
        trace.observe_preference_route(route)
        assert "preference_route" not in trace.report()
        with native_target_trace_scope(engine, req):
            trace.observe_preference_route(route)
            trace.observe_preference_rejection(other, "preference_query", "not_memory")
            expected = deepcopy(trace.report())
            def contaminate():
                trace.observe_preference_route({**route, "entry": "custom_marker"})
                trace.observe_preference_rejection(ref, "preference_query", "not_memory")
            with native_target_trace_scope(object(), req):
                contaminate()
            with ThreadPoolExecutor(max_workers=1) as executor:
                executor.submit(copy_context().run, contaminate).result()
            async def inherited_task():
                contaminate()
            asyncio.run(inherited_task())
            assert trace.report() == expected
            trace.observe_preference_rejection(ref, "preference_query", "no_explicit_preference")
        assert trace.report()["preference_rejections"] == [
            {"branch": "preference_query", "reason": "no_explicit_preference"}]


@pytest.mark.parametrize("confidence", [None, True, float("nan"), float("inf"), -0.1, 1.1])
def test_safe_projection_is_idempotent_private_and_keeps_unknowns(confidence):
    raw = {"schema": PREFERENCE_ROUTE_SCHEMA, "entry": PRIVATE, "selected": PRIVATE,
        "filter_applied": True, "builtin_marker": PRIVATE, "custom_marker": PRIVATE,
        "typed_mode": PRIVATE, "typed_attribute": PRIVATE, "query": PRIVATE,
        "intent_name": PRIVATE, "intent_confidence": confidence, "intent_terms": [PRIVATE]}
    safe = safe_preference_route(raw)
    assert safe["intent_confidence"] is None and safe["selected"] is None
    assert safe_preference_route(safe) == safe
    assert PRIVATE not in json.dumps(safe)
    rejected = safe_preference_rejections([{"branch": "preference_query", "reason": PRIVATE, "query": PRIVATE}] * 4)
    assert len(rejected) == 2 and PRIVATE not in json.dumps(rejected)
    legacy = nightly_result_diagnostics({"memory_eval_ci": {"dataset_source": "controlled_records",
        "samples": [{"passed": False, "retrieval_diagnostics": {}}]}}, [])["memory_benchmark"]
    detail = legacy["failed_case_refs"][0]["retrieval_diagnostics"]
    assert "preference_route" not in detail
