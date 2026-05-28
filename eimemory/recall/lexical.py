from __future__ import annotations

from dataclasses import dataclass
import re


_MAX_ADJUSTMENT = 0.18
_CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
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
_VERSION_RE = re.compile(r"^v\d+(?:\.\d+)?$", re.IGNORECASE)
_PHRASE_RE = re.compile(r"[\"']([^\"']+)[\"']")


@dataclass(frozen=True)
class LexicalSignal:
    score: float
    exact_phrase_hits: tuple[str, ...]
    entity_hits: tuple[str, ...]
    version_hits: tuple[str, ...]
    token_hits: tuple[str, ...]
    suppression_reason: str


def analyze_lexical_signal(
    query: str,
    record_text: str,
    *,
    record_kind: str = "",
    record_source: str = "",
    recall_filters: dict | None = None,
) -> LexicalSignal:
    query_text = _clean_text(query)
    if not query_text:
        return _empty_signal("empty_query", record_kind, "", recall_filters)

    normalized_record = _clean_text(record_text)
    if not normalized_record:
        return _empty_signal("empty_record_text", record_kind, "", recall_filters)

    query_terms = _extract_terms(query_text)
    if not query_terms:
        return _empty_signal("unparseable_query_terms", record_kind, "", recall_filters)

    record_terms = set(_extract_terms(normalized_record))
    exact_phrase_hits = _dedupe(
        [
            phrase
            for phrase in [
                *query_terms,
                *_extract_phrase_terms(query_text),
            ]
            if phrase and phrase in normalized_record and len(phrase) >= 2
        ]
    )
    version_hits = _dedupe(
        [term for term in query_terms if _VERSION_RE.match(term) and term in normalized_record]
    )
    entity_hits = _dedupe(
        [term for term in query_terms if _is_entity_term(term) and term in normalized_record]
    )
    entity_hits.extend(_expand_chinese_context(normalized_record, exact_phrase_hits))
    token_hits = _dedupe([term for term in query_terms if term in record_terms])
    exact_phrase_hits = _dedupe(exact_phrase_hits)
    entity_hits = _dedupe(entity_hits)
    version_hits = _dedupe(version_hits)

    score = _compute_score(
        query_terms=tuple(query_terms),
        token_hits=tuple(token_hits),
        exact_phrase_hits=tuple(exact_phrase_hits),
        entity_hits=tuple(entity_hits),
        version_hits=tuple(version_hits),
    )
    suppression_reason = _build_kind_suppression_reason(
        record_kind=record_kind,
        record_source=record_source,
        recall_filters=recall_filters,
    )

    return LexicalSignal(
        score=score,
        exact_phrase_hits=tuple(exact_phrase_hits),
        entity_hits=tuple(entity_hits),
        version_hits=tuple(version_hits),
        token_hits=tuple(token_hits),
        suppression_reason=suppression_reason,
    )


def _clean_text(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", text, flags=re.UNICODE)


def _extract_terms(text: str) -> list[str]:
    return [match.group(0).strip() for match in _TOKEN_RE.finditer(text)]


def _extract_phrase_terms(text: str) -> list[str]:
    quoted = [match.group(1).strip().lower() for match in _PHRASE_RE.finditer(text)]
    return [term for term in quoted if term]


def _dedupe(values: list[str] | tuple[str, ...]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _is_entity_term(value: str) -> bool:
    lowered = str(value or "").lower()
    if len(lowered) < 2:
        return False
    return _is_chinese(value) or lowered.isupper() or any(char.isdigit() for char in lowered)


def _is_chinese(value: str) -> bool:
    return bool(_CHINESE_RE.search(value or ""))


def _expand_chinese_context(text: str, exact_phrase_hits: list[str]) -> list[str]:
    entities: list[str] = []
    for phrase in exact_phrase_hits:
        if not _is_chinese(phrase):
            continue
        start = 0
        while True:
            index = text.find(phrase, start)
            if index < 0:
                break
            end = index + len(phrase)
            left = index - 1
            while left >= 0 and _is_chinese(text[left]) and text[left].strip():
                left -= 1
            right = end
            while right < len(text) and _is_chinese(text[right]) and text[right].strip():
                right += 1
            context = text[left + 1 : right].strip()
            if len(context) >= 2:
                entities.append(context)
            start = end
    return _dedupe(entities)


def _compute_score(
    *,
    query_terms: tuple[str, ...],
    token_hits: tuple[str, ...],
    exact_phrase_hits: tuple[str, ...],
    entity_hits: tuple[str, ...],
    version_hits: tuple[str, ...],
) -> float:
    query_count = len(query_terms)
    if not query_count:
        return 0.0

    token_rate = len(token_hits) / query_count
    phrase_rate = len(exact_phrase_hits) / query_count
    entity_rate = min(1.0, len(entity_hits) / max(1, min(4, query_count)))
    version_total = sum(1 for term in query_terms if _VERSION_RE.match(term))
    version_rate = len(version_hits) / max(1, version_total)
    match = (0.55 * token_rate) + (0.25 * phrase_rate) + (0.10 * entity_rate) + (0.10 * version_rate)
    return round(max(0.0, min(_MAX_ADJUSTMENT, match * _MAX_ADJUSTMENT)), 4)


def _build_kind_suppression_reason(record_kind: str, record_source: str, recall_filters: dict | None) -> str:
    intent_name = str((recall_filters or {}).get("intent_name") or (recall_filters or {}).get("intent") or "").strip().lower()
    if not intent_name or intent_name == "research":
        return ""
    kind = str(record_kind or "").strip().lower()
    if kind != "knowledge_page":
        return ""
    if intent_name not in {"project_delivery", "operator_preference", "living_posture"}:
        return ""
    return f"intent:{intent_name} downweights kind={kind}; source={str(record_source or '').strip().lower()}"


def _empty_signal(
    suppression_reason_key: str,
    record_kind: str,
    record_source: str,
    recall_filters: dict | None = None,
) -> LexicalSignal:
    return LexicalSignal(
        score=0.0,
        exact_phrase_hits=(),
        entity_hits=(),
        version_hits=(),
        token_hits=(),
        suppression_reason=_build_kind_suppression_reason(
            record_kind=record_kind,
            record_source=record_source,
            recall_filters=recall_filters,
        ) or suppression_reason_key,
    )
