from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope


_MAX_ANCHOR_TERMS = 52
_TOKEN_RE = re.compile(
    r"""(
        [A-Za-z]+\d+(?:[-_]\d+)* |
        v\d+(?:\.\d+)? |
        [A-Za-z]{2,}(?:[0-9._-][A-Za-z0-9._-]*)? |
        \d+(?:\.\d+)? |
        [\u4e00-\u9fff]{2,}
    )""",
    re.IGNORECASE | re.VERBOSE,
)

VALID_LANES = {"primary", "knowledge", "raw", "operational", "news"}
VALID_VISIBILITIES = {"default", "evidence_only", "report_only", "hidden"}


@dataclass(frozen=True, slots=True)
class RecallIndexDocument:
    record_id: str
    kind: str
    scope: dict[str, str]
    lane: str
    visibility: str
    source_class: str
    memory_type: str
    projection_type: str
    title_text: str
    body_text: str
    anchor_terms: tuple[str, ...]
    updated_at: str
    quality_score: float

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["anchor_terms"] = tuple(payload["anchor_terms"])
        return payload


def classify_recall_lane(record: RecordEnvelope) -> str:
    kind = str(record.kind or "").strip().lower()
    source_class = classify_source_class(record)
    projection_type = _projection_type(record)

    if kind == "raw_chunk":
        return "raw"
    if kind in {"knowledge_page", "claim_card", "paper_source", "paper_extract", "entity_record", "relation_record"}:
        return "knowledge"
    if kind == "news" or "news" in source_class or "news" in str(record.source or "").lower():
        return "news"
    if kind == "reflection":
        return "operational"
    if kind in {"incident", "replay_result", "feedback", "unknown", "recall_view"}:
        return "operational"

    memory_type = _memory_type(record)
    if kind in {"memory", "rule"} or memory_type == "living_posture":
        if projection_type == "event_memory":
            return "primary"
        if projection_type == "operational_knowledge":
            return "operational"
        if source_class == "agent_outcome" and _looks_like_actionable_memory(_combined_record_text(record)):
            return "primary"
        if source_class in {"tool_call", "agent_outcome", "diagnostic", "deployment", "operational_projection"}:
            return "operational"
        return "primary"

    if source_class in {"tool_call", "agent_outcome", "diagnostic", "deployment", "operational_projection"}:
        return "operational"

    return "primary"


def classify_recall_visibility(record: RecordEnvelope) -> str:
    kind = str(record.kind or "").strip().lower()
    lane = classify_recall_lane(record)
    source_class = classify_source_class(record)
    is_report = _is_report_friendly_record(record)

    if kind == "raw_chunk":
        return "evidence_only"
    if lane == "knowledge":
        return "default"
    if kind in {"incident", "replay_result", "feedback", "unknown", "reflection", "recall_view"}:
        return "report_only"
    if lane == "news":
        return "report_only" if _news_looks_like_digest_report(record) else "default"
    if lane == "primary":
        return "default"

    if source_class in {"tool_call", "agent_outcome", "diagnostic", "deployment"}:
        return "report_only" if is_report else "evidence_only"

    if is_report and lane == "operational":
        return "report_only"
    return "default"


def classify_source_class(record: RecordEnvelope) -> str:
    source = str(record.source or "").strip().lower()
    kind = str(record.kind or "").strip().lower()
    title = str(record.title or "").strip().lower()
    text = _record_text(record).lower()
    projection_type = _projection_type(record)
    memory_type = _memory_type(record)

    if _is_serialized_tool_call(text):
        return "tool_call"
    if _looks_like_agent_outcome_record(source=source, title=title, text=_combined_record_text(record).lower()):
        return "agent_outcome"
    if projection_type == "event_memory":
        return "event_memory"
    if projection_type == "operational_knowledge":
        return "operational_projection"
    if any(term in source or term in title for term in ("diagnostic", "health", "traceback", "panic")):
        return "diagnostic"
    if any(term in source for term in ("deploy", "deployment")) or any(
        term in text for term in ("release=/opt", "/opt/eimemory/releases")
    ):
        return "deployment"
    if kind == "news" or "news" in source or "daily_brief" in source:
        return "news"
    if "knowledge" in source or kind in {"knowledge_page", "claim_card"}:
        return "knowledge"
    if memory_type in {"preference", "rule", "policy", "living_posture"}:
        return memory_type
    return "default"


def build_recall_index_document(record: RecordEnvelope) -> RecallIndexDocument:
    lane = classify_recall_lane(record)
    visibility = classify_recall_visibility(record)
    source_class = classify_source_class(record)
    memory_type = _memory_type(record)
    projection_type = _projection_type(record)
    title_text = str(record.title or "")
    body_text = " ".join(
        part
        for part in (
            str(record.summary or ""),
            str(record.detail or ""),
            str(record.content.get("text", "")),
            str(record.content.get("excerpt", "")),
        )
        if part
    )
    if not body_text:
        body_text = " ".join(part for part in (title_text, _record_text(record)) if part).strip()
    anchor_terms = _anchor_terms(record=record, text=body_text)

    return RecallIndexDocument(
        record_id=str(record.record_id or ""),
        kind=str(record.kind or ""),
        scope=_scope_dict(record),
        lane=lane,
        visibility=visibility,
        source_class=source_class,
        memory_type=memory_type,
        projection_type=projection_type,
        title_text=title_text,
        body_text=body_text,
        anchor_terms=anchor_terms,
        updated_at=str(record.time.updated_at),
        quality_score=_quality_score(record),
    )


def _memory_type(record: RecordEnvelope) -> str:
    meta = business_metadata(record.meta)
    return str(meta.get("memory_type") or record.content.get("memory_type") or "").strip().lower()


def _projection_type(record: RecordEnvelope) -> str:
    meta = business_metadata(record.meta)
    return str(
        meta.get("projection_type")
        or record.provenance.get("projection_type")
        or record.content.get("projection_type")
        or ""
    ).strip().lower()


def _is_serialized_tool_call(text: str) -> bool:
    compact = re.sub(r"\s+", "", str(text or "").lower())
    return (
        compact.startswith('{"type":"toolcall"')
        or ('"type":"toolcall"' in compact and '"arguments"' in compact)
        or ('"name":"message"' in compact and '"arguments"' in compact and '"input"' in compact)
    )


def _looks_like_agent_outcome_record(*, source: str, title: str, text: str) -> bool:
    if title == "openclaw agent outcome":
        return True
    if source != "openclaw.agent_end":
        return False
    return any(
        marker in text
        for marker in (
            "openclaw agent outcome",
            "agent outcome summary",
            "agent execution failed",
            "agent_end_failure",
        )
    )


def _record_text(record: RecordEnvelope) -> str:
    parts: list[str] = []
    for key in ("text", "memory_type", "raw_text", "notes", "question", "answer"):
        if value := record.content.get(key):
            parts.append(str(value))
    return " ".join(parts).strip()


def _combined_record_text(record: RecordEnvelope) -> str:
    return " ".join(
        part
        for part in [
            str(record.title or ""),
            str(record.summary or ""),
            str(record.detail or ""),
            _record_text(record),
        ]
        if part
    ).strip()


def _looks_like_actionable_memory(text: str) -> bool:
    value = str(text or "").lower()
    if re.search(r"以后.+先.+再", value):
        return True
    actionable_terms = (
        "长期记忆",
        "以后",
        "先对",
        "逐条验收",
        "验收清单",
        "交付要求",
        "硬规则",
        "偏好",
        "不要",
        "必须",
        "优先",
    )
    return sum(1 for term in actionable_terms if term in value) >= 2


def _scope_dict(record: RecordEnvelope) -> dict[str, str]:
    scope = asdict(record.scope)
    return {key: str(value) for key, value in scope.items()}


def _quality_score(record: RecordEnvelope) -> float:
    meta = business_metadata(record.meta)
    quality = meta.get("quality") if isinstance(meta.get("quality"), dict) else {}
    if isinstance(quality, dict):
        score = quality.get("salience_score")
        if score is None:
            score = quality.get("importance")
        try:
            return float(score or 0.0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _split_terms(text: str) -> list[str]:
    raw_terms = _TOKEN_RE.findall(text.lower())
    terms = []
    for value in raw_terms:
        term = str(value or "").strip().lower()
        if not term:
            continue
        terms.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", term):
            terms.extend(
                chunk
                for chunk in (term[index : index + 2] for index in range(0, len(term) - 1, 2))
                if len(chunk) == 2 and chunk != term
            )
    return terms


def _anchor_terms(*, record: RecordEnvelope, text: str) -> tuple[str, ...]:
    terms = [
        str(record.kind or ""),
        str(record.source or ""),
        str(record.title or ""),
        _memory_type(record),
        _projection_type(record),
        *[str(tag) for tag in record.tags],
    ]
    terms.extend(_split_terms(str(text).lower()))
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        normalized = str(term or "").strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= _MAX_ANCHOR_TERMS:
            break
    return tuple(result)


def _is_report_friendly_record(record: RecordEnvelope) -> bool:
    source = str(record.source or "").lower()
    text = _record_text(record).lower()
    title = str(record.title or "").lower()
    summary = str(record.summary or "").lower()
    memory_type = _memory_type(record)
    projection_type = _projection_type(record)
    markers = tuple(
        marker
        for marker in (
            "report",
            "audit",
            "incident",
            "reflection",
            "summary",
            "evaluation",
            "metric",
            projection_type,
        )
        if marker
    )
    return any(
        marker in title or marker in summary or marker in text or marker == memory_type
        for marker in markers
    ) or "report" in source


def _news_looks_like_digest_report(record: RecordEnvelope) -> bool:
    source = str(record.source or "").lower()
    if "digest" in source:
        return True
    text = _record_text(record).lower()
    combined = f"{str(record.title or '')} {str(record.summary or '')} {text}".lower()
    return "digest" in combined or "report" in combined
