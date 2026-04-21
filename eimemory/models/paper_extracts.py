from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.paper_sources import _deep_freeze, _deep_thaw
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef


@dataclass(slots=True, frozen=True)
class PaperExtract:
    paper_extract_id: str
    paper_source_id: str
    title: str
    abstract: str = ""
    body: str = ""
    sections: tuple[dict[str, str], ...] = field(default_factory=tuple)
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(MappingProxyType(dict(section)) for section in self.sections))
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata or {})))
        provenance = {**dict(self.provenance or {}), "paper_source_id": self.paper_source_id}
        object.__setattr__(self, "provenance", _deep_freeze(provenance))

    def to_payload(self) -> dict[str, Any]:
        return {
            "paper_extract_id": self.paper_extract_id,
            "paper_source_id": self.paper_source_id,
            "title": self.title,
            "abstract": self.abstract,
            "body": self.body,
            "sections": [_deep_thaw(section) for section in self.sections],
            "metadata": _deep_thaw(self.metadata),
            "provenance": _deep_thaw(self.provenance),
        }

    def to_record(self, *, scope: ScopeRef, source: str = "eimemory.knowledge.extract") -> RecordEnvelope:
        ts = now_iso()
        return RecordEnvelope(
            record_id=self.paper_extract_id,
            kind="paper_extract",
            status="active",
            title=self.title or self.paper_extract_id,
            summary=self.abstract,
            detail="Structured text extract derived from a paper source",
            content=self.to_payload(),
            tags=["paper", "extract"],
            links=[LinkRef(relation="derived_from", target_kind="paper_source", target_id=self.paper_source_id)],
            evidence=[self.paper_source_id],
            source=source,
            scope=scope,
            time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
            provenance=_deep_thaw(self.provenance),
            meta={
                "paper_source_id": self.paper_source_id,
                "paper_extract_id": self.paper_extract_id,
            },
        )
