from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Any

from eimemory.models.claim_cards import ClaimCard
from eimemory.models.entity_records import EntityRecord
from eimemory.models.relation_records import RelationRecord


_STOP_ENTITIES = {
    "Abstract",
    "Background",
    "Conclusion",
    "Limitation",
    "Method",
    "Results",
    "This",
}


def build_entity_records(
    *,
    paper_source_id: str,
    title: str,
    text: str,
    provenance: dict[str, Any] | None = None,
) -> tuple[EntityRecord, ...]:
    candidates = _entity_candidates(title, text)
    records: list[EntityRecord] = []
    for name, count in candidates[:12]:
        records.append(
            EntityRecord(
                entity_record_id=_stable_id("ent", paper_source_id, name.lower()),
                paper_source_id=paper_source_id,
                name=name,
                entity_type=_entity_type(name),
                salience=min(1.0, 0.35 + (count * 0.15)),
                provenance=provenance or {},
            )
        )
    return tuple(records)


def build_relation_records(
    *,
    paper_source_id: str,
    claims: tuple[ClaimCard, ...],
    entities: tuple[EntityRecord, ...],
    provenance: dict[str, Any] | None = None,
) -> tuple[RelationRecord, ...]:
    if not claims or not entities:
        return tuple()
    relations: list[RelationRecord] = []
    primary = entities[0]
    for claim in claims:
        relation_type = "limited_by" if claim.claim_type == "limitation" else "supports"
        matched = _best_entity_for_claim(claim, entities) or primary
        relations.append(
            RelationRecord(
                relation_record_id=_stable_id("rel", paper_source_id, matched.entity_record_id, claim.claim_card_id, relation_type),
                paper_source_id=paper_source_id,
                subject_id=matched.entity_record_id,
                object_id=claim.claim_card_id,
                relation_type=relation_type,
                evidence_text=claim.evidence_text,
                confidence=min(claim.confidence, matched.salience),
                provenance=provenance or {},
            )
        )
    return tuple(relations)


def _entity_candidates(title: str, text: str) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for phrase in re.findall(r"\b[A-Z][A-Za-z0-9]*(?:[-\s]+[A-Z][A-Za-z0-9]*){0,3}\b", text):
        clean = " ".join(phrase.split())
        if clean not in _STOP_ENTITIES and len(clean) > 2:
            counter[clean] += 2 if clean in title else 1
    lowered_words = re.findall(r"\b[a-z][a-z0-9-]{4,}\b", text.lower())
    for word, count in Counter(lowered_words).most_common(16):
        if word not in {"paper", "shows", "tested", "under", "quality"} and count > 1:
            counter[word] += count
    if title and not counter:
        counter[title] += 1
    return sorted(counter.items(), key=lambda item: (-item[1], item[0].lower()))


def _entity_type(name: str) -> str:
    lowered = name.lower()
    if any(token in lowered for token in ["robot", "embodied"]):
        return "domain"
    if any(token in lowered for token in ["retrieval", "memory", "planning"]):
        return "concept"
    return "concept"


def _best_entity_for_claim(claim: ClaimCard, entities: tuple[EntityRecord, ...]) -> EntityRecord | None:
    lowered = claim.claim_text.lower()
    for entity in entities:
        if entity.name.lower() in lowered:
            return entity
    return None


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"
