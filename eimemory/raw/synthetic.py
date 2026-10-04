from __future__ import annotations

import re


def synthetic_preference_texts(text: str) -> list[str]:
    """Extract deterministic preference paraphrases from raw user text."""
    source = str(text or "")
    if not source.strip():
        return []
    results: list[str] = []
    seen: set[str] = set()
    # Preserve dots inside numbers and versions while retaining sentence stops.
    fragment = r"(?:[^.!?\n;]|(?<=\d)\.(?=\d))"
    patterns = [
        (rf"\bI\s+prefer\s+({fragment}+)", "prefer"),
        (rf"\bI\s+like\s+({fragment}+)", "like"),
        (rf"\bI\s+don['’]?t\s+like\s+({fragment}+)", "do not like"),
        (rf"\bI\s+find\s+({fragment}+?)\s+more\s+reliable\b", "find", "more reliable"),
    ]
    for pattern in patterns:
        regex = pattern[0]
        verb = pattern[1]
        suffix = pattern[2] if len(pattern) > 2 else ""
        for match in re.finditer(regex, source, flags=re.IGNORECASE):
            value = _clean_fragment(match.group(1))
            if not value:
                continue
            if suffix and not value.lower().endswith(suffix):
                value = f"{value} {suffix}"
            sentence = f"User preference: {verb} {value}"
            key = sentence.lower()
            if key in seen:
                continue
            seen.add(key)
            results.append(sentence)
    return results


def _clean_fragment(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" \t\r\n.,;:!?\"'")
    return cleaned
