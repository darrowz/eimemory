from __future__ import annotations

import re
from collections import Counter
from typing import Any

TARGET_PASS_RATE = 0.85
MIN_EXPECTED_POINTS = 3
MAX_EXPECTED_POINTS = 5
MAX_POINT_CHARS = 160
MAX_QUERY_CHARS = 240

_MESSAGE_ID_RE = re.compile(r"^(?:msg|message|run|thread|evt|event|call)_[a-zA-Z0-9]{12,}$")
_MESSAGE_ID_ANY_RE = re.compile(r"\b(?:msg|message|run|thread|evt|event|call)_[a-zA-Z0-9]{12,}\b")
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]*Z?\b")
_STACK_LINE_RE = re.compile(r"\bFile\s+\"[^\"]+\",\s+line\s+\d+|\bat\s+\S+\s+\(.+:\d+:\d+\)")
_MEANINGLESS_SHORT = {"ok", "yes", "no", "n/a", "na", "none", "null", "retry", "again", "继续", "好的", "嗯", "是", "否"}
_SYSTEM_ERROR_PREFIXES = (
    "error:",
    "exception:",
    "traceback",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway timeout",
)
_ACTION_WORDS = {
    "add",
    "ask",
    "build",
    "check",
    "confirm",
    "create",
    "debug",
    "explain",
    "fix",
    "inspect",
    "open",
    "play",
    "reply",
    "route",
    "summarize",
    "update",
    "verify",
}


def govern_replay_cases(cases: list[dict[str, Any]], *, limit: int) -> dict[str, Any]:
    """Filter noisy replay samples and normalize accepted cases into task-like replays."""
    accepted: list[dict[str, Any]] = []
    filter_reasons: Counter[str] = Counter()

    for case in cases:
        normalized, reason = normalize_replay_case(case)
        if normalized is None:
            filter_reasons[reason or "low_quality"] += 1
            continue
        accepted.append(normalized)

    budget = max(1, int(limit or 1))
    accepted = accepted[:budget]
    quality_score = _dataset_quality_score(accepted, filtered=sum(filter_reasons.values()))
    return {
        "cases": accepted,
        "filtered_count": sum(filter_reasons.values()),
        "filter_reasons": dict(sorted(filter_reasons.items())),
        "quality_score": quality_score,
        "case_quality_breakdown": _case_quality_breakdown(accepted, filter_reasons),
        "target_pass_rate": TARGET_PASS_RATE,
    }


def normalize_replay_case(case: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    raw_query = _raw_first_text(case.get("query"), case.get("input"), case.get("question"), case.get("prompt"))
    query_reason = _noise_reason(raw_query)
    query = raw_query if not query_reason else _derive_task_query(case, raw_query)
    if not query:
        return None, query_reason or "missing_query"

    reason = _noise_reason(query)
    if reason:
        return None, reason

    expected_text = normalize_expected_text(
        case.get("expected_text") or case.get("expect_any_text") or [],
        query=query,
        expected=case.get("expected"),
        correction=case.get("correction_from_user") or case.get("correction"),
        task_type=case.get("task_type") or case.get("target_capability"),
    )
    if not expected_text:
        return None, "missing_expected_text"

    normalized = dict(case)
    normalized["query"] = query
    normalized["input"] = query
    normalized["expected_text"] = expected_text
    normalized["expected"] = _first_text(normalized.get("expected"), expected_text[0])
    normalized["quality_score"] = _case_quality_score(normalized)
    normalized["quality_flags"] = _quality_flags(normalized)
    return normalized, ""


def normalize_expected_text(
    values: Any,
    *,
    query: Any = "",
    expected: Any = "",
    correction: Any = "",
    task_type: Any = "",
) -> list[str]:
    points: list[str] = []
    points.extend(_split_expected_points(values))
    points.extend(_split_expected_points(expected))
    points.extend(_split_expected_points(correction))
    points.extend(_derived_acceptance_points(query=query, expected=expected, correction=correction, task_type=task_type))
    return _unique_short(points)[:MAX_EXPECTED_POINTS]


def _derive_task_query(case: dict[str, Any], raw_query: str) -> str:
    candidates = [
        case.get("expected"),
        case.get("expected_behavior"),
        case.get("correction_from_user"),
        case.get("correction"),
        case.get("goal"),
        case.get("task_type"),
        case.get("target_capability"),
    ]
    for value in candidates:
        text = _first_text(value)
        if not text:
            continue
        sentence = _best_sentence(text)
        if sentence and not _noise_reason(sentence) and _looks_like_task_intent(sentence):
            return sentence
    return "" if _noise_reason(raw_query) else _clean_text(raw_query, max_chars=MAX_QUERY_CHARS)


def _noise_reason(text: str) -> str:
    raw_value = str(text or "")
    if _is_long_log_fragment(raw_value):
        return "long_log_fragment"
    value = _clean_text(raw_value, max_chars=5000)
    lower = value.lower()
    if not value:
        return "missing_query"
    if _MESSAGE_ID_RE.fullmatch(value) or (_MESSAGE_ID_ANY_RE.search(value) and len(value.split()) <= 3):
        return "message_id"
    if "usage-limit" in lower or "usage limit" in lower or "quota exceeded" in lower or "token budget" in lower:
        return "usage_limit"
    if "timeout" in lower or "timed out" in lower or "deadline exceeded" in lower or "etimedout" in lower:
        return "timeout"
    if "heartbeat" in lower or "healthcheck" in lower or "keepalive" in lower or lower in {"ping", "pong"}:
        return "heartbeat"
    if _is_pure_system_error(lower):
        return "system_error"
    if _is_short_meaningless(value):
        return "short_query"
    return ""


def _is_long_log_fragment(text: str) -> bool:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(text) > 1200:
        return True
    if len(lines) < 4:
        return False
    log_signals = 0
    for line in lines:
        lower = line.lower()
        if _TIMESTAMP_RE.search(line) or _STACK_LINE_RE.search(line):
            log_signals += 1
        if any(token in lower for token in ("traceback", "exception", "error:", "warning", "retrying", "timeout")):
            log_signals += 1
    return log_signals >= 3


def _is_pure_system_error(lower: str) -> bool:
    if not any(lower.startswith(prefix) for prefix in _SYSTEM_ERROR_PREFIXES):
        return False
    task_terms = ("user", "should", "must", "wanted", "expected", "before", "after")
    return not any(term in lower for term in task_terms)


def _is_short_meaningless(text: str) -> bool:
    lower = text.lower()
    if lower in _MEANINGLESS_SHORT:
        return True
    if len(text) < 4:
        return True
    return len(text) < 8 and len(text.split()) <= 1 and not any(ch.isalpha() for ch in text)


def _looks_like_task_intent(text: str) -> bool:
    lower = text.lower()
    if any(word in lower.split() for word in _ACTION_WORDS):
        return True
    if any(term in lower for term in (" before ", " after ", " required", "expected", "wanted", "not generic")):
        return True
    return False


def _best_sentence(text: str) -> str:
    for part in re.split(r"(?<=[.!?])\s+|\n+|;+", text):
        sentence = _clean_text(part, max_chars=MAX_QUERY_CHARS).strip(" -:")
        if sentence:
            return sentence
    return _clean_text(text, max_chars=MAX_QUERY_CHARS)


def _split_expected_points(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        points: list[str] = []
        for key in ("text", "summary", "expected", "expected_behavior", "required", "requirement", "value"):
            points.extend(_split_expected_points(value.get(key)))
        return points
    if isinstance(value, (list, tuple, set)):
        points = []
        for item in value:
            points.extend(_split_expected_points(item))
        return points
    text = str(value or "")
    if not text.strip():
        return []
    return [_clean_text(part, max_chars=MAX_POINT_CHARS) for part in re.split(r"[\n\r;]+", text) if _clean_text(part, max_chars=MAX_POINT_CHARS)]


def _derived_acceptance_points(*, query: Any, expected: Any, correction: Any, task_type: Any) -> list[str]:
    points: list[str] = []
    query_text = _first_text(query)
    expected_text = _first_text(expected)
    correction_text = _first_text(correction)
    task_text = _first_text(task_type)
    if query_text:
        points.append(f"Address the real task intent: {query_text}")
    if expected_text and expected_text != query_text:
        points.append(f"Satisfy the expected behavior: {expected_text}")
    if correction_text:
        points.append(f"Apply the user correction: {correction_text}")
    if task_text:
        points.append(f"Keep the response scoped to {task_text}.")
    points.append("Avoid repeating the noisy failure pattern from the source outcome.")
    return points


def _case_quality_score(case: dict[str, Any]) -> float:
    score = 0.4
    query = _first_text(case.get("query"))
    expected_text = _split_expected_points(case.get("expected_text"))
    if query and not _noise_reason(query):
        score += 0.25
    if len(expected_text) >= MIN_EXPECTED_POINTS:
        score += 0.2
    if len(expected_text) >= MAX_EXPECTED_POINTS:
        score += 0.05
    if _first_text(case.get("correction_from_user")):
        score += 0.05
    if _first_text(case.get("task_type"), case.get("target_capability")):
        score += 0.05
    return round(min(score, 1.0), 3)


def _dataset_quality_score(cases: list[dict[str, Any]], *, filtered: int) -> float:
    if not cases:
        return 0.0
    average = sum(float(case.get("quality_score") or 0.0) for case in cases) / len(cases)
    filter_penalty = min(0.2, filtered * 0.01)
    return round(max(0.0, min(1.0, average - filter_penalty)), 3)


def _case_quality_breakdown(cases: list[dict[str, Any]], filter_reasons: Counter[str]) -> dict[str, Any]:
    high = sum(1 for case in cases if float(case.get("quality_score") or 0.0) >= 0.8)
    medium = sum(1 for case in cases if 0.6 <= float(case.get("quality_score") or 0.0) < 0.8)
    low = sum(1 for case in cases if float(case.get("quality_score") or 0.0) < 0.6)
    return {
        "accepted": len(cases),
        "high_quality": high,
        "medium_quality": medium,
        "low_quality": low,
        "filtered": sum(filter_reasons.values()),
        "filter_reasons": dict(sorted(filter_reasons.items())),
    }


def _quality_flags(case: dict[str, Any]) -> list[str]:
    flags = ["real_task_query", "normalized_expected_text"]
    if _first_text(case.get("correction_from_user")):
        flags.append("has_user_correction")
    return flags


def _unique_short(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = _clean_text(value, max_chars=MAX_POINT_CHARS).strip(" -")
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, (list, tuple, set)):
            text = _first_text(*value)
        elif isinstance(value, dict):
            text = _first_text(value.get("text"), value.get("summary"), value.get("expected"), value.get("value"))
        else:
            text = _clean_text(value, max_chars=MAX_QUERY_CHARS)
        if text:
            return text
    return ""


def _raw_first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
        text = _first_text(value)
        if text:
            return text
    return ""


def _clean_text(value: Any, *, max_chars: int) -> str:
    return " ".join(str(value or "").split())[:max_chars]
