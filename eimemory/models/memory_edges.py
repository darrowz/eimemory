from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef


MEMORY_EDGE_TYPES = frozenset({"semantic", "temporal", "causal", "entity"})


@dataclass(slots=True)
class MemoryEdge:
    edge_id: str
    from_id: str
    to_id: str
    edge_type: str
    confidence: float
    evidence_id: str
    scope: ScopeRef
    reason: str = ""
    created_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        from_id: str,
        to_id: str,
        edge_type: str,
        confidence: float,
        evidence_id: str = "",
        scope: ScopeRef,
        reason: str = "",
        meta: dict[str, Any] | None = None,
    ) -> "MemoryEdge":
        if edge_type not in MEMORY_EDGE_TYPES:
            raise ValueError(f"invalid memory edge type: {edge_type}")
        edge_id = stable_memory_edge_id(
            scope=scope,
            from_id=from_id,
            to_id=to_id,
            edge_type=edge_type,
            evidence_id=evidence_id,
        )
        now = now_iso()
        return cls(
            edge_id=edge_id,
            from_id=str(from_id),
            to_id=str(to_id),
            edge_type=edge_type,
            confidence=round(max(0.0, min(1.0, float(confidence or 0.0))), 3),
            evidence_id=str(evidence_id or ""),
            scope=scope,
            reason=str(reason or ""),
            created_at=now,
            updated_at=now,
            meta=dict(meta or {}),
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemoryEdge":
        return cls(
            edge_id=str(payload.get("edge_id") or ""),
            from_id=str(payload.get("from_id") or ""),
            to_id=str(payload.get("to_id") or ""),
            edge_type=str(payload.get("edge_type") or ""),
            confidence=round(max(0.0, min(1.0, float(payload.get("confidence") or 0.0))), 3),
            evidence_id=str(payload.get("evidence_id") or ""),
            scope=ScopeRef.from_dict(payload.get("scope") if isinstance(payload.get("scope"), dict) else {}),
            reason=str(payload.get("reason") or ""),
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            meta=dict(payload.get("meta") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scope"] = asdict(self.scope)
        payload["meta"] = dict(self.meta or {})
        return payload


def stable_memory_edge_id(
    *,
    scope: ScopeRef,
    from_id: str,
    to_id: str,
    edge_type: str,
    evidence_id: str = "",
) -> str:
    raw = "\x1f".join(
        [
            scope.tenant_id or "default",
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            str(edge_type),
            str(from_id),
            str(to_id),
            str(evidence_id or ""),
        ]
    )
    return f"edge_{sha256(raw.encode('utf-8')).hexdigest()[:24]}"
