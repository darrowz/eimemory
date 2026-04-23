from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef, evaluate_memory_quality
from eimemory.storage.runtime_store import RuntimeStore


PROJECTION_TYPE = "operational_knowledge"
PROJECTOR_SOURCE = "eimemory.knowledge.projectors"
MIN_CLAIM_CONFIDENCE = 0.75
MIN_PROJECTION_SCORE = 0.72

_BLOCKED_STATUSES = {"rejected", "deprecated", "conflicted", "needs_refresh"}
_OPERATIONAL_TERMS = {
    "api",
    "architecture",
    "config",
    "contract",
    "decision",
    "deploy",
    "eibrain",
    "eimemory",
    "interface",
    "memory",
    "must",
    "openclaw",
    "operational",
    "policy",
    "prefer",
    "preference",
    "recall",
    "runtime",
    "scope",
    "should",
    "tenant",
    "user",
    "verified",
}


@dataclass(slots=True, frozen=True)
class ProjectionCandidate:
    source: RecordEnvelope
    text: str
    title: str
    reason: str
    score: float
    confidence: float


def project_operational_knowledge(
    store: RuntimeStore,
    *,
    scope: ScopeRef | dict | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Project high-value compiled knowledge into memory records for recall only."""
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    source_records = store.list_records(
        kinds=["claim_card", "knowledge_page"],
        scope=scope_ref,
        limit=max(0, int(limit)),
    )
    existing_source_ids = _existing_projected_source_ids(store, scope_ref)
    projected: list[RecordEnvelope] = []
    skipped: list[dict[str, str]] = []
    for source in source_records:
        if source.record_id in existing_source_ids or store.get_by_id(stable_projection_id(source)) is not None:
            skipped.append({"record_id": source.record_id, "reason": "already_projected"})
            continue
        candidate, skip_reason = _candidate_from_record(source)
        if candidate is None:
            skipped.append({"record_id": source.record_id, "reason": skip_reason})
            continue
        memory = _memory_from_candidate(candidate)
        store.append(memory)
        projected.append(memory)
        existing_source_ids.add(source.record_id)
    return {
        "ok": True,
        "scanned_count": len(source_records),
        "projected_count": len(projected),
        "skipped_count": len(skipped),
        "projected_ids": [record.record_id for record in projected],
        "skipped": skipped,
    }


def stable_projection_id(source: RecordEnvelope) -> str:
    digest = hashlib.sha256(
        "\x1f".join([PROJECTION_TYPE, source.kind, source.record_id]).encode("utf-8")
    ).hexdigest()[:16]
    return f"mem_proj_{digest}"


def _candidate_from_record(record: RecordEnvelope) -> tuple[ProjectionCandidate | None, str]:
    if _is_blocked_source(record):
        return None, "unsafe_source_status"
    if record.kind == "claim_card":
        return _claim_candidate(record)
    if record.kind == "knowledge_page":
        return _page_candidate(record)
    return None, "unsupported_kind"


def _claim_candidate(record: RecordEnvelope) -> tuple[ProjectionCandidate | None, str]:
    text = _source_text(record, "claim_text")
    if not _substantial(text):
        return None, "empty_or_thin_summary"
    confidence = _number(record.meta.get("reliability"), record.meta.get("confidence"), record.content.get("confidence"))
    if confidence < MIN_CLAIM_CONFIDENCE:
        return None, "low_confidence"
    if not _has_operational_terms(text, record.title, record.detail):
        return None, "not_operational"
    score = _projection_score(text=text, confidence=confidence, source_kind=record.kind)
    if score < MIN_PROJECTION_SCORE:
        return None, "low_projection_score"
    return (
        ProjectionCandidate(
            source=record,
            text=text,
            title=f"Operational claim: {record.title[:80] or record.record_id}",
            reason="high_confidence_operational_claim",
            score=score,
            confidence=confidence,
        ),
        "",
    )


def _page_candidate(record: RecordEnvelope) -> tuple[ProjectionCandidate | None, str]:
    text = _source_text(record, "summary")
    if not _substantial(text):
        return None, "empty_or_thin_summary"
    if not _has_operational_terms(text, record.title, record.detail):
        return None, "not_operational"
    confidence = 0.82 if record.content.get("supporting_claim_ids") else 0.76
    score = _projection_score(text=text, confidence=confidence, source_kind=record.kind)
    if score < MIN_PROJECTION_SCORE:
        return None, "low_projection_score"
    return (
        ProjectionCandidate(
            source=record,
            text=text,
            title=f"Operational page: {record.title[:80] or record.record_id}",
            reason="operational_knowledge_page",
            score=score,
            confidence=confidence,
        ),
        "",
    )


def _memory_from_candidate(candidate: ProjectionCandidate) -> RecordEnvelope:
    ts = now_iso()
    source = candidate.source
    quality = evaluate_memory_quality(
        text=candidate.text,
        title=candidate.title,
        memory_type="fact",
        source=PROJECTOR_SOURCE,
    )
    provenance = {
        **dict(source.provenance or {}),
        "projection_type": PROJECTION_TYPE,
        "projector": PROJECTOR_SOURCE,
        "source_record_id": source.record_id,
        "source_record_kind": source.kind,
    }
    meta = {
        "memory_type": "fact",
        "projection_type": PROJECTION_TYPE,
        "projection_reason": candidate.reason,
        "projection_score": candidate.score,
        "projector": PROJECTOR_SOURCE,
        "source_record_id": source.record_id,
        "source_record_kind": source.kind,
        "source_confidence": candidate.confidence,
        "source_status": source.status,
        "quality": quality,
    }
    return RecordEnvelope(
        record_id=stable_projection_id(source),
        kind="memory",
        status="active",
        title=candidate.title,
        summary=candidate.text,
        detail=source.detail,
        content={
            "text": candidate.text,
            "memory_type": "fact",
            "projection_type": PROJECTION_TYPE,
            "source_record_id": source.record_id,
            "source_record_kind": source.kind,
        },
        tags=_projected_tags(source),
        links=[LinkRef(relation="projected_from", target_kind=source.kind, target_id=source.record_id)],
        evidence=[source.record_id, *source.evidence],
        source=PROJECTOR_SOURCE,
        scope=source.scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        provenance=provenance,
        meta=meta,
    )


def _existing_projected_source_ids(store: RuntimeStore, scope: ScopeRef) -> set[str]:
    source_ids: set[str] = set()
    offset = 0
    page_size = 500
    while True:
        memories = store.list_records(kinds=["memory"], scope=scope, limit=page_size, offset=offset)
        for memory in memories:
            if memory.meta.get("projection_type") != PROJECTION_TYPE:
                continue
            source_id = str(
                memory.meta.get("source_record_id")
                or memory.provenance.get("source_record_id")
                or memory.content.get("source_record_id")
                or ""
            )
            if source_id:
                source_ids.add(source_id)
        if len(memories) < page_size:
            break
        offset += len(memories)
    return source_ids


def _is_blocked_source(record: RecordEnvelope) -> bool:
    if record.status in _BLOCKED_STATUSES:
        return True
    blocked_flags = (
        record.meta.get("deprecated"),
        record.content.get("deprecated"),
        record.meta.get("contradiction_ids"),
        record.content.get("contradiction_ids"),
        record.meta.get("contradiction_claim_ids"),
        record.content.get("contradiction_claim_ids"),
    )
    return any(bool(flag) for flag in blocked_flags)


def _source_text(record: RecordEnvelope, content_key: str) -> str:
    return str(record.content.get(content_key) or record.summary or record.detail or record.title).strip()


def _substantial(text: str) -> bool:
    alnum = sum(1 for char in text if char.isalnum())
    word_count = len(re.findall(r"[\w]+", text, flags=re.UNICODE))
    return alnum >= 24 and word_count >= 5


def _has_operational_terms(*parts: str) -> bool:
    normalized = " ".join(str(part or "").lower() for part in parts)
    return any(term in normalized for term in _OPERATIONAL_TERMS)


def _projection_score(*, text: str, confidence: float, source_kind: str) -> float:
    normalized = text.lower()
    term_hits = sum(1 for term in _OPERATIONAL_TERMS if term in normalized)
    length_bonus = min(0.08, len(text) / 1200)
    kind_bonus = 0.06 if source_kind == "knowledge_page" else 0.04
    score = 0.36 + (confidence * 0.36) + min(0.18, term_hits * 0.035) + length_bonus + kind_bonus
    return round(max(0.0, min(1.0, score)), 3)


def _number(*values: object) -> float:
    for value in values:
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _projected_tags(source: RecordEnvelope) -> list[str]:
    tags = ["projected", "operational", "knowledge", source.kind]
    for tag in source.tags:
        if tag not in tags:
            tags.append(tag)
    return tags
