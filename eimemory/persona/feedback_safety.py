"""Bounded correction validation and content-minimized Store receipts.

These checks validate data shape/source consistency, not authenticated origin.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re
from typing import Any

from eimemory.persona.schema import PersonaTraits

RAW_TEXT_WITHHELD = "[raw feedback intentionally not retained]"
MAX_FEEDBACK_CHARS = 16_384
RULES = {
    "safety": "Never store, quote, or log plaintext secrets; use approved secret aliases and confirmation gates.",
    "verbosity": "When the user says the agent is overacting or verbose, answer direct result first.",
    "reinforcement": "Keep the current reply style when the user explicitly says it worked well.",
    "resourcefulness": "When the first path fails, try an alternate tool or route before reporting a blocker.",
    "correctness": "Convert user corrections into replay checks before treating the behavior as fixed.",
    "latency": "Prefer a short status-first response when latency pressure is explicit.",
    "memory": "Treat explicit user preferences as memory candidates with clear scope.",
    "tone": "Keep tone grounded and adapt to the user's correction.",
}
_ERROR_CODES = frozenset({
    "sensitive_feedback", "invalid_feedback", "invalid_source_type", "record_store_unavailable",
    "legacy_sensitive_payload_withheld", "invalid_existing_correction", "idempotency_conflict",
    "correction_scan_incomplete", "invalid_scan_limit",
})
# A conservative recognition layer, not a claim of exhaustive secret detection.
# Raw text/rule/key are never retained even when no pattern matches.
_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16})\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/-]{8,}", re.I),
    re.compile(r"(?:api[ _-]?key|access[ _-]?token|refresh[ _-]?token|token|password|passwd|secret|cookie|recovery[ _-]?codes?|密码|密钥|恢复码)\s*(?:[:=：]|\bis\b|是|为)\s*[^\s,;，；]+", re.I),
)


class PersonaCorrectionRejected(ValueError):
    """No record was stored; the exception and public payload contain no input."""
    def __init__(self, code: str = "invalid_feedback") -> None:
        self.code = code if code in _ERROR_CODES else "invalid_feedback"
        super().__init__(self.code)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "persisted": False, "record_id": None, "error": self.code}


def contains_sensitive_feedback(value: str) -> bool:
    return any(pattern.search(value) is not None for pattern in _SECRET_PATTERNS)


def validate_idempotency_key(value: Any) -> str:
    if type(value) is not str or len(value) > MAX_FEEDBACK_CHARS:
        raise PersonaCorrectionRejected("invalid_feedback")
    if contains_sensitive_feedback(value):
        raise PersonaCorrectionRejected("sensitive_feedback")
    return value


def safe_correction_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Allowlist values; drop all raw input, extension fields and arbitrary rules."""
    if not isinstance(data, dict):
        raise PersonaCorrectionRejected("invalid_feedback")
    if data.get("source") != "user_message" or data.get("event_type") != "persona.correction":
        raise PersonaCorrectionRejected("invalid_source_type")
    category = data.get("category")
    if type(category) is not str or category not in RULES:
        raise PersonaCorrectionRejected("invalid_feedback")
    for key in ("raw_text", "rule_candidate"):
        value = data.get(key)
        if type(value) is not str or len(value) > MAX_FEEDBACK_CHARS:
            raise PersonaCorrectionRejected("invalid_feedback")
        if contains_sensitive_feedback(value):
            raise PersonaCorrectionRejected("sensitive_feedback")
    if not data["raw_text"].strip():
        raise PersonaCorrectionRejected("invalid_feedback")
    try:
        severity = _number(data.get("severity"))
        if not 0.0 <= severity <= 1.0:
            raise ValueError
        raw_delta = data.get("trait_delta")
        if not isinstance(raw_delta, dict):
            raise ValueError
        delta = {}
        for key, value in raw_delta.items():
            if type(key) is not str or key not in PersonaTraits.__dataclass_fields__:
                raise ValueError
            parsed = _number(value)
            if not -1.0 <= parsed <= 1.0:
                raise ValueError
            delta[key] = parsed
        timestamp = data.get("created_at")
        if type(timestamp) is not str or len(timestamp) > 40:
            raise ValueError
        instant = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError
        created_at = instant.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (TypeError, ValueError, OverflowError):
        raise PersonaCorrectionRejected("invalid_feedback") from None
    return {
        "raw_text": RAW_TEXT_WITHHELD, "category": category, "severity": severity,
        "trait_delta": delta, "rule_candidate": RULES[category], "source": "user_message",
        "event_type": "persona.correction", "created_at": created_at,
    }


def _number(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError
    result = float(value)
    if not math.isfinite(result):
        raise ValueError
    return result
