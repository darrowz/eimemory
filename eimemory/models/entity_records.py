from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.paper_sources import _deep_freeze, _deep_thaw
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef


@dataclass(slots=True, frozen=True)
class EntityRecord:
    entity_record_id: str
    paper_source_id: str
    name: str
    entity_type: str = "concept"
    aliases: tuple[str, ...] = field(default_factory=tuple)
    salience: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "aliases", tuple(str(alias) for alias in self.aliases if str(alias).strip()))
        object.__setattr__(self, "salience", max(0.0, min(1.0, float(self.salience))))
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata or {})))
        provenance = {**dict(self.provenance or {}), "paper_source_id": self.paper_source_id}
        object.__setattr__(self, "provenance", _deep_freeze(provenance))

    def to_payload(self) -> dict[str, Any]:
        return {
            "entity_record_id": self.entity_record_id,
            "paper_source_id": self.paper_source_id,
            "name": self.name,
            "entity_type": self.entity_type,
            "aliases": list(self.aliases),
            "salience": self.salience,
            "metadata": _deep_thaw(self.metadata),
            "provenance": _deep_thaw(self.provenance),
        }

    def to_record(self, *, scope: ScopeRef, source: str = "eimemory.knowledge.relations") -> RecordEnvelope:
        ts = now_iso()
        return RecordEnvelope(
            record_id=self.entity_record_id,
            kind="entity_record",
            status="active",
            title=self.name or self.entity_record_id,
            summary=f"{self.entity_type}: {self.name}",
            detail=", ".join(self.aliases),
            content=self.to_payload(),
            tags=["paper", "entity", self.entity_type],
            links=[LinkRef(relation="mentioned_in", target_kind="paper_source", target_id=self.paper_source_id)],
            evidence=[self.paper_source_id],
            source=source,
            scope=scope,
            time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
            provenance=_deep_thaw(self.provenance),
            meta={
                "paper_source_id": self.paper_source_id,
                "entity_type": self.entity_type,
                "salience": self.salience,
            },
        )
