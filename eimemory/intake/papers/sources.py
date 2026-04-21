from __future__ import annotations

from typing import Any

from eimemory.intake.papers.normalize import normalize_paper_source_payload
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore

def _paper_source_id_from_hash(source_hash: str) -> str:
    return f"psrc_{source_hash[:12]}"


def paper_source_from_payload(payload: dict[str, Any]) -> PaperSource:
    source_hash = str(payload.get("source_hash") or "")
    paper_source_id = str(payload.get("paper_source_id") or _paper_source_id_from_hash(source_hash))
    authors = tuple(str(item) for item in (payload.get("authors") or []))
    return PaperSource(
        paper_source_id=paper_source_id,
        source_kind=str(payload.get("source_kind") or ""),
        title=str(payload.get("title") or ""),
        authors=authors,
        abstract=str(payload.get("abstract") or ""),
        venue=str(payload.get("venue") or ""),
        published_at=str(payload.get("published_at") or ""),
        doi=str(payload.get("doi") or ""),
        arxiv_id=str(payload.get("arxiv_id") or ""),
        canonical_url=str(payload.get("canonical_url") or ""),
        pdf_blob_ref=str(payload.get("pdf_blob_ref") or ""),
        normalized_text_ref=str(payload.get("normalized_text_ref") or ""),
        source_hash=source_hash,
        metadata=dict(payload.get("metadata") or {}),
        provenance=dict(payload.get("provenance") or {}),
    )


def ingest_paper_source(
    store: RuntimeStore,
    paper_input: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    normalized = normalize_paper_source_payload(paper_input)
    paper_source = paper_source_from_payload(normalized)
    record = paper_source.to_record(scope=scope_ref)
    return store.append(record)
