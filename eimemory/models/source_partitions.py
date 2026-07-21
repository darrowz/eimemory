"""Validation for channel-local source partitions.

Source IDs are UTF-8 text normalized with Unicode NFKC and casefold.  The
safe, portable slug alphabet is ASCII lowercase letters, digits, `.`, `_`,
and `-`; a value must start and end with an alphanumeric character and may be
at most 128 characters.  This is deliberately unrelated to provenance IDs.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


DEFAULT_SOURCE_ID = "default"
MAX_SOURCE_ID_LENGTH = 128
MAX_SOURCE_ID_ALLOWLIST = 64
_SOURCE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")


def normalize_source_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("source_id must be text")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    if not normalized:
        raise ValueError("source_id must not be empty after normalization")
    if len(normalized) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"source_id exceeds {MAX_SOURCE_ID_LENGTH} characters")
    if not _SOURCE_ID_RE.fullmatch(normalized):
        raise ValueError("source_id uses characters outside the safe slug alphabet")
    return normalized


def normalize_source_ids(values: Iterable[object] | None) -> tuple[str, ...] | None:
    """Normalize an optional allowlist without silently merging collisions."""

    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        raise ValueError("source_ids must be an allowlist, not one string")
    normalized: list[str] = []
    original_by_normalized: dict[str, str] = {}
    for value in values:
        if len(normalized) >= MAX_SOURCE_ID_ALLOWLIST:
            raise ValueError(f"source_ids allowlist exceeds {MAX_SOURCE_ID_ALLOWLIST} entries")
        if not isinstance(value, str):
            raise ValueError("source_id must be text")
        source_id = normalize_source_id(value)
        previous = original_by_normalized.get(source_id)
        if previous is not None:
            raise ValueError(
                f"source_id normalization collision: {previous!r} and {value!r} both normalize to {source_id!r}"
            )
        original_by_normalized[source_id] = value
        normalized.append(source_id)
    return tuple(normalized)
