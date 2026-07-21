from __future__ import annotations

from collections.abc import Iterable, Mapping
from itertools import islice
import unicodedata


MAX_IDENTITY_ALIASES = 32
MAX_IDENTITY_TEXT_CHARS = 256
IDENTITY_ALIASES_VERSION = "record_aliases.v1"

# Legacy projections are deliberately kind-specific.  In particular, arbitrary
# metadata keys never participate in identity matching.
_LEGACY_CONTENT_ALIAS_KINDS = frozenset(
    {
        "entity_record",
        "capability_model",
        "l5_world_model",
    }
)


def normalize_identity_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    normalized = " ".join(text.casefold().split())
    return normalized if len(normalized) <= MAX_IDENTITY_TEXT_CHARS else ""


def normalize_record_aliases(
    aliases: object,
    *,
    kind: str,
    content: Mapping[str, object] | None = None,
) -> list[str]:
    values: list[object] = []
    if isinstance(aliases, str):
        values.append(aliases)
    elif isinstance(aliases, Iterable) and not isinstance(aliases, (bytes, bytearray, Mapping)):
        values.extend(islice(aliases, MAX_IDENTITY_ALIASES * 2))
    if str(kind or "") in _LEGACY_CONTENT_ALIAS_KINDS and isinstance(content, Mapping):
        legacy = content.get("aliases")
        if isinstance(legacy, str):
            values.append(legacy)
        elif isinstance(legacy, Iterable) and not isinstance(legacy, (bytes, bytearray, Mapping)):
            values.extend(islice(legacy, MAX_IDENTITY_ALIASES * 2))
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        alias = normalize_identity_text(value)
        if not alias or alias in seen:
            continue
        seen.add(alias)
        normalized.append(alias)
        if len(normalized) >= MAX_IDENTITY_ALIASES:
            break
    return normalized
