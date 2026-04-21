from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.paper_sources import _deep_freeze, _deep_thaw
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef


@dataclass(slots=True, frozen=True)
class ClaimCard:
    claim_card_id: str
    paper_source_id: str
    paper_extract_id: str
    claim_text: str
    claim_type: str = "finding"
    evidence_text: str = ""
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "confidence", max(0.0, min(1.0, float(self.confidence))))
        object.__setattr__(self, "metadata", _deep_freeze(dict(self.metadata or {})))
        provenance = {
            **dict(self.provenance or {}),
            "paper_source_id": self.paper_source_id,
            "paper_extract_id": self.paper_extract_id,
        }
        object.__setattr__(self, "provenance", _deep_freeze(provenance))

    def to_payload(self) -> dict[str, Any]:
        return {
            "claim_card_id": self.claim_card_id,
            "paper_source_id": self.paper_source_id,
            "paper_extract_id": self.paper_extract_id,
            "claim_text": self.claim_text,
            "claim_type": self.claim_type,
            "evidence_text": self.evidence_text,
            "confidence": self.confidence,
            "metadata": _deep_thaw(self.metadata),
            "provenance": _deep_thaw(self.provenance),
        }

    def to_record(self, *, scope: ScopeRef, source: str = "eimemory.knowledge.claims") -> RecordEnvelope:
        ts = now_iso()
        return RecordEnvelope(
            record_id=self.claim_card_id,
            kind="claim_card",
            status="active",
            title=self.claim_text[:96] or self.claim_card_id,
            summary=self.claim_text,
            detail=self.evidence_text,
            content=self.to_payload(),
            tags=["paper", "claim", self.claim_type],
            links=[
                LinkRef(relation="derived_from", target_kind="paper_source", target_id=self.paper_source_id),
                LinkRef(relation="extracted_from", target_kind="paper_extract", target_id=self.paper_extract_id),
            ],
            evidence=[self.evidence_text or self.paper_source_id],
            source=source,
            scope=scope,
            time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
            provenance=_deep_thaw(self.provenance),
            meta={
                "paper_source_id": self.paper_source_id,
                "paper_extract_id": self.paper_extract_id,
                "claim_type": self.claim_type,
                "confidence": self.confidence,
            },
        )
