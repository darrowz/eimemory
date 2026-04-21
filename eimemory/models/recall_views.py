from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.paper_sources import _deep_freeze, _deep_thaw
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


@dataclass(slots=True, frozen=True)
class RecallView:
    view_type: str
    items: tuple[dict[str, Any], ...]
    guidance: str = "Organize recalled memory for the consumer; do not make decisions or control workflows."
    query: str = ""
    generated_at: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(_deep_freeze(dict(item)) for item in self.items))
        object.__setattr__(self, "generated_at", self.generated_at or now_iso())
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata or {})))

    def to_dict(self) -> dict[str, Any]:
        return {
            "view_type": self.view_type,
            "items": [_deep_thaw(item) for item in self.items],
            "guidance": self.guidance,
            "query": self.query,
            "generated_at": self.generated_at,
            "metadata": _deep_thaw(self.metadata),
        }

    def to_record(self, *, scope: ScopeRef, title: str = "Recall view") -> RecordEnvelope:
        return RecordEnvelope.create(
            kind="recall_view",
            title=title,
            summary=f"{self.view_type} recall view",
            detail=self.guidance,
            content=self.to_dict(),
            tags=["recall_view", self.view_type],
            scope=scope,
            source="eimemory.knowledge.views",
            meta={"view_type": self.view_type},
        )
