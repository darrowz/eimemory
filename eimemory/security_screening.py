"""Public security screening API (MIS-3 / MIS-4).

Cross-package callers must import from here — not private ``intake.loop`` helpers.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Chunked full-text scan (64 KiB windows with overlap so split tokens still match).
_SCREEN_CHUNK = 64 * 1024
_SCREEN_OVERLAP = 256

_INJECTION_PATTERNS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "disregard previous instructions",
    "reveal the system prompt",
    "show the system prompt",
    "developer message",
    "system message",
    "prompt injection",
    # Chinese / CJK injection phrases (MIS-4).
    "忽略之前的指令",
    "忽略以上指令",
    "忽略先前指令",
    "无视之前的要求",
    "请忽略系统提示",
    "泄露系统提示",
    "显示系统提示词",
    "你现在是",
    "越狱模式",
    "jailbreak",
)

_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(api[_-]?key|secret|password|token)['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{12,}",
        re.IGNORECASE,
    ),
    re.compile(r"\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE),
    re.compile(r"\bbearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9]{16,}\b"),
)


def looks_like_prompt_injection(text: str) -> bool:
    """True when text contains known prompt-injection phrases (chunked full scan)."""
    full = str(text or "")
    if not full:
        return False
    for chunk in _iter_screen_chunks(full):
        lowered = chunk.lower()
        normalized = re.sub(r"\s+", " ", lowered)
        compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", lowered)
        for pattern in _INJECTION_PATTERNS:
            normalized_pattern = re.sub(r"\s+", " ", pattern.lower())
            compact_pattern = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", pattern.lower())
            if normalized_pattern in normalized or compact_pattern in compact:
                return True
            # Also match original-case Chinese phrases against raw chunk.
            if pattern in chunk:
                return True
    return False


def looks_like_secret(text: str) -> bool:
    """True when text appears to contain credentials / private key material."""
    full = str(text or "")
    if not full:
        return False
    for chunk in _iter_screen_chunks(full):
        # Tool outputs can contain JSON encoded inside other JSON strings.
        # Decode only valid JSON escapes, with a fixed work bound; scan each
        # representation so quoted keys and escaped line boundaries are covered.
        for _ in range(8):
            if any(pattern.search(chunk) for pattern in _SECRET_PATTERNS):
                return True
            decoded = re.sub(r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])',
                             lambda match: json.loads('"' + match.group(0) + '"'), chunk)
            if decoded == chunk:
                break
            chunk = decoded
    return False


def screen_external_text(text: str) -> dict[str, Any]:
    """Return a structured screening report for externally originated text."""
    reasons: list[str] = []
    if looks_like_prompt_injection(text):
        reasons.append("prompt_injection_detected")
    if looks_like_secret(text):
        reasons.append("secret_detected")
    return {
        "ok": not reasons,
        "reasons": reasons,
        "origin": "external",
    }


def screen_record_payload(payload: dict[str, Any] | Any) -> dict[str, Any]:
    """Screen a record-like mapping (title/summary/detail/content)."""
    if hasattr(payload, "to_dict"):
        data = payload.to_dict()
    elif isinstance(payload, dict):
        data = payload
    else:
        data = {"text": str(payload or "")}
    parts = [
        str(data.get("title") or ""),
        str(data.get("summary") or ""),
        str(data.get("detail") or ""),
    ]
    content = data.get("content")
    if isinstance(content, dict):
        parts.append(json.dumps(content, ensure_ascii=False, sort_keys=True))
    elif content:
        parts.append(str(content))
    report = screen_external_text("\n".join(parts))
    report["origin"] = "external"
    return report


def mark_external_origin(record: Any) -> Any:
    """Stamp provenance/meta so recall paths can treat the record as external."""
    meta = dict(getattr(record, "meta", None) or {})
    provenance = dict(getattr(record, "provenance", None) or {})
    meta["origin"] = "external"
    provenance["origin"] = "external"
    record.meta = meta
    record.provenance = provenance
    return record


def _iter_screen_chunks(text: str):
    size = len(text)
    if size <= _SCREEN_CHUNK:
        yield text
        return
    start = 0
    while start < size:
        end = min(size, start + _SCREEN_CHUNK)
        yield text[start:end]
        if end >= size:
            break
        start = max(0, end - _SCREEN_OVERLAP)


# Back-compat aliases (private names used historically).
_looks_like_prompt_injection = looks_like_prompt_injection
_looks_like_secret = looks_like_secret
