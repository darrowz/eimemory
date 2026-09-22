"""Bounded JSON decoding for untrusted transport input; no runtime dependencies."""
from __future__ import annotations

import json
from math import isfinite
from typing import Any


class StrictJSONError(ValueError):
    """A fixed diagnostic code, never a copy of a private request/response."""


def _finite_float(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise StrictJSONError("nonfinite_number")
    return number


def _constant(_value: str) -> None:
    raise StrictJSONError("nonfinite_number")


def _unique_pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise StrictJSONError("duplicate_key")
        result[key] = value
    return result


def loads(raw: str | bytes, *, max_bytes: int = 1_000_000, max_depth: int = 64) -> Any:
    """Reject duplicate keys, non-finite numbers (including 1e309), and deep input.

    Limits apply before recursive decoding. This is input validation, not an
    authentication decision or an evaluation/evidence approval.
    """
    if type(max_bytes) is not int or max_bytes < 1 or type(max_depth) is not int or max_depth < 1:
        raise ValueError("JSON limits must be positive integers")
    if not isinstance(raw, (str, bytes)):
        raise StrictJSONError("invalid_json_type")
    if len(raw) > max_bytes:
        raise StrictJSONError("json_too_large")
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if len(text.encode("utf-8")) > max_bytes:
            raise StrictJSONError("json_too_large")
        depth = 0
        quoted = escaped = False
        for char in text:
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "[{":
                depth += 1
                if depth > max_depth:
                    raise StrictJSONError("json_too_deep")
            elif char in "]}":
                depth -= 1
        return json.loads(text, object_pairs_hook=_unique_pairs,
                          parse_float=_finite_float, parse_constant=_constant)
    except StrictJSONError:
        raise
    except (UnicodeError, ValueError, RecursionError, OverflowError):
        raise StrictJSONError("invalid_json") from None
