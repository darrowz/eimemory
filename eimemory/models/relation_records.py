from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.paper_sources import _deep_freeze, _deep_thaw
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef


@dataclass(slots=True, frozen=True)
class RelationRecord:
    relation_record_id: str
    paper_source_id: str
    subject_id: str
    object_id: str
    relation_type: str
    evidence_text: str = ""
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata or {})))
        provenance = {**dict(self.provenance or {}), "paper_source_id": self.paper_source_id}
        object.__setattr__(self, "provenance", _deep_freeze(provenance))

    def to_payload(self) -> dict[str, Any]:
        return {
            "relation_record_id": self.relation_record_id,
            "paper_source_id": self.paper_source_id,
            "subject_id": self.subject_id,
            "object_id": self.object_id,
            "relation_type": self.relation_type,
            "evidence_text": self.evidence_text,
            "confidence": self.confidence,
            "metadata": _deep_thaw(self.metadata),
            "provenance": _deep_thaw(self.provenance),
        }

    def to_record(self, *, scope: ScopeRef, source: str = "eimemory.knowledge.relations") -> RecordEnvelope:
        ts = now_iso()
        return RecordEnvelope(
            record_id=self.relation_record_id,
            kind="relation_record",
            status="active",
            title=f"{self.subject_id} {self.relation_type} {self.object_id}",
            summary=self.evidence_text,
            detail=self.relation_type,
            content=self.to_payload(),
            tags=["paper", "relation", self.relation_type],
            links=[
                LinkRef(relation="derived_from", target_kind="paper_source", target_id=self.paper_source_id),
                LinkRef(relation="subject", target_kind="record", target_id=self.subject_id),
                LinkRef(relation="object", target_kind="record", target_id=self.object_id),
            ],
            evidence=[self.evidence_text or self.paper_source_id],
            source=source,
            scope=scope,
            time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
            provenance=_deep_thaw(self.provenance),
            meta={
                "paper_source_id": self.paper_source_id,
                "relation_type": self.relation_type,
                "confidence": self.confidence,
            },
        )
