from __future__ import annotations

from dataclasses import dataclass
from bisect import bisect_right
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

    # Include generic synonym neighbors so paraphrase anchors (链接↔短链) can
    # match without treating dense cosine as admission evidence.
    requested = set(query_terms)
    for term in query_terms:
        requested.update(_synonym_neighbors(term))
    record_terms = _matching_record_terms(normalized_record, requested)

    def _term_hit(term: str) -> bool:
        if _term_matches_record(term, normalized_record, record_terms):
            return True
        # Synonym neighbors reuse the same token/substring rules as direct terms
        # (RC-09: short CJK and ASCII must be tokens, not raw substrings).
        return any(
            _term_matches_record(neighbor, normalized_record, record_terms)
            for neighbor in _synonym_neighbors(term)
        )

    exact_phrase_hits = _dedupe(
        [
            phrase
            for phrase in [
                *query_terms,
                *_extract_phrase_terms(query_text),
            ]
            if phrase and _term_hit(phrase) and len(phrase) >= 2
        ]
    )
    version_hits = _dedupe(
        [term for term in query_terms if _VERSION_RE.match(term) and term in record_terms]
    )
    entity_hits = _dedupe(
        [term for term in query_terms if _is_entity_term(term) and _term_hit(term)]
    )
    entity_hits.extend(_expand_chinese_context(normalized_record, exact_phrase_hits))
    token_hits = _dedupe([term for term in query_terms if term in record_terms or _term_hit(term)])
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
    # Natural how-to paraphrases dilute the denominator with interrogative
    # fillers ("应该/怎么/什么"). Rescore on content terms only; never lower
    # the raw score, and never invent hits that were not content anchors.
    content_terms = _content_query_terms(query_terms)
    if content_terms and content_terms != query_terms:
        content_hits = _dedupe([term for term in content_terms if _term_hit(term)])
        content_score = _compute_score(
            query_terms=tuple(content_terms),
            token_hits=tuple(content_hits),
            exact_phrase_hits=tuple(content_hits),
            entity_hits=tuple(term for term in content_hits if _is_entity_term(term)),
            version_hits=tuple(term for term in content_hits if _VERSION_RE.match(term)),
        )
        if content_score > score:
            score = content_score
            token_hits = _dedupe([*token_hits, *content_hits])
            exact_phrase_hits = _dedupe([*exact_phrase_hits, *content_hits])
            entity_hits = _dedupe(
                [*entity_hits, *[term for term in content_hits if _is_entity_term(term)]]
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



# Interrogative / procedure wrappers that dilute Chinese paraphrase overlap.
# Keep this generic — no product-specific entities (抖音/福建/微信).
_FILLER_TERMS = frozenset({
    "应该", "怎么", "怎样", "如何", "什么", "哪些", "哪个", "为何", "为什么", "为啥",
    "请问", "之后", "之前", "以后", "然后", "时候", "先做", "做什", "是什", "该怎",
    "该如", "该先", "何处", "何提", "何检", "么处", "可否", "能否", "可以", "需要",
    "启之", "头之", "前应", "后应", "复时", "时如", "查之", "址后", "后该", "纸应",
    "接应", "权的", "的工", "取文", "前授", "是多", "多少", "格是", "令是",
    "码是", "收到", "拿到", "提取", "处理",
})

# Bounded generic paraphrase neighbors for common procedure/link wording.
# Groups are closed; membership is exact term match only.
_SYNONYM_GROUPS = (
    frozenset({"链接", "短链", "网址", "地址", "url", "link"}),
    frozenset({"检查", "复核", "查看", "核对"}),
    frozenset({"工作", "任务"}),
    frozenset({"文案", "标题"}),
)


def _synonym_neighbors(term: str) -> frozenset[str]:
    normalized = str(term or "").strip().lower()
    if not normalized:
        return frozenset()
    for group in _SYNONYM_GROUPS:
        if normalized in group:
            return group - {normalized}
    return frozenset()


def _content_query_terms(query_terms: list[str]) -> list[str]:
    """Drop interrogative fillers and whole-question mega-tokens for rescoring."""
    content: list[str] = []
    for term in query_terms:
        if term in _FILLER_TERMS:
            continue
        # Whole natural questions tokenize as one long CJK span plus bigrams.
        if len(term) > 8 and _is_chinese(term):
            continue
        content.append(term)
    return _dedupe(content)


_CLEAN_TEXT_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def _clean_text(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return _CLEAN_TEXT_RE.sub(" ", text)


def _extract_terms(text: str) -> list[str]:
    terms: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        term = match.group(0).strip()
        if not term:
            continue
        terms.append(term)
        terms.extend(_split_chinese_compound(term))
    return _dedupe(terms)


def _matching_record_terms(text: str, requested: set[str]) -> set[str]:
    """Find only requested token memberships, without allocating all bigrams.

    Chinese substring evidence is evaluated separately. Token membership still
    requires a full regex token or a two-character compound chunk, exactly as
    `_extract_terms` does. Large queries retain the linear full-token path.
    """
    if len(requested) > 64:
        return set(_extract_terms(text)) & requested
    remaining = set(requested)
    found: set[str] = set()
    chinese_pairs = {term for term in requested if len(term) == 2 and _is_chinese(term)}
    for match in _TOKEN_RE.finditer(text):
        term = match.group(0)
        if term in remaining:
            found.add(term)
            remaining.remove(term)
            chinese_pairs.discard(term)
        if len(term) > 2 and chinese_pairs and _is_chinese(term):
            matched = {pair for pair in chinese_pairs if pair in term}
            found.update(matched)
            remaining.difference_update(matched)
            chinese_pairs.difference_update(matched)
        if not remaining:
            break
    return found


def _split_chinese_compound(term: str) -> list[str]:
    if not _is_chinese(term) or len(term) <= 2:
        return []
    chunks = [term[index : index + 2] for index in range(len(term) - 1)]
    return [chunk for chunk in chunks if len(chunk) == 2 and chunk != term]


def _extract_phrase_terms(text: str) -> list[str]:
    quoted = [match.group(1).strip().lower() for match in _PHRASE_RE.finditer(text)]
    return [term for term in quoted if term]


def _term_matches_record(term: str, normalized_record: str, record_terms: set[str]) -> bool:
    if _is_chinese(term):
        if term in record_terms:
            return True
        if term not in normalized_record:
            return False
        # RC-09: short CJK terms (e.g. 中国) must be tokens, not substrings of 中国人.
        return len(term) > 2
    return term in record_terms


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
    # Index each contiguous run once. Repeated bigram hits previously scanned
    # to both ends of the same run for every occurrence (quadratic on repeats).
    runs = [(match.start(), match.end()) for match in re.finditer(r"[\u4e00-\u9fff]+", text)]
    starts = [start for start, _end in runs]
    seen_spans: set[tuple[int, int]] = set()
    for phrase in exact_phrase_hits:
        if not _is_chinese(phrase):
            continue
        start = 0
        while True:
            index = text.find(phrase, start)
            if index < 0:
                break
            end = index + len(phrase)
            left = index
            left_run = bisect_right(starts, index - 1) - 1
            if left_run >= 0 and index - 1 < runs[left_run][1]:
                left = runs[left_run][0]
            right = end
            right_run = bisect_right(starts, end) - 1
            if right_run >= 0 and end < runs[right_run][1]:
                right = runs[right_run][1]
            span = (left, right)
            if span not in seen_spans:
                seen_spans.add(span)
                context = text[left:right].strip()
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

    token_rate = min(1.0, len(token_hits) / query_count)
    phrase_rate = min(1.0, len(exact_phrase_hits) / query_count)
    entity_rate = min(1.0, len(entity_hits) / max(1, min(4, query_count)))
    version_total = sum(1 for term in query_terms if _VERSION_RE.match(term))
    version_rate = min(1.0, len(version_hits) / max(1, version_total))
    match = (0.55 * token_rate) + (0.25 * phrase_rate) + (0.10 * entity_rate) + (0.10 * version_rate)
    return round(max(0.0, min(_MAX_ADJUSTMENT, match * _MAX_ADJUSTMENT)), 4)


def _build_kind_suppression_reason(record_kind: str, record_source: str, recall_filters: dict | None) -> str:
    intent_name = str((recall_filters or {}).get("intent_name") or (recall_filters or {}).get("intent") or "").strip().lower()
    if not intent_name or intent_name == "research":
        return ""
    kind = str(record_kind or "").strip().lower()
    suppressed_kinds = {
        str(value or "").strip().lower()
        for value in (recall_filters or {}).get("suppressed_kinds") or ()
    }
    if not suppressed_kinds and intent_name in {
        "project_delivery",
        "operator_preference",
        "living_posture",
        "operational_issue",
    }:
        suppressed_kinds = {"knowledge_page", "news"}
    if kind not in suppressed_kinds:
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
