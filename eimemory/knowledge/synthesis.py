from __future__ import annotations

from collections import Counter
from typing import Iterable

from eimemory.core.clock import now_iso
from eimemory.knowledge.pages import stable_compiled_id
from eimemory.models.knowledge_pages import KnowledgePage
from eimemory.models.records import RecordEnvelope, ScopeRef

SYNTHESIS_SOURCE = "eimemory.knowledge.synthesis"


def build_research_digest(
    *,
    paper_sources: Iterable[RecordEnvelope],
    claim_cards: Iterable[RecordEnvelope],
    knowledge_pages: Iterable[RecordEnvelope],
    candidates: Iterable[RecordEnvelope] = (),
    limit: int = 5,
    digest_date: str | None = None,
) -> dict:
    max_items = max(1, int(limit))
    date = _digest_date(digest_date)
    papers = _recent_records([record for record in paper_sources if record.kind == "paper_source"])
    claims = _recent_records([record for record in claim_cards if record.kind == "claim_card"])
    pages = _recent_records(
        [
            record
            for record in knowledge_pages
            if record.kind == "knowledge_page" and _page_type(record) not in {"digest", "synthesis"}
        ]
    )
    candidate_records = _recent_records([record for record in candidates if record.kind == "knowledge_candidate"])

    source_ids = _source_ids(papers, claims, pages)
    top_papers = _top_papers(papers, pages, max_items)
    themes = _themes(papers, pages, max_items)
    notable_claims = _notable_claims(claims, max_items)
    open_questions = _open_questions(claims, pages, max_items)
    skipped = _skipped_summary(claims, candidate_records)
    summary = _summary(top_papers, themes, notable_claims, open_questions)

    return {
        "ok": bool(source_ids or top_papers or notable_claims or pages),
        "digest_date": date,
        "summary": summary,
        "paper_count": len(source_ids) or len(top_papers),
        "claim_count": len(claims),
        "knowledge_page_count": len(pages),
        "candidate_count": len(candidate_records),
        "top_papers": top_papers,
        "themes": themes,
        "notable_claims": notable_claims,
        "open_questions": open_questions,
        "skipped_low_confidence": skipped,
        "source_ids": source_ids,
        "persisted": False,
        "persisted_page_id": "",
    }


def digest_to_knowledge_page(
    digest: dict,
    *,
    scope: ScopeRef | dict | None = None,
    page_id: str | None = None,
) -> KnowledgePage:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    source_ids = [str(item) for item in digest.get("source_ids") or []]
    digest_date = str(digest.get("digest_date") or _digest_date(None))
    stable_scope = "|".join(
        [
            scope_ref.tenant_id,
            scope_ref.agent_id,
            scope_ref.workspace_id,
            scope_ref.user_id,
        ]
    )
    knowledge_page_id = page_id or stable_compiled_id("digest", digest_date, stable_scope)
    return KnowledgePage(
        knowledge_page_id=knowledge_page_id,
        page_type="digest",
        title=f"Research digest {digest_date}",
        summary=str(digest.get("summary") or "No recent paper knowledge available."),
        sections=(
            {"name": "top_papers", "text": _bullets(item["title"] for item in digest.get("top_papers") or [])},
            {"name": "themes", "text": _bullets(item["name"] for item in digest.get("themes") or [])},
            {"name": "notable_claims", "text": _bullets(item["text"] for item in digest.get("notable_claims") or [])},
            {"name": "open_questions", "text": _bullets(digest.get("open_questions") or [])},
            {
                "name": "skipped_low_confidence",
                "text": str(digest.get("skipped_low_confidence") or {}),
            },
        ),
        supporting_claim_ids=tuple(item["claim_id"] for item in digest.get("notable_claims") or [] if item.get("claim_id")),
        source_ids=tuple(source_ids),
        compile_version="research_digest.v1",
        metadata={
            "digest_date": digest_date,
            "paper_count": int(digest.get("paper_count") or 0),
            "claim_count": int(digest.get("claim_count") or 0),
            "candidate_count": int(digest.get("candidate_count") or 0),
        },
        provenance={"digest_date": digest_date, "source": SYNTHESIS_SOURCE},
    )


def digest_to_record(
    digest: dict,
    *,
    scope: ScopeRef | dict | None = None,
    page_id: str | None = None,
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    page = digest_to_knowledge_page(digest, scope=scope_ref, page_id=page_id)
    return page.to_record(scope=scope_ref, source=SYNTHESIS_SOURCE)


def _recent_records(records: list[RecordEnvelope]) -> list[RecordEnvelope]:
    return sorted(records, key=lambda record: (_record_date(record), record.record_id), reverse=True)


def _record_date(record: RecordEnvelope) -> str:
    return str(
        record.content.get("published_at")
        or record.provenance.get("published_at")
        or record.time.occurred_at
        or record.time.updated_at
        or ""
    )


def _source_ids(
    papers: list[RecordEnvelope],
    claims: list[RecordEnvelope],
    pages: list[RecordEnvelope],
) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in [
        *(paper.record_id for paper in papers),
        *(_paper_source_id(claim) for claim in claims),
        *(source_id for page in pages for source_id in _page_source_ids(page)),
    ]:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _top_papers(papers: list[RecordEnvelope], pages: list[RecordEnvelope], limit: int) -> list[dict]:
    if papers:
        return [
            {
                "record_id": record.record_id,
                "title": record.title,
                "published_at": str(record.content.get("published_at") or ""),
                "source_kind": str(record.content.get("source_kind") or ""),
                "summary": record.summary,
            }
            for record in papers[:limit]
        ]
    paper_pages = [page for page in pages if _page_type(page) == "paper"]
    return [
        {
            "record_id": record.record_id,
            "title": record.title,
            "published_at": _record_date(record),
            "source_kind": "knowledge_page",
            "summary": record.summary,
        }
        for record in paper_pages[:limit]
    ]


def _themes(papers: list[RecordEnvelope], pages: list[RecordEnvelope], limit: int) -> list[dict]:
    counter: Counter[str] = Counter()
    for record in papers:
        for category in _categories(record):
            counter[category] += 1
    for record in pages:
        page_type = _page_type(record)
        if page_type == "topic" and record.title:
            counter[record.title] += 1
    return [
        {"name": name, "count": count}
        for name, count in sorted(counter.items(), key=lambda item: (-item[1], item[0].lower()))[:limit]
    ]


def _categories(record: RecordEnvelope) -> list[str]:
    metadata = record.content.get("metadata") if isinstance(record.content.get("metadata"), dict) else {}
    categories = metadata.get("categories") or metadata.get("category") or []
    if isinstance(categories, str):
        categories = [categories]
    return [str(item).strip() for item in categories if str(item).strip()]


def _notable_claims(claims: list[RecordEnvelope], limit: int) -> list[dict]:
    ranked = sorted(
        claims,
        key=lambda record: (float(record.content.get("confidence") or record.meta.get("confidence") or 0.0), _record_date(record), record.record_id),
        reverse=True,
    )
    return [
        {
            "claim_id": record.record_id,
            "paper_source_id": _paper_source_id(record),
            "text": str(record.content.get("claim_text") or record.summary or record.title),
            "confidence": float(record.content.get("confidence") or record.meta.get("confidence") or 0.0),
        }
        for record in ranked[:limit]
    ]


def _open_questions(claims: list[RecordEnvelope], pages: list[RecordEnvelope], limit: int) -> list[str]:
    questions: list[str] = []
    for record in [*claims, *pages]:
        for text in [record.summary, record.detail, str(record.content.get("claim_text") or "")]:
            lowered = text.lower()
            if "open question" not in lowered and "limitation" not in lowered and "?" not in text:
                continue
            cleaned = " ".join(text.split()).strip()
            if cleaned and cleaned not in questions:
                questions.append(cleaned)
            if len(questions) >= limit:
                return questions
    return questions


def _skipped_summary(claims: list[RecordEnvelope], candidates: list[RecordEnvelope]) -> dict:
    low_confidence = [
        record
        for record in claims
        if float(record.content.get("confidence") or record.meta.get("confidence") or 0.0) < 0.5
    ]
    statuses = Counter(record.status for record in candidates if record.status in {"rejected", "quarantined", "candidate"})
    return {
        "low_confidence_claim_count": len(low_confidence),
        "candidate_count": statuses.get("candidate", 0),
        "rejected_count": statuses.get("rejected", 0),
        "quarantined_count": statuses.get("quarantined", 0),
    }


def _summary(
    top_papers: list[dict],
    themes: list[dict],
    notable_claims: list[dict],
    open_questions: list[str],
) -> str:
    if not top_papers and not notable_claims:
        return "No recent paper knowledge available for synthesis."
    paper_titles = ", ".join(item["title"] for item in top_papers[:3])
    theme_names = ", ".join(item["name"] for item in themes[:3]) or "no dominant theme"
    claim_text = notable_claims[0]["text"] if notable_claims else "no notable claim"
    question_text = f" Open question: {open_questions[0]}" if open_questions else ""
    return f"Recent papers: {paper_titles}. Themes: {theme_names}. Notable claim: {claim_text}.{question_text}"


def _paper_source_id(record: RecordEnvelope) -> str:
    return str(record.content.get("paper_source_id") or record.meta.get("paper_source_id") or record.provenance.get("paper_source_id") or "")


def _page_source_ids(record: RecordEnvelope) -> list[str]:
    return [str(item) for item in (record.content.get("source_ids") or record.meta.get("source_ids") or []) if str(item)]


def _page_type(record: RecordEnvelope) -> str:
    return str(record.content.get("page_type") or record.meta.get("page_type") or "")


def _bullets(values: Iterable[str]) -> str:
    lines = [f"- {str(value).strip()}" for value in values if str(value).strip()]
    return "\n".join(lines)


def _digest_date(value: str | None) -> str:
    if value:
        return str(value)[:10]
    return now_iso()[:10]
