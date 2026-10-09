from __future__ import annotations
# EXT-11 FIXED: project_operational_knowledge pages with incomplete flag

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.knowledge.evidence_contracts import (
    declared_confidences as _declared_confidences,
    finite_confidence_floor as _number,
    record_version_digest as _source_version_digest,
    versioned_record_ref as _versioned_record_ref,
)
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef, evaluate_memory_quality
from eimemory.storage.runtime_store import RuntimeStore


PROJECTION_TYPE = "operational_knowledge"
PROJECTOR_SOURCE = "eimemory.knowledge.projectors"
MIN_CLAIM_CONFIDENCE = 0.75
MIN_PROJECTION_SCORE = 0.72
SUPPORT_LINEAGE_SCHEMA = "knowledge.projection_support.v1"
PROJECTION_LINEAGE_SCHEMA = "knowledge.projection_lineage.v1"
MAX_SUPPORTING_CLAIMS = 100

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
    supporting_claims: tuple[RecordEnvelope, ...] = ()


def project_operational_knowledge(
    store: RuntimeStore,
    *,
    scope: ScopeRef | dict | None = None,
    limit: int = 100,
    max_pages: int = 20,
    page_size: int = 100,
) -> dict[str, Any]:
    """Project high-value compiled knowledge into memory records for recall only.

    EXT-11: stream pages instead of one unbounded materialization.
    """
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    target = max(0, int(limit))
    page = max(1, min(500, int(page_size)))
    pages = max(1, min(100, int(max_pages)))
    source_records: list[RecordEnvelope] = []
    incomplete = False
    offset = 0
    for _ in range(pages):
        if len(source_records) >= target:
            break
        batch = store.list_records(
            kinds=["claim_card", "knowledge_page"],
            scope=scope_ref,
            limit=min(page, target - len(source_records)),
            offset=offset,
        )
        if not batch:
            break
        source_records.extend(batch)
        offset += len(batch)
        if len(batch) < page:
            break
    else:
        incomplete = len(source_records) < target
    skipped: list[dict[str, str]] = []
    candidates: list[tuple[RecordEnvelope, str]] = []
    for source in source_records:
        candidate, skip_reason = _candidate_from_record(source, store=store)
        if candidate is None:
            skipped.append({"record_id": source.record_id, "reason": skip_reason})
            continue
        candidates.append((source, _source_version_digest(source)))

    if not candidates:
        report = _projection_report(source_records=source_records, projected=[], skipped=skipped)
        report['incomplete'] = incomplete
        return report

    def mutation(sqlite):
        projected: list[RecordEnvelope] = []
        transaction_skips = list(skipped)
        for planned_source, planned_version in candidates:
            current_source = sqlite.get_by_exact_ref(
                planned_source.record_id,
                scope=planned_source.scope,
                source_id=planned_source.source_id,
            )
            if (
                current_source is None
                or _source_version_digest(current_source) != planned_version
            ):
                transaction_skips.append(
                    {"record_id": planned_source.record_id, "reason": "source_changed"}
                )
                continue
            # Recheck support versions and eligibility before even accepting an
            # existing projection as idempotent.  Visibility-expanded lookups
            # must never stand in for the exact parent authority.
            candidate, skip_reason = _candidate_from_record(current_source, store=sqlite)
            if candidate is None:
                transaction_skips.append(
                    {"record_id": current_source.record_id, "reason": skip_reason}
                )
                continue
            existing_projection = sqlite.get_by_exact_ref(
                stable_projection_id(current_source),
                scope=current_source.scope,
                source_id=current_source.source_id,
            )
            if existing_projection is not None:
                if not (_projection_matches_parent(existing_projection, current_source)
                        or _refresh_replaces_projection(existing_projection, current_source)):
                    transaction_skips.append(
                        {"record_id": current_source.record_id, "reason": "projection_identity_conflict"}
                    )
                    continue
                if existing_projection.status == "active":
                    transaction_skips.append(
                        {"record_id": current_source.record_id, "reason": "already_projected"}
                    )
                    continue
            memory = _memory_from_candidate(candidate)
            sqlite.upsert(memory, commit=False)
            projected.append(memory)
        report = _projection_report(
            source_records=source_records,
            projected=projected,
            skipped=transaction_skips,
        )
        report["incomplete"] = incomplete
        return (
            report,
            projected,
            [],
        )

    result = store.mutate_records_atomically(mutation)
    if isinstance(result, tuple) and result and isinstance(result[0], dict):
        result[0]["incomplete"] = incomplete
    elif isinstance(result, dict):
        result["incomplete"] = incomplete
    return result


def _projection_report(
    *,
    source_records: list[RecordEnvelope],
    projected: list[RecordEnvelope],
    skipped: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "ok": True,
        "scanned_count": len(source_records),
        "projected_count": len(projected),
        "skipped_count": len(skipped),
        "projected_ids": [record.record_id for record in projected],
        "skipped": skipped,
    }


def stable_projection_id(source: RecordEnvelope) -> str:
    parts = [PROJECTION_TYPE, source.kind, source.record_id]
    # Keep the established default-origin ID contract.  A different source
    # domain gets a different ID, without moving/rewriting legacy artifacts.
    if source.source_id != "default":
        parts.extend(["source_partition.v1", source.source_id])
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"mem_proj_{digest}"


def _candidate_from_record(record: RecordEnvelope, *, store=None) -> tuple[ProjectionCandidate | None, str]:
    if _is_blocked_source(record):
        return None, "unsafe_source_status"
    if record.kind == "claim_card":
        return _claim_candidate(record)
    if record.kind == "knowledge_page":
        return _page_candidate(record, store=store)
    return None, "unsupported_kind"


def _claim_candidate(record: RecordEnvelope) -> tuple[ProjectionCandidate | None, str]:
    text = _source_text(record, "claim_text")
    if not _substantial(text):
        return None, "empty_or_thin_summary"
    confidence = _claim_confidence(record)
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


def _page_candidate(record: RecordEnvelope, *, store=None) -> tuple[ProjectionCandidate | None, str]:
    text = _source_text(record, "summary")
    if not _substantial(text):
        return None, "empty_or_thin_summary"
    if not _has_operational_terms(text, record.title, record.detail):
        return None, "not_operational"
    supporting_claims, reason = _validated_supporting_claims(record, store=store)
    if reason:
        return None, reason
    confidence = min(_claim_confidence(claim) for claim in supporting_claims)
    declared_confidence = _declared_confidences(record)
    if declared_confidence:
        confidence = min(confidence, _number(*declared_confidence))
    if confidence < MIN_CLAIM_CONFIDENCE:
        return None, "low_confidence"
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
            supporting_claims=supporting_claims,
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
        "projection_lineage_schema": PROJECTION_LINEAGE_SCHEMA,
        "source_record_ref": _versioned_record_ref(source),
        "supporting_claim_refs": [_versioned_record_ref(claim) for claim in candidate.supporting_claims],
    }
    meta = {
        "memory_type": "fact",
        "projection_type": PROJECTION_TYPE,
        "projection_reason": candidate.reason,
        "projection_score": candidate.score,
        "projector": PROJECTOR_SOURCE,
        "source_record_id": source.record_id,
        "source_record_kind": source.kind,
        "projection_lineage_schema": PROJECTION_LINEAGE_SCHEMA,
        "source_record_ref": _versioned_record_ref(source),
        "supporting_claim_refs": [_versioned_record_ref(claim) for claim in candidate.supporting_claims],
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
        source_id=source.source_id,
        scope=source.scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        provenance=provenance,
        meta=meta,
    )


def _projection_matches_parent(memory: RecordEnvelope, source: RecordEnvelope) -> bool:
    if (
        memory.kind != "memory"
        or memory.source != PROJECTOR_SOURCE
        or memory.scope != source.scope
        or memory.source_id != source.source_id
        or memory.meta.get("projection_type") != PROJECTION_TYPE
        or memory.meta.get("source_record_id") != source.record_id
        or memory.meta.get("source_record_kind") != source.kind
    ):
        return False
    parent_ref = memory.meta.get("source_record_ref")
    if parent_ref is not None:
        expected_support = source.content.get("supporting_claim_refs", []) if source.kind == "knowledge_page" else []
        return (
            memory.meta.get("projection_lineage_schema") == PROJECTION_LINEAGE_SCHEMA
            and memory.provenance.get("projection_lineage_schema") == PROJECTION_LINEAGE_SCHEMA
            and parent_ref == _versioned_record_ref(source)
            and memory.provenance.get("source_record_ref") == parent_ref
            and memory.meta.get("supporting_claim_refs") == expected_support
            and memory.provenance.get("supporting_claim_refs") == expected_support
        )
    # Only the historical default-origin format is compatible without an
    # exact versioned ref.  Partial/corrupted new lineage is not legacy data.
    lineage_keys = {"source_record_ref", "supporting_claim_refs", "projection_lineage_schema"}
    return source.source_id == "default" and not any(
        key in container for container in (memory.meta, memory.provenance) for key in lineage_keys
    )


def _refresh_replaces_projection(memory: RecordEnvelope, source: RecordEnvelope) -> bool:
    """A refresh transaction may replace only the exact projection it retired."""
    run_id = source.meta.get("refresh_run_id")
    return (
        source.kind == "knowledge_page" and source.source == "eimemory.knowledge.refresh"
        and source.meta.get("refresh_state") == "recompiled" and bool(run_id)
        and memory.kind == "memory" and memory.source == PROJECTOR_SOURCE
        and memory.status == "deprecated" and memory.scope == source.scope
        and memory.source_id == source.source_id
        and memory.meta.get("projection_type") == PROJECTION_TYPE
        and memory.meta.get("source_record_id") == source.record_id
        and memory.meta.get("source_record_kind") == source.kind
        and memory.meta.get("retired_reason") == "knowledge_refresh"
        and memory.meta.get("refresh_run_id") == run_id
        and source.content.get("replaces_projection_ref") == _versioned_record_ref(memory)
    )


def _validated_supporting_claims(record: RecordEnvelope, *, store) -> tuple[tuple[RecordEnvelope, ...], str]:
    # Only exact, versioned producer lineage can authorize page projection.
    refs = record.content.get("supporting_claim_refs")
    if (
        record.content.get("supporting_claim_refs_schema") != SUPPORT_LINEAGE_SCHEMA
        or not isinstance(refs, list)
        or not refs
        or len(refs) > MAX_SUPPORTING_CLAIMS
        or store is None
    ):
        return (), "missing_support_lineage"
    expected_fields = {"record_id", "kind", "scope", "source_id", "version_digest"}
    expected_scope = {"tenant_id", "agent_id", "workspace_id", "user_id"}
    claims: list[RecordEnvelope] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != expected_fields:
            return (), "invalid_support_lineage"
        scope_payload = ref.get("scope")
        record_id = ref.get("record_id")
        version = ref.get("version_digest")
        if (
            ref.get("kind") != "claim_card"
            or not isinstance(record_id, str) or not record_id
            or record_id in seen
            or not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version)
            or not isinstance(scope_payload, dict) or set(scope_payload) != expected_scope
            or any(not isinstance(value, str) for value in scope_payload.values())
        ):
            return (), "invalid_support_lineage"
        try:
            scope = ScopeRef.from_dict(scope_payload)
        except (TypeError, ValueError):
            return (), "invalid_support_lineage"
        if scope != record.scope or ref.get("source_id") != record.source_id:
            return (), "support_scope_source_mismatch"
        claim = store.get_by_exact_ref(record_id, scope=scope, source_id=record.source_id)
        if claim is None:
            return (), "support_missing"
        if _versioned_record_ref(claim) != ref:
            return (), "support_changed"
        if claim.kind != "claim_card" or _is_blocked_source(claim):
            return (), "unsafe_support"
        if _claim_confidence(claim) < MIN_CLAIM_CONFIDENCE:
            return (), "low_support_confidence"
        seen.add(record_id)
        claims.append(claim)
    ids = record.content.get("supporting_claim_ids")
    if (not isinstance(ids, (list, tuple))
            or any(not isinstance(item, str) or not item for item in ids)
            or set(ids) != seen or len(ids) != len(seen)):
        return (), "support_ids_mismatch"
    return tuple(claims), ""


def _claim_confidence(record: RecordEnvelope) -> float:
    return _number(*_declared_confidences(record))


def _is_blocked_source(record: RecordEnvelope) -> bool:
    if record.status != "active":
        return True
    return any(
        bool(container.get(key))
        for container in (record.meta, record.content, record.provenance)
        for key in ("deprecated", "contradiction_ids", "contradiction_claim_ids", "conflict")
    )


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


def _projected_tags(source: RecordEnvelope) -> list[str]:
    tags = ["projected", "operational", "knowledge", source.kind]
    for tag in source.tags:
        if tag not in tags:
            tags.append(tag)
    return tags
