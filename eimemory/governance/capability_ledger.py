from __future__ import annotations

from statistics import mean
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef

SEEDED_LEDGER_CAPABILITIES = [
    "search.discovery",
    "code.implementation",
    "operations.uumit",
    "office.daily_task",
    "device.control",
    "research.synthesis",
    "proactive.judgment",
    "safety.boundary",
]


def record_capability_score(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    capability: str,
    score: float,
    evidence_record_ids: list[str] | None = None,
    regression_count: int = 0,
) -> str:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    existing = [
        record
        for record in runtime.store.list_records(kinds=["capability_score"], scope=scope_ref, limit=500)
        if str(record.meta.get("capability") or "") == capability
    ]
    sequence = len(existing) + 1
    semantic_key = stable_semantic_key("capability_score", capability, loop_id, score, evidence_record_ids or [])
    record = append_learning_record_once(
        runtime,
        kind="capability_score",
        title=f"Capability score: {capability}",
        summary=f"{capability} score {round(float(score), 3)}",
        scope=scope_ref,
        loop_id=loop_id,
        step_name="ledger",
        semantic_key=semantic_key,
        authority_tier="L0",
        status="active",
        content={"capability": capability, "score": round(float(score), 3), "evidence_record_ids": list(evidence_record_ids or []), "regression_count": regression_count, "score_sequence": sequence},
        meta={"capability": capability, "score": round(float(score), 3), "score_sequence": sequence, "evidence_count": len(evidence_record_ids or []), "regression_count": regression_count},
    )
    return record.record_id


def build_capability_ledger(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 500,
    ensure_seeded: bool = False,
) -> dict[str, Any]:
    if ensure_seeded:
        from eimemory.governance.capability_seeding import ensure_all_seeded

        ensure_all_seeded(runtime, scope=scope, loop_id="seed")

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    records = runtime.store.list_records(kinds=["capability_score"], scope=scope_ref, limit=limit)
    by_capability: dict[str, list[RecordEnvelope]] = {}
    for record in records:
        by_capability.setdefault(str(record.meta.get("capability") or "general"), []).append(record)
    capabilities = {}
    for capability, items in by_capability.items():
        ordered = sorted(
            items,
            key=lambda item: (
                int(item.meta.get("score_sequence") or item.content.get("score_sequence") or 0),
                item.time.updated_at,
                item.time.created_at,
            ),
            reverse=True,
        )
        scores = [float(item.meta.get("score") or 0.0) for item in ordered]
        capabilities[capability] = {
            "score": scores[0] if scores else 0.0,
            "average": round(mean(scores), 3) if scores else 0.0,
            "trend": round(scores[0] - scores[-1], 3) if len(scores) >= 2 else 0.0,
            "evidence_count": sum(int(item.meta.get("evidence_count") or 0) for item in ordered),
            "regression_count": sum(int(item.meta.get("regression_count") or 0) for item in ordered),
            "last_record_id": ordered[0].record_id if ordered else "",
        }
    for capability in SEEDED_LEDGER_CAPABILITIES:
        capabilities.setdefault(
            capability,
            {
                "score": 0.0,
                "average": 0.0,
                "trend": 0.0,
                "evidence_count": 0,
                "regression_count": 0,
                "last_record_id": "",
            },
        )
    return {"ok": True, "capabilities": capabilities, "record_count": len(records)}
