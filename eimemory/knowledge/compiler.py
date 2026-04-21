from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eimemory.knowledge.extract import PaperMemoryExtraction
from eimemory.knowledge.pages import stable_compiled_id, stable_page_id, summarize_claims
from eimemory.models.knowledge_pages import KnowledgePage
from eimemory.models.records import RecordEnvelope, ScopeRef


@dataclass(slots=True, frozen=True)
class KnowledgeCompilation:
    pages: tuple[KnowledgePage, ...]

    def to_records(self, *, scope: ScopeRef | dict | None = None) -> list[RecordEnvelope]:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return [page.to_record(scope=scope_ref) for page in self.pages]


def compile_paper_knowledge(
    *,
    extraction: PaperMemoryExtraction | None = None,
    paper_source_id: str | None = None,
    paper_title: str | None = None,
    claims: list[str] | None = None,
    entities: list[str] | None = None,
    claim_records: list[RecordEnvelope] | None = None,
    entity_records: list[RecordEnvelope] | None = None,
    provenance: dict[str, Any] | None = None,
) -> KnowledgeCompilation:
    if extraction is not None:
        source_id = extraction.extract.paper_source_id
        title = extraction.extract.title
        claim_pairs = [(claim.claim_card_id, claim.claim_text) for claim in extraction.claims]
        entity_names = [entity.name for entity in extraction.entities]
        page_provenance = dict(extraction.extract.provenance)
    else:
        source_id = str(paper_source_id or "")
        title = str(paper_title or source_id or "Untitled paper")
        if claim_records:
            claim_pairs = [
                (record.record_id, record.summary or str(record.content.get("claim_text") or record.title))
                for record in claim_records
                if record.kind == "claim_card"
            ]
        else:
            claim_pairs = [
                (stable_compiled_id("claim", source_id, claim), claim)
                for claim in (claims or [])
                if str(claim).strip()
            ]
        if entity_records:
            entity_names = [
                str(record.content.get("name") or record.title).strip()
                for record in entity_records
                if record.kind == "entity_record" and str(record.content.get("name") or record.title).strip()
            ]
        else:
            entity_names = [str(entity).strip() for entity in (entities or []) if str(entity).strip()]
        page_provenance = dict(provenance or {})
    if not source_id:
        raise ValueError("paper_source_id is required for knowledge compilation")

    claim_texts = [claim_text for _, claim_text in claim_pairs]
    claim_ids = [claim_id for claim_id, _ in claim_pairs]
    paper_page_id = stable_page_id("paper", source_id, title)
    topic_pages: list[KnowledgePage] = []
    paper_page = KnowledgePage(
        knowledge_page_id=paper_page_id,
        page_type="paper",
        title=title,
        summary=summarize_claims(claim_texts) or title,
        sections=[
            {
                "name": "claims",
                "text": "\n".join(f"- {claim}" for claim in claim_texts),
            },
            {
                "name": "entities",
                "text": ", ".join(entity_names),
            },
        ],
        supporting_claim_ids=tuple(claim_ids),
        source_ids=(source_id,),
        provenance=page_provenance,
    )
    for entity_name in _dedupe_preserve_order(entity_names):
        page_id = stable_page_id("topic", entity_name.lower(), source_id)
        relevant_claims = [
            (claim_id, claim_text)
            for claim_id, claim_text in claim_pairs
            if entity_name.lower() in claim_text.lower()
        ] or claim_pairs[:2]
        topic_pages.append(
            KnowledgePage(
                knowledge_page_id=page_id,
                page_type="topic",
                title=entity_name,
                summary=summarize_claims([claim_text for _, claim_text in relevant_claims]) or entity_name,
                sections=[
                    {
                        "name": "topic_summary",
                        "text": summarize_claims([claim_text for _, claim_text in relevant_claims], max_items=5),
                    }
                ],
                supporting_claim_ids=tuple(claim_id for claim_id, _ in relevant_claims),
                source_ids=(source_id,),
                related_page_ids=(paper_page_id,),
                provenance=page_provenance,
            )
        )
    return KnowledgeCompilation(pages=(paper_page, *tuple(topic_pages)))


def _dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
