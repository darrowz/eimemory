from __future__ import annotations

from typing import Any

from eimemory.governance.capability_ledger import record_capability_score
from eimemory.models.records import ScopeRef


SEEDED_CAPABILITIES: list[str] = [
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "code.implementation",
    "operations.uumit",
    "office.daily_task",
    "device.control",
    "research.synthesis",
    "safety.boundary",
]


def ensure_all_seeded(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "seed",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    existing = {
        str(record.meta.get("capability") or record.content.get("capability") or "")
        for record in runtime.store.list_records(kinds=["capability_score"], scope=scope_ref, limit=1000)
    }
    created: list[str] = []
    for capability in SEEDED_CAPABILITIES:
        if capability in existing:
            continue
        created.append(
            record_capability_score(
                runtime,
                scope=scope_ref,
                loop_id=loop_id,
                capability=capability,
                score=0.0,
                evidence_record_ids=[],
            )
        )
    return {
        "ok": True,
        "seeded_capabilities": list(SEEDED_CAPABILITIES),
        "created_count": len(created),
        "created_record_ids": created,
    }
