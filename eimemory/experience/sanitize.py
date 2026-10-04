from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import fields, is_dataclass
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


MAX_DEPTH = 5
MAX_LIST_LENGTH = 100
MAX_STRING_LENGTH = 4096
SENSITIVE_KEY_RE = re.compile(r"(authorization|cookie|credential|password|secret|token|api[_-]?key)", re.IGNORECASE)
SENSITIVE_VALUE_RE = re.compile(
    r"(authorization\s*:|bearer\s+[a-z0-9._~+/=-]+|password\s*=|secret\s*=|token\s*=|cookie\s*:|api[_-]?key\s*=)",
    re.IGNORECASE,
)
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=\r\n]+$")


class OutcomeSanitizationError(ValueError):
    pass


def sanitize_outcome_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OutcomeSanitizationError("payload must be an object")
    return _sanitize(payload, depth=0, path="")


def _sanitize(value: Any, *, depth: int, path: str, ancestors: set[int] | None = None) -> Any:
    if depth > MAX_DEPTH:
        raise OutcomeSanitizationError("payload exceeds max depth")
    if ancestors is None:
        ancestors = set()
    dataclass_instance = is_dataclass(value) and not isinstance(value, type)
    if dataclass_instance or isinstance(value, (dict, list, tuple, set)):
        identity = id(value)
        if identity in ancestors:
            raise OutcomeSanitizationError("payload contains a cycle")
        ancestors.add(identity)
        try:
            if dataclass_instance:
                # asdict recursively copies before our limits can run. Walk
                # each field through this same bounded conversion instead.
                items = ((field.name, getattr(value, field.name)) for field in fields(value))
                return _sanitize_items(items, depth=depth, path=path, ancestors=ancestors)
            if isinstance(value, dict):
                return _sanitize_items(value.items(), depth=depth, path=path, ancestors=ancestors)
            if len(value) > MAX_LIST_LENGTH:
                raise OutcomeSanitizationError("payload list exceeds max length")
            safe_items = [
                _sanitize(item, depth=depth + 1, path=path, ancestors=ancestors)
                for item in value
            ]
            return sorted(safe_items, key=lambda item: repr(item)) if isinstance(value, set) else safe_items
        finally:
            # Shared non-cyclic subobjects remain valid in sibling branches.
            ancestors.remove(identity)
    if isinstance(value, Path):
        return _sanitize_string(str(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date_type):
        return value.isoformat()
    if isinstance(value, str):
        return _sanitize_string(value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return _sanitize_string(str(value))


def _sanitize_items(
    items: Iterable[tuple[Any, Any]], *, depth: int, path: str, ancestors: set[int]
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, item in items:
        key_text = _sanitize_string(str(key))
        _validate_key_value(key_text, item)
        safe[key_text] = _sanitize(
            item,
            depth=depth + 1,
            path=f"{path}.{key_text}" if path else key_text,
            ancestors=ancestors,
        )
    return safe


def _validate_key_value(key: str, value: Any) -> None:
    if key == "raw_image_stored" and value is True:
        raise OutcomeSanitizationError("raw image storage is not allowed")
    # Only the measured non-negative integer counter is non-secret telemetry.
    # Do not weaken matching for camelCase or suffixed credential field names.
    if key == "token_count" and type(value) is int and value >= 0:
        return
    if SENSITIVE_KEY_RE.search(key):
        raise OutcomeSanitizationError("sensitive payload key is not allowed")


def _sanitize_string(value: str) -> str:
    if len(value) > MAX_STRING_LENGTH:
        raise OutcomeSanitizationError("payload string exceeds max length")
    lowered = value.lower()
    if "data:image" in lowered:
        raise OutcomeSanitizationError("inline image data is not allowed")
    if SENSITIVE_VALUE_RE.search(value):
        raise OutcomeSanitizationError("sensitive payload value is not allowed")
    if _has_url_credentials(value):
        raise OutcomeSanitizationError("credentialed URLs are not allowed")
    if _looks_like_large_base64(value):
        raise OutcomeSanitizationError("large base64 payloads are not allowed")
    return value


def _has_url_credentials(value: str) -> bool:
    for match in re.finditer(r"https?://[^\s<>'\"]+", value, flags=re.IGNORECASE):
        try:
            parsed = urlparse(match.group(0))
            if parsed.username or parsed.password:
                return True
        except ValueError as exc:
            raise OutcomeSanitizationError("malformed URL is not allowed") from exc
    return False


def _looks_like_large_base64(value: str) -> bool:
    compact = "".join(value.split())
    if len(compact) < 512:
        return False
    return bool(BASE64_RE.fullmatch(compact))
