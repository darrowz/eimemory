from __future__ import annotations

_CANDIDATE_TITLE_PREFIXES = (
    "News RSS candidate",
    "News domain candidate",
    "Manual source review",
    "Source candidate",
    "Knowledge candidate",
    "News item",
)


def strip_candidate_title_prefixes(value: object, *, default: str = "") -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return default
    while True:
        stripped = text
        lowered = stripped.lower()
        for prefix in _CANDIDATE_TITLE_PREFIXES:
            marker = f"{prefix}:"
            if lowered.startswith(marker.lower()):
                stripped = stripped[len(marker) :].strip()
                break
        if stripped == text:
            return stripped or default
        text = stripped
