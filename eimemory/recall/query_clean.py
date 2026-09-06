from __future__ import annotations

import re


_TAG = re.compile(r"</?(?:user_info|additional_data|question_answer|tdai_[a-z0-9_]+|system-reminder|user_query)[^>]*>", re.I)
_USER_QUERY = re.compile(r"<user_query>(.*?)</user_query>", re.I | re.S)
_ROLE_LINE = re.compile(r"^(?:User|Assistant|System)\s*:\s*", re.I)
_SPACE = re.compile(r"\s+")


def clean_user_query(text: str, *, max_chars: int = 2048) -> str:
    """Strip injection wrappers and keep the caller's question, like Tencent's user-query extractor."""

    raw = str(text or "").strip()
    if not raw:
        return ""
    queries = [match.strip() for match in _USER_QUERY.findall(raw) if match.strip()]
    if queries:
        raw = queries[-1]
    raw = _TAG.sub(" ", raw)
    parts = [line.strip() for line in raw.splitlines() if line.strip()]
    users: list[str] = []
    leftover: list[str] = []
    for line in parts:
        if re.match(r"^Assistant\s*:", line, re.I):
            continue
        if re.match(r"^User\s*:", line, re.I):
            users.append(_ROLE_LINE.sub("", line).strip())
            continue
        leftover.append(_ROLE_LINE.sub("", line).strip())
    chosen = users[-1] if users else " ".join(leftover) or raw
    chosen = _SPACE.sub(" ", chosen).strip()
    return chosen[: max(32, int(max_chars))]
