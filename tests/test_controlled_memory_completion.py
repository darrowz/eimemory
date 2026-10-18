"""A known-item hit cannot conceal explicitly incomplete controlled retrieval."""
from copy import deepcopy

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecallBundle
from test_controlled_memory_benchmark import SCOPE, _build, _memory, _run


def _complete_explanation():
    return {
        "retrieval_status": "evidence_found",
        "engine_diagnostics": {
            "source_searches": 1,
            "source_budget_exhausted": 0,
            "fallback": False,
            "drops": {},
        },
        "relevance_selector": {
            "status": "evidence_found",
            "collection_complete": True,
        },
    }


def _run_returned_target(runtime, target, dataset, explanation, monkeypatch):
    """Keep real source storage/revalidation and substitute only the adapter result."""
    calls = []

    def recall(**kwargs):
        calls.append(kwargs)
        return RecallBundle(
            items=[deepcopy(target)], rules=[], reflections=[], confidence=1.0,
            next_action_hint="", explanation=deepcopy(explanation),
        )

    monkeypatch.setattr(runtime.memory, "recall", recall)
    report = _run(runtime, dataset)
    assert len(calls) == 1
    assert calls[0]["scope"] == {
        "tenant_id": SCOPE.tenant_id, "agent_id": SCOPE.agent_id,
        "workspace_id": SCOPE.workspace_id, "user_id": SCOPE.user_id,
    }
    assert calls[0]["task_context"]["source_ids"] == ["hermes"]
    assert report["samples"][0]["returned_refs"][0]["record_id"] == target.record_id
    # These are controlled diagnostics even when their known-item check passes.
    assert report["formal_business_evidence"] is False
    assert report["memory_benchmark_accepted"] is False
    assert report["certifies_natural_queries"] is False
    assert report["certifies_no_answer"] is False
    return report


@pytest.mark.parametrize("damage", [
    "collection_incomplete", "source_budget_exhausted", "recall_budget_exhausted",
    "selector_degraded", "retrieval_degraded",
])
def test_final_target_hit_cannot_override_explicit_incompleteness(tmp_path, monkeypatch, damage):
    with Runtime.create(root=tmp_path) as runtime:
        target = _memory(runtime)
        dataset = _build(runtime)
        explanation = _complete_explanation()
        if damage == "collection_incomplete":
            explanation["relevance_selector"]["collection_complete"] = False
        elif damage == "source_budget_exhausted":
            explanation["engine_diagnostics"]["source_budget_exhausted"] = 2
        elif damage == "recall_budget_exhausted":
            explanation["engine_diagnostics"]["drops"]["recall_budget_exhausted"] = 1
        elif damage == "selector_degraded":
            explanation["relevance_selector"]["status"] = "degraded"
        else:
            explanation["retrieval_status"] = "degraded"
        report = _run_returned_target(runtime, target, dataset, explanation, monkeypatch)
        sample = report["samples"][0]
        assert sample["failure_reason"] == "controlled_retrieval_incomplete"
        assert sample["passed"] is False and sample["evaluable"] is False
        assert report["pass_count"] == 0
        assert report["controlled_benchmark_passed_threshold"] is False
        assert report["passed_threshold"] is False
        assert report["ok"] is report["execution_ok"] is False


@pytest.mark.parametrize("budget", ["source", "recall"])
@pytest.mark.parametrize("value", [None, False, True, "0", -1, 0.0])
def test_explicit_budget_values_require_integer_zero(tmp_path, monkeypatch, budget, value):
    with Runtime.create(root=tmp_path) as runtime:
        target = _memory(runtime)
        dataset = _build(runtime)
        explanation = _complete_explanation()
        if budget == "source":
            explanation["engine_diagnostics"]["source_budget_exhausted"] = value
        else:
            explanation["engine_diagnostics"]["drops"]["recall_budget_exhausted"] = value
        report = _run_returned_target(runtime, target, dataset, explanation, monkeypatch)
        assert report["samples"][0]["failure_reason"] == "controlled_retrieval_incomplete"
        assert report["pass_count"] == 0
        assert report["controlled_benchmark_passed_threshold"] is False
        assert report["ok"] is report["execution_ok"] is False


def test_complete_fallback_remains_a_valid_known_item_diagnostic(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = _memory(runtime)
        dataset = _build(runtime)
        explanation = _complete_explanation()
        explanation["engine_diagnostics"]["fallback"] = True
        report = _run_returned_target(runtime, target, dataset, explanation, monkeypatch)
        sample = report["samples"][0]
        assert sample["passed"] is sample["evaluable"] is True
        assert sample["expected_rank"] == 1
        assert report["controlled_benchmark_passed_threshold"] is True
        assert report["ok"] is report["execution_ok"] is True
        availability = sample["retrieval_diagnostics"]["source_availability"]
        assert availability["status"] == "degraded"
        assert availability["collection_complete"] is True and availability["fallback"] is True


def test_legacy_adapter_without_diagnostics_keeps_known_item_behavior(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        target = _memory(runtime)
        dataset = _build(runtime)
        report = _run_returned_target(runtime, target, dataset, {}, monkeypatch)
        sample = report["samples"][0]
        assert sample["passed"] is sample["evaluable"] is True
        assert sample["expected_rank"] == 1
        assert report["controlled_benchmark_passed_threshold"] is True
        assert report["ok"] is report["execution_ok"] is True
        availability = sample["retrieval_diagnostics"]["source_availability"]
        assert availability["status"] == "unknown"
        assert availability["collection_complete"] is None
        assert availability["source_budget_exhausted"] is None
        assert availability["fallback"] is None
