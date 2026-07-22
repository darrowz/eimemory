from __future__ import annotations

import json
import re
from typing import Any


_SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|auth|cookie|credential|password|private[_-]?key|access[_-]?key|secret|token|api[_-]?key)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)[\"']?\b([a-z0-9_.-]*(?:authorization|auth|cookies?|credentials?|passwords?|"
    r"private[_-]?keys?|access[_-]?keys?|secrets?|tokens?|api[_-]?keys?)[a-z0-9_.-]*)[\"']?"
    r"(\s*[:=]\s*)(?:"
    r'"(?:\\.|[^"\\\r\n])*"|'
    r"'(?:\\.|[^'\\\r\n])*'|"
    r"bearer\s+[^\r\n,;|&\]})]+|"
    # Unquoted values are field tails, not single tokens. Consume spaces and
    # tabs, but preserve newlines and explicit structured-field separators.
    r"[^\r\n,;|&\]})]+"
    r")"
)
_BEARER = re.compile(
    r"(?i)\bbearer\s+(?:"
    r'"(?:\\.|[^"\\\r\n])*"|'
    r"'(?:\\.|[^'\\\r\n])*'|"
    r"[^\r\n,;|&\]})]+"
    r")"
)
_SECRET_TOKEN = re.compile(r"\b(?:sk|ghp|github_pat)-[A-Za-z0-9_-]{8,}\b", re.IGNORECASE)
_TRUNCATED = "[TRUNCATED]"
_REDACTED = "[REDACTED]"


def redact_bounded(value: Any, *, max_chars: int, max_depth: int = 8, max_items: int = 64) -> Any:
    budget = [max(1, int(max_chars))]

    def redact(item: Any, *, depth: int) -> Any:
        if depth > max_depth or budget[0] <= 0:
            return _TRUNCATED
        if isinstance(item, dict):
            safe: dict[str, Any] = {}
            for index, (key, nested) in enumerate(item.items()):
                if index >= max_items or budget[0] <= 0:
                    safe[_TRUNCATED] = _TRUNCATED
                    break
                sensitive_key = _SENSITIVE_KEY.search(str(key)) is not None
                key_text = f"redacted_field_{index}" if sensitive_key else _redact_text(str(key))[:100]
                budget[0] -= len(key_text)
                safe[key_text] = (
                    _REDACTED
                    if sensitive_key
                    else redact(nested, depth=depth + 1)
                )
            return safe
        if isinstance(item, (list, tuple, set)):
            values = sorted(item, key=repr) if isinstance(item, set) else list(item)
            safe_values = [redact(nested, depth=depth + 1) for nested in values[:max_items]]
            if len(values) > max_items:
                safe_values.append(_TRUNCATED)
            return safe_values
        if item is None or isinstance(item, (bool, int, float)):
            return item
        text = _redact_text(str(item))
        allowed = max(0, min(8_000, budget[0]))
        budget[0] -= min(len(text), allowed)
        return text[:allowed] if len(text) <= allowed else text[:allowed] + _TRUNCATED

    return redact(value, depth=0)


def bounded_redacted_text(value: Any, *, max_chars: int) -> str:
    parsed_value = _serialized_container(value) if isinstance(value, str) else None
    source = parsed_value if parsed_value is not None else value
    safe = redact_bounded(source, max_chars=max_chars)
    if isinstance(source, str) and isinstance(safe, str):
        rendered = safe.strip()
    else:
        rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return rendered if len(rendered) <= max_chars else rendered[:max_chars]


def _redact_text(value: str) -> str:
    safe = _SENSITIVE_ASSIGNMENT.sub(_REDACTED, value)
    safe = _BEARER.sub(f"Bearer {_REDACTED}", safe)
    return _SECRET_TOKEN.sub(_REDACTED, safe)


def _serialized_container(value: str) -> Any | None:
    text = str(value or "").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None
