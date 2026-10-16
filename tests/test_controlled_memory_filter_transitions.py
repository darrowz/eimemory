"""Target filter causes come from actual list transitions, never other drops."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.governance.learning.supervisor import (
    build_supervisor_contract, persist_supervisor_summary, supervisor_summary,
)
from eimemory.retrieval.contracts import CandidateRequest
from eimemory.retrieval.target_trace import (
    FILTER_DROP_REASONS, MAX_INSPECTED_REFS, target_trace_scope,
)
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_controlled_memory_target_trace import (
    NativeCandidates, SCOPE, install, memory, stages, target_ref,
)


@pytest.fixture(autouse=True)
def prohibit_models(monkeypatch):
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    monkeypatch.setenv("EIMEMORY_RELEVANCE_ADMISSION_ENABLED", "0")
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete",
                        lambda *args, **kwargs: pytest.fail("Model call prohibited"))


def _request(target, query, context):
    return CandidateRequest.create(query=query, scope=SCOPE, source_ids=["hermes"], target_source_id="hermes",
        limit=5, task_context={"exact_scope_only": True, **context})


@pytest.mark.parametrize("branch", ["default_suppression", "task_evidence", "preference_query",
                                    "recall_view", "content_dedupe", "hard_filter", "online_gate"])
def test_native_target_loss_records_only_the_actual_filter_branch(tmp_path, branch):
    with Runtime.create(root=tmp_path) as runtime:
        meta = ({"memory_type": "run_log"} if branch == "default_suppression" else
                {"memory_type": "external_knowledge"} if branch == "online_gate" else
                {"quality": {"salience_score": 0.1}} if branch == "content_dedupe" else {})
        target = memory(runtime, "Orchid project green notebook archive", meta=meta)
        candidates = [target]
        if branch == "content_dedupe":
            candidates.append(memory(runtime, target.title, index=1, meta={"quality": {"salience_score": 0.9}}))
        source = NativeCandidates(runtime.store, candidates)
        engine = install(runtime, source)
        context = ({"recall_view": "contradiction"} if branch == "recall_view" else
                   {"allowed_memory_types": ["preference"]} if branch == "hard_filter" else {})
        query = ("全部上次我授权了什么任务？" if branch == "task_evidence" else
                 "What are my personal preferences" if branch == "preference_query" else target.title)
        req = _request(target, query, context)
        plain = engine.recall(req)
        with target_trace_scope(engine, target_ref(target), query=req.query, source_ids=req.source_ids) as trace:
            measured = engine.recall(req)
        rows = stages(trace.report())
        assert rows["hydrated"]["target_presence"] == "present"
        assert rows["filtered"]["target_presence"] == "absent_in_observed_window"
        assert rows["filtered"]["drop_reasons"] == [branch]
        assert rows["selector_input"]["drop_reasons"] == []
        assert [item.to_dict() for item in plain.items] == [item.to_dict() for item in measured.items]
        assert plain.explanation["relevance_selector"] == measured.explanation["relevance_selector"]
        assert plain.explanation["fusion"] == measured.explanation["fusion"]
        assert source.calls == 2
        assert target.record_id not in [item.record_id for item in measured.items]
        assert SCOPE.user_id not in json.dumps(trace.report())


def test_unrelated_online_gate_drop_does_not_label_retained_target(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid green notebook archive")
        rejected = memory(runtime, "Synthetic unrelated archive", index=1, meta={"memory_type": "external_knowledge"})
        source = NativeCandidates(runtime.store, [target, rejected])
        engine = install(runtime, source)
        req = _request(target, target.title, {})
        with target_trace_scope(engine, target_ref(target), query=req.query, source_ids=req.source_ids) as trace:
            bundle = engine.recall(req)
        assert bundle.explanation["online_recall_gate"]["blocked_counts"]["external_knowledge_untrusted"] == 1
        rows = stages(trace.report())
        assert rows["filtered"]["target_presence"] == "present"
        assert rows["filtered"]["drop_reasons"] == []
        assert target.record_id in [item.record_id for item in bundle.items]


def test_observer_does_not_reexecute_filter_predicates(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Orchid notebook archive")
        engine = install(runtime, NativeCandidates(runtime.store, [target]))
        calls = []
        predicate = runtime.memory._online_recall_pollution_reason
        def observed(*args, **kwargs):
            calls.append(args[0].record_id)
            return predicate(*args, **kwargs)
        monkeypatch.setattr(runtime.memory, "_online_recall_pollution_reason", observed)
        req = _request(target, target.title, {})
        engine.recall(req)
        plain_calls = list(calls)
        calls.clear()
        with target_trace_scope(engine, target_ref(target), query=req.query, source_ids=req.source_ids):
            engine.recall(req)
        assert calls == plain_calls


def test_branch_causes_survive_controlled_and_durable_summary_without_source_content(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, "Synthetic untrusted knowledge archive", meta={"memory_type": "external_knowledge"})
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        before = deepcopy(dataset)
        install(runtime, NativeCandidates(runtime.store, [target]))
        report = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])
        assert dataset == before
        assert report["samples"][0]["failure_reason"] == "known_item_missing"
        assert report["pass_count"] == 0 and report["fail_count"] == 1
        report["dataset_source"] = "controlled_records"
        diagnostics = nightly_result_diagnostics({"memory_eval_ci": report}, [])
        failure = diagnostics["memory_benchmark"]["failed_case_refs"][0]
        assert stages(failure["retrieval_diagnostics"]["target_trace"])["filtered"]["drop_reasons"] == ["online_gate"]
        assert diagnostics["memory_benchmark_accepted"] is False
        summary = supervisor_summary(command="nightly", ok=True, duration_ms=1, memory_peak=0)
        summary["nightly_diagnostics"] = diagnostics
        saved_id = persist_supervisor_summary(runtime, scope=SCOPE, summary=summary).record_id
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.store.get_by_id(saved_id, scope=SCOPE).content["nightly_diagnostics"] == diagnostics
        assert build_supervisor_contract(runtime, scope=SCOPE)["runs"]["nightly"]["nightly_diagnostics"] == diagnostics
    assert target.title not in json.dumps(diagnostics)
    assert SCOPE.user_id not in json.dumps(diagnostics)


def test_transition_observation_ignores_incomplete_unknown_and_other_target_windows():
    engine = type("Engine", (), {"store": object()})()
    from eimemory.retrieval.contracts import CandidateRef, ExactScope
    target = CandidateRef("mem_target", ExactScope.from_scope(SCOPE), "hermes")
    other = CandidateRef("mem_other", target.scope, target.source_id)
    with target_trace_scope(engine, target, query="Synthetic query", source_ids=["hermes"]) as trace:
        consumed = []
        def unconsumed():
            consumed.append(True)
            yield target
        trace.filter_transition(unconsumed(), [], "online_gate")
        trace.filter_transition([target], unconsumed(), "online_gate")
        trace.filter_transition([target] * (MAX_INSPECTED_REFS + 1), [], "online_gate")
        trace.filter_transition([target], [other] * (MAX_INSPECTED_REFS + 1), "online_gate")
        trace.filter_transition([other], [], "online_gate")
        trace.filter_transition([target], [target], "online_gate")
        trace.filter_transition([target], [], "private unknown branch")
        context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(context.run, trace.filter_transition, [target], [], "online_gate").result()
        assert consumed == []
        assert stages(trace.report())["filtered"]["target_presence"] == "unknown"
        assert stages(trace.report())["filtered"]["drop_reasons"] == []
        for branch in FILTER_DROP_REASONS:
            trace.filter_transition([target], [], branch)
        assert stages(trace.report())["filtered"]["drop_reasons"] == sorted(FILTER_DROP_REASONS)
