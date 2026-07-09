from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eimemory.intake.closure import build_research_closure_review
from eimemory.models.records import RecordEnvelope, ScopeRef

SKIPPED_REPORT = {
    "ok": False,
    "paper_source_id": "",
    "extracted_record_count": 0,
    "compiled_record_count": 0,
    "record_ids": [],
}

UNSAFE_DECISIONS = {"quarantined", "rejected"}
PAPER_SOURCE_KINDS = {"arxiv", "doi", "pdf", "url", "paper", "chatpaper_arxiv"}
COLLECTED_PAPER_SOURCE_KINDS = {"arxiv", "doi", "chatpaper_arxiv", "url"}
UNSAFE_SAFETY_KEYS = {"prompt_injection", "secret", "secret_detected", "content_redacted"}


@dataclass(slots=True)
class PaperIntakePipeline:
    runtime: Any

    def promote(self, candidate_record_or_dict: RecordEnvelope | dict[str, Any], scope: dict[str, Any] | None) -> dict[str, Any]:
        candidate = _candidate_payload(candidate_record_or_dict)
        if isinstance(candidate_record_or_dict, RecordEnvelope):
            if candidate_record_or_dict.kind != "knowledge_candidate":
                return _skip("not_a_knowledge_candidate")
            if scope is not None and not _same_scope(candidate_record_or_dict.scope, ScopeRef.from_dict(scope)):
                return _skip("scope_mismatch")
        skipped_reason = _skipped_reason(candidate_record_or_dict, candidate)
        if skipped_reason:
            return _skip(skipped_reason)

        paper_input = _paper_input_from_candidate(candidate)
        if not paper_input:
            return _skip("not_a_paper_candidate")

        source_record = self.runtime.ingest_paper_source(paper_input, scope=scope)
        extraction_input = {
            **paper_input,
            "paper_source_id": source_record.record_id,
            "provenance": {
                **dict(paper_input.get("provenance") or {}),
                "paper_source_id": source_record.record_id,
                "source": "eimemory.intake.pipeline",
            },
        }
        extraction = self.runtime.extract_paper_memory(extraction_input, scope=scope)
        compilation = self.runtime.compile_paper_knowledge(extraction=extraction, scope=scope)
        extracted_records = extraction.to_records(scope=scope)
        compiled_records = compilation.to_records(scope=scope)
        record_ids = [source_record.record_id]
        record_ids.extend(record.record_id for record in extracted_records)
        record_ids.extend(record.record_id for record in compiled_records)

        report = {
            "ok": True,
            "paper_source_id": source_record.record_id,
            "extracted_record_count": len(extracted_records),
            "compiled_record_count": len(compiled_records),
            "skipped_reason": "",
            "record_ids": _dedupe(record_ids),
        }
        closure_review = build_research_closure_review(
            self.runtime,
            candidate_record_or_dict,
            report,
            scope=scope,
            persist=True,
        )
        if closure_review.get("record_id"):
            report["record_ids"] = _dedupe([*report["record_ids"], str(closure_review["record_id"])])
        report["closure_review"] = closure_review
        report["closure_review_record_id"] = str(closure_review.get("record_id") or "")
        report["closure_decision"] = str(closure_review.get("decision") or "")
        if isinstance(candidate_record_or_dict, RecordEnvelope):
            _mark_paper_promoted(self.runtime, candidate_record_or_dict, report)
        return report


def promote_paper_candidate(
    runtime: Any,
    candidate_record_or_dict: RecordEnvelope | dict[str, Any],
    scope: dict[str, Any] | None,
) -> dict[str, Any]:
    return PaperIntakePipeline(runtime).promote(candidate_record_or_dict, scope)


def promote_collected_paper_candidates(
    runtime: Any,
    scope: dict[str, Any] | None,
    *,
    limit: int = 100,
    auto: bool = False,
) -> dict[str, Any]:
    records = [
        *runtime.store.list_records(kinds=["knowledge_candidate"], scope=scope, status="candidate", limit=limit),
        *runtime.store.list_records(kinds=["knowledge_candidate"], scope=scope, status="reviewed", limit=limit),
    ][: max(0, int(limit))]
    reasons: dict[str, int] = {}
    promoted_reports: list[dict[str, Any]] = []
    skipped_reports: list[dict[str, str]] = []
    promoted = 0
    skipped = 0

    for record in records:
        candidate = _candidate_payload(record)
        reason = _collected_candidate_skip_reason(candidate)
        if reason:
            skipped += 1
            _count_reason(reasons, reason)
            skipped_reports.append({"record_id": record.record_id, "reason": reason})
            continue
        if not auto:
            skipped += 1
            _count_reason(reasons, "auto_disabled")
            skipped_reports.append({"record_id": record.record_id, "reason": "auto_disabled"})
            continue
        report = runtime.promote_paper_candidate(record, scope=scope)
        if report.get("ok"):
            promoted += 1
            promoted_reports.append({"record_id": record.record_id, **report})
            continue
        reason = str(report.get("skipped_reason") or "promotion_skipped")
        skipped += 1
        _count_reason(reasons, reason)
        skipped_reports.append({"record_id": record.record_id, "reason": reason})

    return {
        "ok": True,
        "auto": bool(auto),
        "scanned": len(records),
        "promoted": promoted,
        "skipped": skipped,
        "closure_review_count": sum(
            1 for report in promoted_reports if str(report.get("closure_review_record_id") or "")
        ),
        "reasons": reasons,
        "promoted_reports": promoted_reports,
        "skipped_reports": skipped_reports,
    }


def _skip(reason: str) -> dict[str, Any]:
    return {**SKIPPED_REPORT, "skipped_reason": reason}


def _mark_paper_promoted(runtime: Any, record: RecordEnvelope, report: dict[str, Any]) -> None:
    from eimemory.intake.review import mark_candidate_paper_promoted

    mark_candidate_paper_promoted(runtime, record, report)


def _candidate_payload(candidate_record_or_dict: RecordEnvelope | dict[str, Any]) -> dict[str, Any]:
    if isinstance(candidate_record_or_dict, RecordEnvelope):
        payload = dict(candidate_record_or_dict.content or {})
        payload.setdefault("record_id", candidate_record_or_dict.record_id)
        payload.setdefault("status", candidate_record_or_dict.status)
        payload.setdefault("title", candidate_record_or_dict.title)
        payload.setdefault("summary", candidate_record_or_dict.summary)
        payload.setdefault("content_excerpt", candidate_record_or_dict.detail)
        payload.setdefault("provenance", dict(candidate_record_or_dict.provenance or {}))
        payload.setdefault("meta", dict(candidate_record_or_dict.meta or {}))
        return payload
    return dict(candidate_record_or_dict or {})


def _skipped_reason(candidate_record_or_dict: RecordEnvelope | dict[str, Any], candidate: dict[str, Any]) -> str:
    status = ""
    if isinstance(candidate_record_or_dict, RecordEnvelope):
        status = str(candidate_record_or_dict.status or "").strip().lower()
    status = str(candidate.get("status") or status).strip().lower()
    decision = str(candidate.get("decision") or candidate.get("intake_decision") or "").strip().lower()
    if status in UNSAFE_DECISIONS or decision in UNSAFE_DECISIONS:
        return f"{status or decision}_candidate"
    meta = candidate.get("meta")
    if isinstance(meta, dict):
        meta_decision = str(meta.get("intake_decision") or "").strip().lower()
        if meta_decision in UNSAFE_DECISIONS:
            return f"{meta_decision}_candidate"
    if _has_unsafe_safety_flag(candidate):
        return "unsafe_candidate"
    return ""


def _paper_input_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    source_kind = str(candidate.get("source_kind") or "").strip().lower()
    uri = str(candidate.get("canonical_url") or candidate.get("paper_url") or candidate.get("url") or candidate.get("uri") or "").strip()
    metadata = _candidate_metadata(candidate)
    metadata_url = _metadata_text(metadata, "pdf_url")
    if not uri:
        uri = metadata_url
    paper_input = {
        "source_kind": source_kind,
        "title": _candidate_text(candidate, "title"),
        "abstract": (
            _candidate_text(candidate, "abstract")
            or _metadata_text(metadata, "translated_abstract")
            or _metadata_text(metadata, "original_abstract")
            or _candidate_text(candidate, "summary")
        ),
        "body": _candidate_text(candidate, "body") or _candidate_text(candidate, "text") or _candidate_text(candidate, "content_excerpt"),
        "authors": candidate.get("authors") if isinstance(candidate.get("authors"), list) else [],
        "venue": _candidate_text(candidate, "venue"),
        "published_at": _candidate_text(candidate, "published_at"),
        "doi": _candidate_text(candidate, "doi") or _metadata_text(metadata, "doi"),
        "arxiv_id": _candidate_text(candidate, "arxiv_id") or _metadata_text(metadata, "arxiv_id"),
        "canonical_url": uri,
        "paper_url": uri,
        "url": uri,
        "pdf_blob_ref": _candidate_text(candidate, "pdf_blob_ref"),
        "normalized_text_ref": _candidate_text(candidate, "normalized_text_ref"),
        "metadata": _paper_metadata(candidate),
        "provenance": _paper_provenance(candidate),
    }
    if source_kind == "paper":
        paper_input["source_kind"] = ""
    if not _has_paper_identity(paper_input):
        return {}
    if not _has_enough_content(paper_input):
        return {}
    return {key: value for key, value in paper_input.items() if value not in ("", [], {})}


def _candidate_text(candidate: dict[str, Any], key: str) -> str:
    value = candidate.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _candidate_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    metadata = candidate.get("metadata")
    if isinstance(metadata, dict):
        return dict(metadata)
    content = candidate.get("content")
    if isinstance(content, dict) and isinstance(content.get("metadata"), dict):
        return dict(content["metadata"])
    return {}


def _metadata_text(metadata: dict[str, Any], key: str) -> str:
    value = metadata.get(key)
    if value is None:
        return ""
    return str(value).strip()


def _paper_metadata(candidate: dict[str, Any]) -> dict[str, Any]:
    result = _candidate_metadata(candidate)
    for key in ("source_id", "fingerprint", "quality"):
        if key in candidate:
            result[key] = candidate[key]
    return result


def _paper_provenance(candidate: dict[str, Any]) -> dict[str, Any]:
    provenance = candidate.get("provenance")
    result = dict(provenance) if isinstance(provenance, dict) else {}
    for key in ("record_id", "source_id", "fingerprint"):
        if candidate.get(key):
            result[key] = candidate[key]
    result["input_kind"] = "paper_intake_pipeline"
    return result


def _has_paper_identity(paper_input: dict[str, Any]) -> bool:
    source_kind = str(paper_input.get("source_kind") or "").strip().lower()
    if source_kind and source_kind not in PAPER_SOURCE_KINDS:
        return False
    return any(str(paper_input.get(key) or "").strip() for key in ("arxiv_id", "doi", "canonical_url", "paper_url", "url", "pdf_blob_ref"))


def _has_enough_content(paper_input: dict[str, Any]) -> bool:
    title = str(paper_input.get("title") or "").strip()
    text = " ".join(
        str(paper_input.get(key) or "").strip()
        for key in ("abstract", "body")
        if str(paper_input.get(key) or "").strip()
    )
    return bool(title) and len("".join(char for char in text if char.isalnum())) >= 32


def _collected_candidate_skip_reason(candidate: dict[str, Any]) -> str:
    skipped_reason = _skipped_reason(candidate, candidate)
    if skipped_reason:
        return skipped_reason
    if _has_unsafe_safety_flag(candidate):
        return "unsafe_candidate"
    if not _is_collected_paper_like(candidate):
        return "not_paper_like"
    paper_input = _paper_input_from_candidate(candidate)
    if not paper_input:
        return "insufficient_content"
    return ""


def _is_collected_paper_like(candidate: dict[str, Any]) -> bool:
    source_kind = str(candidate.get("source_kind") or "").strip().lower()
    if source_kind not in COLLECTED_PAPER_SOURCE_KINDS:
        return False
    metadata = _candidate_metadata(candidate)
    arxiv_id = _candidate_text(candidate, "arxiv_id") or _metadata_text(metadata, "arxiv_id")
    doi = _candidate_text(candidate, "doi") or _metadata_text(metadata, "doi")
    url = str(candidate.get("canonical_url") or candidate.get("paper_url") or candidate.get("url") or candidate.get("uri") or "").strip()
    if source_kind == "url":
        return bool(arxiv_id or doi or _url_looks_like_paper_identifier(url))
    return bool(arxiv_id or doi or url)


def _has_unsafe_safety_flag(candidate: dict[str, Any]) -> bool:
    containers = [candidate, _candidate_metadata(candidate)]
    meta = candidate.get("meta")
    if isinstance(meta, dict):
        containers.append(meta)
    for container in containers:
        safety = container.get("safety") if isinstance(container, dict) else None
        if isinstance(safety, dict) and any(bool(safety.get(key)) for key in UNSAFE_SAFETY_KEYS):
            return True
    return False


def _url_looks_like_paper_identifier(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    return "arxiv.org/abs/" in lowered or "arxiv.org/pdf/" in lowered or "doi.org/10." in lowered


def _count_reason(reasons: dict[str, int], reason: str) -> None:
    reasons[reason] = reasons.get(reason, 0) + 1


def _dedupe(record_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for record_id in record_ids:
        if record_id in seen:
            continue
        seen.add(record_id)
        result.append(record_id)
    return result


def _same_scope(left: ScopeRef, right: ScopeRef) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.agent_id == right.agent_id
        and left.workspace_id == right.workspace_id
        and left.user_id == right.user_id
    )
