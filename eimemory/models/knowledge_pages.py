from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.paper_sources import _deep_freeze, _deep_thaw
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef


@dataclass(slots=True, frozen=True)
class KnowledgePage:
    knowledge_page_id: str
    page_type: str
    title: str
    summary: str
    sections: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    supporting_claim_ids: tuple[str, ...] = field(default_factory=tuple)
    source_ids: tuple[str, ...] = field(default_factory=tuple)
    related_page_ids: tuple[str, ...] = field(default_factory=tuple)
    open_question_ids: tuple[str, ...] = field(default_factory=tuple)
    contradiction_ids: tuple[str, ...] = field(default_factory=tuple)
    last_compiled_at: str = ""
    compile_version: str = "knowledge_page.v1"
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "sections", tuple(MappingProxyType(dict(section)) for section in self.sections))
        object.__setattr__(self, "supporting_claim_ids", tuple(str(item) for item in self.supporting_claim_ids))
        object.__setattr__(self, "source_ids", tuple(str(item) for item in self.source_ids))
        object.__setattr__(self, "related_page_ids", tuple(str(item) for item in self.related_page_ids))
        object.__setattr__(self, "open_question_ids", tuple(str(item) for item in self.open_question_ids))
        object.__setattr__(self, "contradiction_ids", tuple(str(item) for item in self.contradiction_ids))
        object.__setattr__(self, "last_compiled_at", self.last_compiled_at or now_iso())
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata or {})))
        provenance = {**dict(self.provenance or {})}
        if self.source_ids:
            provenance["paper_source_id"] = self.source_ids[0]
            provenance["source_ids"] = list(self.source_ids)
        object.__setattr__(self, "provenance", _deep_freeze(provenance))

    def to_payload(self) -> dict[str, Any]:
        return {
            "knowledge_page_id": self.knowledge_page_id,
            "page_type": self.page_type,
            "title": self.title,
            "summary": self.summary,
            "sections": [_deep_thaw(section) for section in self.sections],
            "supporting_claim_ids": list(self.supporting_claim_ids),
            "source_ids": list(self.source_ids),
            "related_page_ids": list(self.related_page_ids),
            "open_question_ids": list(self.open_question_ids),
            "contradiction_ids": list(self.contradiction_ids),
            "last_compiled_at": self.last_compiled_at,
            "compile_version": self.compile_version,
            "metadata": _deep_thaw(self.metadata),
            "provenance": _deep_thaw(self.provenance),
        }

    def to_record(self, *, scope: ScopeRef, source: str = "eimemory.knowledge.compiler") -> RecordEnvelope:
        ts = now_iso()
        source_links = [
            LinkRef(relation="compiled_from", target_kind="paper_source", target_id=source_id)
            for source_id in self.source_ids
        ]
        claim_links = [
            LinkRef(relation="supported_by", target_kind="claim_card", target_id=claim_id)
            for claim_id in self.supporting_claim_ids
        ]
        page_links = [
            LinkRef(relation="related_page", target_kind="knowledge_page", target_id=page_id)
            for page_id in self.related_page_ids
        ]
        return RecordEnvelope(
            record_id=self.knowledge_page_id,
            kind="knowledge_page",
            status="active",
            title=self.title,
            summary=self.summary,
            detail="\n".join(str(section.get("text", "")) for section in self.sections),
            content=self.to_payload(),
            tags=["knowledge", "page", self.page_type],
            links=[*source_links, *claim_links, *page_links],
            evidence=[*self.source_ids, *self.supporting_claim_ids],
            source=source,
            scope=scope,
            time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
            provenance=_deep_thaw(self.provenance),
            meta={
                "page_type": self.page_type,
                "source_ids": list(self.source_ids),
                "supporting_claim_ids": list(self.supporting_claim_ids),
                "compile_version": self.compile_version,
            },
        )
