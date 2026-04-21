from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from eimemory.knowledge.claims import build_claim_cards
from eimemory.knowledge.relations import build_entity_records, build_relation_records
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.entity_records import EntityRecord
from eimemory.models.paper_extracts import PaperExtract
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.relation_records import RelationRecord


@dataclass(slots=True, frozen=True)
class PaperMemoryExtraction:
    extract: PaperExtract
    claims: tuple[ClaimCard, ...]
    entities: tuple[EntityRecord, ...]
    relations: tuple[RelationRecord, ...]

    def to_records(self, *, scope: ScopeRef | dict | None = None) -> list[RecordEnvelope]:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        records = [self.extract.to_record(scope=scope_ref)]
        records.extend(claim.to_record(scope=scope_ref) for claim in self.claims)
        records.extend(entity.to_record(scope=scope_ref) for entity in self.entities)
        records.extend(relation.to_record(scope=scope_ref) for relation in self.relations)
        return records


def extract_paper_memory(
    *,
    paper_source_id: str,
    title: str,
    abstract: str = "",
    body: str = "",
    metadata: dict[str, Any] | None = None,
    provenance: dict[str, Any] | None = None,
) -> PaperMemoryExtraction:
    normalized_title = _clean_text(title)
    normalized_abstract = _clean_text(abstract)
    normalized_body = _clean_text(body)
    extract_id = _stable_id("paper_extract", paper_source_id, normalized_title, normalized_abstract, normalized_body)
    sentences = _sentences(" ".join(part for part in [normalized_abstract, normalized_body] if part))
    sections = _sections(normalized_abstract, normalized_body)
    extract = PaperExtract(
        paper_extract_id=extract_id,
        paper_source_id=paper_source_id,
        title=normalized_title,
        abstract=normalized_abstract,
        body=normalized_body,
        sections=tuple(sections),
        metadata=metadata or {},
        provenance=provenance or {},
    )
    claims = build_claim_cards(
        paper_source_id=paper_source_id,
        paper_extract_id=extract_id,
        sentences=sentences,
        provenance=provenance or {},
    )
    entities = build_entity_records(
        paper_source_id=paper_source_id,
        title=normalized_title,
        text=" ".join(part for part in [normalized_title, normalized_abstract, normalized_body] if part),
        provenance=provenance or {},
    )
    relations = build_relation_records(
        paper_source_id=paper_source_id,
        claims=claims,
        entities=entities,
        provenance=provenance or {},
    )
    return PaperMemoryExtraction(
        extract=extract,
        claims=tuple(claims),
        entities=tuple(entities),
        relations=tuple(relations),
    )


def stable_memory_id(kind: str, *parts: str) -> str:
    return _stable_id(kind, *parts)


def _stable_id(kind: str, *parts: str) -> str:
    prefix_by_kind = {
        "paper_extract": "pex",
        "claim_card": "claim",
        "entity_record": "ent",
        "relation_record": "rel",
    }
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix_by_kind.get(kind, 'rec')}_{digest}"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _sentences(text: str) -> list[str]:
    return [
        sentence.strip(" \t\r\n.;:")
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text)
        if sentence.strip(" \t\r\n.;:")
    ]


def _sections(abstract: str, body: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    if abstract:
        sections.append({"name": "abstract", "text": abstract})
    if body:
        sections.append({"name": "body", "text": body})
    return sections
