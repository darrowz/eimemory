"""Record ID charset validation — reject, never silent-sanitize."""

from __future__ import annotations

import re

# Path-safe identifier charset. Reject traversal / separators / whitespace.
RECORD_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_RECORD_ID_LEN = 256


class InvalidRecordId(ValueError):
    """Raised when a record_id fails the charset / length whitelist."""


def validate_record_id(record_id: str, *, field: str = "record_id") -> str:
    """Return record_id if it matches ``^[A-Za-z0-9_.-]+$``; else raise.

    Never silently sanitizes. Empty / oversized / path-like values are errors.
    """
    value = str(record_id if record_id is not None else "")
    if not value or len(value) > MAX_RECORD_ID_LEN:
        raise InvalidRecordId(f"{field}_invalid:{value[:80]!r}")
    if RECORD_ID_PATTERN.fullmatch(value) is None:
        raise InvalidRecordId(f"{field}_invalid:{value[:80]!r}")
    if value in {".", ".."} or value.startswith(".") and value.count(".") == len(value):
        raise InvalidRecordId(f"{field}_invalid:{value[:80]!r}")
    return value


def is_valid_record_id(record_id: str) -> bool:
    try:
        validate_record_id(record_id)
        return True
    except InvalidRecordId:
        return False
