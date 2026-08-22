"""Trusted production capability catalog for the Hongtu embodied runtime.

The bootstrap is installed as package metadata so normal Runtime owners load a
sealed, code-backed evaluation catalog.  Executors emit aggregate evidence
only: recalled payloads, secrets, and executable inputs never cross the catalog
boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eimemory.evaluation.capability_catalog import CatalogCase

EXECUTOR_ID = "hongtu.eval.memory-recall"
EXECUTOR_REVISION = "v1"
CASE_ID = "hongtu_memory_recall_contract_v1"
CAPABILITY_ID = "memory.recall"
RUNTIME_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}


def _scope_dict(value: object) -> dict[str, str]:
    raw: Mapping[str, Any]
    if isinstance(value, Mapping):
        raw = value
    else:
        to_dict = getattr(value, "to_dict", None)
        serialized = to_dict() if callable(to_dict) else None
        if isinstance(serialized, Mapping):
            raw = serialized
        else:
            raw = {
                "tenant_id": getattr(value, "tenant_id", "default"),
                "agent_id": getattr(value, "agent_id", ""),
                "workspace_id": getattr(value, "workspace_id", ""),
                "user_id": getattr(value, "user_id", ""),
            }
    return {
        "tenant_id": str(raw.get("tenant_id") or "default"),
        "agent_id": str(raw.get("agent_id") or ""),
        "workspace_id": str(raw.get("workspace_id") or ""),
        "user_id": str(raw.get("user_id") or ""),
    }


def evaluate_memory_recall(
    input_data: dict[str, Any],
    fixture: dict[str, Any],
    runtime: Any,
) -> dict[str, Any]:
    """Exercise the real recall engine and emit bounded structural evidence."""

    query = str(input_data.get("query") or "").strip()
    expected_scope = _scope_dict(fixture.get("scope"))
    raw_limit = input_data.get("limit", 8)
    if not query:
        raise ValueError("query is required")
    if expected_scope != RUNTIME_SCOPE:
        raise ValueError("catalog fixture scope is not the Hongtu production scope")
    if isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
        raise ValueError("limit must be an integer")
    limit = max(1, min(8, raw_limit))

    memory = getattr(runtime, "memory", None)
    recall = getattr(memory, "recall", None)
    if not callable(recall):
        raise RuntimeError("runtime memory recall is unavailable")

    bundle = recall(
        query=query,
        scope=dict(expected_scope),
        task_context={
            "task_type": "catalog.memory_recall_probe",
            "recall_profile": "precision",
            "catalog_case_id": CASE_ID,
        },
        limit=limit,
    )
    lanes = (
        tuple(getattr(bundle, "items", ()) or ()),
        tuple(getattr(bundle, "rules", ()) or ()),
        tuple(getattr(bundle, "reflections", ()) or ()),
    )
    records = tuple(record for lane in lanes for record in lane)
    confidence = float(getattr(bundle, "confidence", 0.0) or 0.0)
    lane_counts = tuple(len(lane) for lane in lanes)
    scope_isolated = all(_scope_dict(getattr(record, "scope", None)) == expected_scope for record in records)

    return {
        "execution_ok": True,
        "result_count": len(records),
        "max_lane_count": max(lane_counts, default=0),
        "scope_isolated": scope_isolated,
        "confidence_bounded": 0.0 <= confidence <= 1.0,
        "payload_redacted": True,
    }


def install(bootstrap: Any) -> None:
    """Register the trusted production catalog through the sealed bootstrap."""

    registration = bootstrap.register_executor(
        executor_id=EXECUTOR_ID,
        revision=EXECUTOR_REVISION,
        handler=evaluate_memory_recall,
        contract_descriptor={
            "operation": "memory.recall",
            "evidence": "aggregate-only",
            "scope": "hongtu/embodied/darrow",
            "side_effect_class": "none",
        },
    )
    bootstrap.register_case(
        CatalogCase(
            case_id=CASE_ID,
            capability_id=CAPABILITY_ID,
            executor_id=EXECUTOR_ID,
            executor_revision=EXECUTOR_REVISION,
            executor_contract_digest=registration.contract_digest,
            input_data={"query": "eimemory Hermes 商务助理", "limit": 8},
            fixture={"scope": dict(RUNTIME_SCOPE)},
            expected_invariants=[
                {"field": "execution_ok", "op": "eq", "value": True},
                {"field": "result_count", "op": "min", "value": 1},
                {"field": "result_count", "op": "max", "value": 24},
                {"field": "max_lane_count", "op": "max", "value": 8},
                {"field": "scope_isolated", "op": "eq", "value": True},
                {"field": "confidence_bounded", "op": "eq", "value": True},
                {"field": "payload_redacted", "op": "eq", "value": True},
            ],
            resource_budget={
                "timeout_seconds": 30,
                "max_memory_mb": 256,
                "max_artifact_bytes": 262_144,
            },
        )
    )


__all__ = [
    "CAPABILITY_ID",
    "CASE_ID",
    "EXECUTOR_ID",
    "EXECUTOR_REVISION",
    "RUNTIME_SCOPE",
    "evaluate_memory_recall",
    "install",
]
