from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date as date_type
from datetime import datetime
from datetime import timedelta
from email.utils import parsedate_to_datetime
import html
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from eimemory.core.clock import now_iso
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.knowledge.evidence_gate import current_evidence_reasons, grade_research_evidence
from eimemory.models.records import RecordEnvelope

DAILY_BRIEF_SCHEMA_VERSION = 1
DAILY_BRIEF_SOURCE_LINEAGE_SCHEMA = "daily_brief_sources.v1"
MAX_DAILY_BRIEF_SOURCE_REFS = 2500
RESEARCH_DIGEST_KINDS = {"paper_source", "knowledge_page"}
NEWS_DIGEST_KINDS = {"news"}
EXPERIENCE_SOURCES = {
    "openclaw.agent_end",
    "openclaw.message_received",
}


def build_daily_brief(
    records: Iterable[RecordEnvelope | Mapping[str, Any]],
    *,
    date: str | date_type,
    research_lookback_days: int = 0,
    store=None,
) -> dict[str, Any]:
    """Build a JSON-serializable daily brief from already available records."""
    day = _date_string(date)
    inputs = [(record, _record_payload(record)) for record in records]
    all_records = [payload for _, payload in inputs]
    day_records = [record for record in all_records if _is_on_day(record, day)]
    day_records.sort(key=_record_sort_key)

    digest_records = [
        record
        for record in all_records
        if _is_research_digest_record(record)
        and _is_within_research_window(record, day, research_lookback_days)
    ]
    news_records = [
        record
        for record in all_records
        if _is_news_digest_record(record)
        and _is_news_within_window(record, day, research_lookback_days)
    ]
    # Every body section uses the same current-evidence boundary. A research
    # record rejected from the digest must not reappear as a followup or as an
    # already projected memory. Health retains only status/count diagnostics.
    body_candidates = list({id(record): record for record in
                            [*day_records, *digest_records, *news_records]}.values())
    allowed, body_excluded = _apply_evidence_gate(body_candidates, store=store)
    allowed_ids = {id(record) for record in allowed}
    current_day_records = [record for record in day_records if id(record) in allowed_ids]
    experience_records = [record for record in current_day_records if _is_new_memory_record(record)]
    digest_records = [record for record in digest_records if id(record) in allowed_ids]
    digest_records.sort(key=_record_sort_key)
    news_records = [record for record in news_records if id(record) in allowed_ids]
    news_records.sort(key=_record_sort_key)
    news_records = _dedupe_records_by_url(news_records)

    decisions = _extract_marked_items(_marked_records(
        experience_records,
        markers=("decision:", "decided:", "决策", "决定"),
    ))
    followup_records = _marked_records(
        current_day_records,
        markers=("follow up:", "follow-up:", "todo:", "next:", "待跟进", "跟进"),
    )
    followups = _extract_marked_items(followup_records)
    contributors = {id(record) for record in
                    [*experience_records, *digest_records, *news_records, *followup_records]}
    source_lineage = _source_lineage([record for record, payload in inputs
                                    if id(payload) in contributors])

    return {
        "ok": True,
        "date": day,
        "generated_at": now_iso(),
        "daily_brief_schema_version": DAILY_BRIEF_SCHEMA_VERSION,
        "conversation_summary": _conversation_summary(experience_records),
        "decisions": decisions,
        "new_memories": [_brief_record(record) for record in experience_records],
        "research_digest": {
            "count": len(digest_records),
            "items": [_research_item(record) for record in digest_records],
        },
        "news_digest": {
            "count": len(news_records),
            "items": [_news_item(record) for record in news_records],
        },
        "followups": followups,
        "source_health": _source_health(day_records, evidence_excluded=body_excluded),
        **source_lineage,
    }


def build_daily_brief_delivery_payload(
    brief: Mapping[str, Any],
    *,
    channel: str = "feishu",
) -> dict[str, Any]:
    """Prepare an operator-readable delivery artifact without performing network delivery."""
    safe_brief = _json_safe(dict(brief))
    day = str(safe_brief.get("date") or "")
    prepared_at = now_iso()
    return {
        "ok": True,
        "channel": str(channel or "unknown"),
        "network_called": False,
        "prepared_at": prepared_at,
        "outbox": {
            "kind": "daily_brief",
            "status": "prepared",
            "channel": str(channel or "unknown"),
            "title": f"Daily brief {day}".strip(),
            "body": safe_brief,
        },
        "audit": {
            "action": "daily_brief.prepared",
            "status": "prepared",
            "channel": str(channel or "unknown"),
            "date": day,
            "prepared_at": prepared_at,
        },
    }


def _is_on_day(record: RecordEnvelope | Mapping[str, Any], day: str) -> bool:
    payload = _record_payload(record)
    time_payload = payload.get("time") if isinstance(payload.get("time"), dict) else {}
    for key in ("occurred_at", "created_at", "updated_at"):
        value = str(time_payload.get(key) or "")
        if value.startswith(day):
            return True
    return False


def _is_within_research_window(record: Mapping[str, Any], day: str, lookback_days: int) -> bool:
    if _is_on_day(record, day):
        return True
    lookback_days = max(0, int(lookback_days))
    if lookback_days == 0:
        return False
    target_day = _parse_day(day)
    record_day = _record_day(record)
    if target_day is None or record_day is None:
        return False
    return target_day - timedelta(days=lookback_days) <= record_day < target_day


def _is_news_within_window(record: Mapping[str, Any], day: str, lookback_days: int) -> bool:
    target_day = _parse_day(day)
    published_day = _news_published_day(record)
    if target_day is None:
        return False
    if published_day is None:
        return _is_within_research_window(record, day, lookback_days)
    if published_day > target_day:
        return False
    lookback_days = max(0, int(lookback_days))
    return target_day - timedelta(days=lookback_days) <= published_day <= target_day


def _record_payload(record: RecordEnvelope | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(record, RecordEnvelope):
        return record.to_dict()
    return _json_safe(dict(record))


def _is_new_memory_record(record: Mapping[str, Any]) -> bool:
    kind = str(record.get("kind") or "")
    source = str(record.get("source") or "")
    return source in EXPERIENCE_SOURCES or kind == "memory"


def _is_research_digest_record(record: Mapping[str, Any]) -> bool:
    kind = str(record.get("kind") or "")
    source = str(record.get("source") or "").lower()
    tags = {str(tag).lower() for tag in record.get("tags") or []}
    if kind in RESEARCH_DIGEST_KINDS:
        return True
    return "research_digest" in tags or "digest" in source


def _is_news_digest_record(record: Mapping[str, Any]) -> bool:
    kind = str(record.get("kind") or "")
    source = str(record.get("source") or "").lower()
    if kind in NEWS_DIGEST_KINDS:
        return True
    return kind == "knowledge_candidate" and "news.collect" in source


def _conversation_summary(records: list[Mapping[str, Any]]) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    highlights: list[str] = []
    for record in records:
        source = str(record.get("source") or "unknown")
        by_source[source] = by_source.get(source, 0) + 1
        text = _record_text(record)
        if text:
            highlights.append(text)
    return {
        "message_count": len(records),
        "by_source": dict(sorted(by_source.items())),
        "highlights": highlights[:5],
    }


def _marked_records(
    records: list[Mapping[str, Any]],
    *,
    markers: tuple[str, ...],
) -> list[Mapping[str, Any]]:
    marked: list[Mapping[str, Any]] = []
    for record in records:
        text = _record_text(record)
        lowered = text.lower()
        if text and any(marker in lowered or marker in text for marker in markers):
            marked.append(record)
    return marked


def _extract_marked_items(records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"record_id": str(record.get("record_id") or ""),
             "title": str(record.get("title") or ""),
             "source": str(record.get("source") or ""),
             "text": _record_text(record)} for record in records]


def _source_lineage(records: list[RecordEnvelope | Mapping[str, Any]]) -> dict[str, Any]:
    # Native Runtime input is authoritative RecordEnvelope data. The pure
    # mapping helper remains usable, but cannot certify a versioned source it
    # did not read. Empty native briefs have a complete empty declaration.
    if any(not isinstance(record, RecordEnvelope) for record in records):
        return {}
    refs = {}
    for record in records:
        ref = versioned_record_ref(record)
        key = (*ref["scope"].values(), ref["source_id"], ref["record_id"])
        if key in refs and refs[key] != ref:
            raise ValueError("daily_brief_source_version_conflict")
        refs[key] = ref
        if len(refs) > MAX_DAILY_BRIEF_SOURCE_REFS:
            raise ValueError("daily_brief_source_reference_limit")
    return {"source_lineage_schema": DAILY_BRIEF_SOURCE_LINEAGE_SCHEMA,
            "source_record_refs": [refs[key] for key in sorted(refs)]}


def _brief_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "record_id": str(record.get("record_id") or ""),
        "kind": str(record.get("kind") or ""),
        "title": str(record.get("title") or ""),
        "summary": str(record.get("summary") or ""),
        "source": str(record.get("source") or ""),
        "occurred_at": str((record.get("time") or {}).get("occurred_at") or ""),
    }


def _research_item(record: Mapping[str, Any]) -> dict[str, Any]:
    content = record.get("content") if isinstance(record.get("content"), dict) else {}
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    gate = grade_research_evidence(record)
    return {
        "record_id": str(record.get("record_id") or ""),
        "kind": str(record.get("kind") or ""),
        "title": str(record.get("title") or ""),
        "summary": str(record.get("summary") or ""),
        "source": str(record.get("source") or ""),
        "url": str(
            content.get("canonical_url")
            or content.get("source_url")
            or content.get("url")
            or content.get("uri")
            or meta.get("source_uri")
            or meta.get("source_url")
            or ""
        ),
        "published_at": gate["published_at"],
        "evidence_gate": gate,
    }


def _news_item(record: Mapping[str, Any]) -> dict[str, Any]:
    content = record.get("content") if isinstance(record.get("content"), dict) else {}
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    title = _clean_digest_text(str(record.get("title") or content.get("title") or ""))
    raw_summary = str(record.get("summary") or content.get("summary") or "")
    summary = _clean_digest_text(raw_summary)
    if _looks_like_broken_html(summary) or ("<" in raw_summary and not summary):
        summary = _clean_digest_text(str(content.get("title") or title))
    gate = grade_research_evidence(record)
    return {
        "record_id": str(record.get("record_id") or ""),
        "kind": str(record.get("kind") or ""),
        "title": title,
        "summary": summary,
        "source": str(record.get("source") or ""),
        "url": str(content.get("item_url") or content.get("source_url") or content.get("url") or meta.get("item_url") or meta.get("source_url") or ""),
        "published_at": str(content.get("published_at") or ""),
        "evidence_gate": gate,
    }


def _dedupe_records_by_url(records: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    deduped: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        key = _record_url(record) or str(record.get("record_id") or "")
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped


def _record_url(record: Mapping[str, Any]) -> str:
    content = record.get("content") if isinstance(record.get("content"), dict) else {}
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    return str(content.get("item_url") or content.get("url") or meta.get("item_url") or "")


def _clean_digest_text(value: str) -> str:
    """Normalize RSS snippets so daily brief output is readable text, not HTML."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"<[^>]*$", " ", text)
    return " ".join(text.split())


def _looks_like_broken_html(value: str) -> bool:
    return "<" in value or ">" in value or value.lower().startswith(("http://", "https://"))


def _apply_evidence_gate(records: list[Mapping[str, Any]], *, store=None) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    accepted: list[Mapping[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for record, parent_reason in zip(records, current_evidence_reasons(records, store=store)):
        reasons = []
        if parent_reason:
            reasons.append(parent_reason)
        if (_is_research_digest_record(record) or _is_news_digest_record(record)
                or record.get("kind") in {"claim_card", "paper_extract"}):
            reasons.extend(grade_research_evidence(record)["reasons"])
        if reasons:
            reasons = list(dict.fromkeys(reasons))
            # Exclusion diagnostics must not become another body channel.
            excluded.append({"record_id": str(record.get("record_id") or ""),
                             "reason": reasons[0], "reasons": reasons})
            continue
        accepted.append(record)
    return accepted, excluded


def _source_health(records: list[Mapping[str, Any]], *, evidence_excluded: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    by_kind: dict[str, int] = {}
    by_source: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for record in records:
        kind = str(record.get("kind") or "unknown")
        source = str(record.get("source") or "unknown")
        status = str(record.get("status") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
        by_status[status] = by_status.get(status, 0) + 1
    return {
        "record_count": len(records),
        "by_kind": dict(sorted(by_kind.items())),
        "by_source": dict(sorted(by_source.items())),
        "by_status": dict(sorted(by_status.items())),
        "evidence_gate": {
            "excluded_count": len(evidence_excluded or []),
            "excluded": list(evidence_excluded or []),
        },
    }


def _record_text(record: Mapping[str, Any]) -> str:
    content = record.get("content") if isinstance(record.get("content"), dict) else {}
    for value in (content.get("text"), record.get("summary"), record.get("detail"), record.get("title")):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _record_sort_key(record: Mapping[str, Any]) -> str:
    time_payload = record.get("time") if isinstance(record.get("time"), dict) else {}
    return str(time_payload.get("occurred_at") or time_payload.get("created_at") or "")


def _date_string(value: str | date_type) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    return str(value)[:10]


def _record_day(record: Mapping[str, Any]) -> date_type | None:
    time_payload = record.get("time") if isinstance(record.get("time"), dict) else {}
    for key in ("occurred_at", "created_at", "updated_at"):
        parsed = _parse_day(str(time_payload.get(key) or ""))
        if parsed is not None:
            return parsed
    return None


def _parse_day(value: str) -> date_type | None:
    if not value:
        return None
    try:
        return date_type.fromisoformat(value[:10])
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError, IndexError):
        return None


def _news_published_day(record: Mapping[str, Any]) -> date_type | None:
    content = record.get("content") if isinstance(record.get("content"), dict) else {}
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    for value in (
        content.get("published_at"),
        content.get("published"),
        meta.get("published_at"),
        provenance.get("published_at"),
    ):
        parsed = _parse_day(str(value or ""))
        if parsed is not None:
            return parsed
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted((_json_safe(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
