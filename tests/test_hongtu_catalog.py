from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eimemory.evaluation.capability_catalog import (
    ApplicationCatalogBootstrap,
    CapabilityEvaluationCatalog,
)
from eimemory.evaluation.hongtu_catalog import (
    CASE_ID,
    RUNTIME_SCOPE,
    evaluate_memory_recall,
    install,
)
from eimemory.governance.capability_probe_executor import execute_probe
from eimemory.models.records import ScopeRef


@dataclass
class _Record:
    scope: Any


@dataclass
class _Bundle:
    items: list[_Record]
    rules: list[_Record]
    reflections: list[_Record]
    confidence: float


class _Memory:
    def __init__(self, *, scope: object | None = None) -> None:
        self.scope = scope or ScopeRef.from_dict(RUNTIME_SCOPE)
        self.calls: list[dict[str, Any]] = []

    def recall(self, **kwargs: Any) -> _Bundle:
        self.calls.append(kwargs)
        return _Bundle(items=[_Record(self.scope)], rules=[], reflections=[], confidence=0.81)


class _Runtime:
    def __init__(self, *, scope: object | None = None) -> None:
        self.memory = _Memory(scope=scope)


def _catalog() -> CapabilityEvaluationCatalog:
    catalog = CapabilityEvaluationCatalog()
    install(ApplicationCatalogBootstrap(catalog))
    return catalog.seal()


def test_hongtu_recall_executor_uses_runtime_and_redacts_payloads() -> None:
    runtime = _Runtime()

    result = evaluate_memory_recall(
        {"query": "eimemory", "limit": 8},
        {"scope": dict(RUNTIME_SCOPE)},
        runtime,
    )

    assert result == {
        "execution_ok": True,
        "result_count": 1,
        "max_lane_count": 1,
        "scope_isolated": True,
        "confidence_bounded": True,
        "payload_redacted": True,
    }
    assert runtime.memory.calls[0]["scope"] == RUNTIME_SCOPE
    assert runtime.memory.calls[0]["task_context"]["task_type"] == "catalog.memory_recall_probe"
    assert "items" not in result


def test_hongtu_recall_executor_rejects_cross_scope_results() -> None:
    runtime = _Runtime(scope=ScopeRef.from_dict({**RUNTIME_SCOPE, "user_id": "other"}))

    result = evaluate_memory_recall(
        {"query": "eimemory", "limit": 8},
        {"scope": dict(RUNTIME_SCOPE)},
        runtime,
    )

    assert result["scope_isolated"] is False


def test_hongtu_catalog_case_is_sealed_and_passes() -> None:
    catalog = _catalog()
    case = catalog.get_case(CASE_ID)

    assert catalog.sealed is True
    assert case is not None
    result = catalog.execute(case.to_artifact(), runtime=_Runtime(), evidence_ref="test://catalog")
    assert result["passed"] is True
    assert result["verdict"] == "pass"
    assert result["output"]["payload_redacted"] is True


def test_governed_probe_preserves_pass_verdict_for_persistence() -> None:
    catalog = _catalog()
    case = catalog.get_case(CASE_ID)
    assert case is not None

    result = execute_probe(
        case.to_artifact(),
        runtime=_Runtime(),
        evidence_ref="test://governed-probe",
        catalog=catalog,
    )

    assert result["passed"] is True
    assert result["verdict"] == "pass"


def test_catalog_evaluation_spec_is_stable_across_repeated_runs() -> None:
    catalog = _catalog()
    case = catalog.get_case(CASE_ID)
    assert case is not None

    first = case.to_evaluation_spec(
        capability_revision_id="memory.recall:v1",
        capability_scope="global",
    )
    second = case.to_evaluation_spec(
        capability_revision_id="memory.recall:v1",
        capability_scope="global",
    )

    assert first.eval_spec_id == second.eval_spec_id
    assert first.spec_digest == second.spec_digest
    assert first.created_at == second.created_at
