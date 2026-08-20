from __future__ import annotations

from typing import Any

from eimemory.governance.capability_ledger import (
    _require_compact_capability_scores,
    record_capability_score,
)
from eimemory.models.records import ScopeRef


LEGACY_SEEDED_CAPABILITIES: tuple[str, ...] = (
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
)


def ensure_all_seeded(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "seed",
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    """Optionally materialize the retired fixed capability-score cohort.

    L5 v3 learns its capability universe from the registry/profile projection;
    writing zero-score records for a historical Python list would otherwise make
    those names appear to be live capabilities.  The compatibility cohort stays
    available only to explicit v2 migration/replay callers.
    """
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not legacy_compatibility:
        return {
            "ok": True,
            "status": "not_applicable",
            "legacy_compatibility": False,
            "seeded_capabilities": [],
            "created_count": 0,
            "created_record_ids": [],
            "reason": "dynamic_registry_authority",
        }
    records = _require_compact_capability_scores(runtime, scope=scope_ref, limit=1000)
    existing = {
        str(record.meta.get("capability") or record.content.get("capability") or "")
        for record in records
    }
    created: list[str] = []
    for capability in LEGACY_SEEDED_CAPABILITIES:
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
        "legacy_compatibility": True,
        "seeded_capabilities": list(LEGACY_SEEDED_CAPABILITIES),
        "created_count": len(created),
        "created_record_ids": created,
    }
