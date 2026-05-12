from __future__ import annotations

import hashlib
import re


def stable_compiled_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def stable_page_id(page_type: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]
    prefix = "kpg" if page_type == "paper" else "ktop"
    return f"{prefix}_{digest}"


def summarize_claims(claims: list[str], *, max_items: int = 3) -> str:
    selected: list[str] = []
    seen: set[str] = set()
    for claim in claims:
        cleaned = _collapse_repeated_sentences(str(claim).strip())
        if not cleaned:
            continue
        key = re.sub(r"\s+", " ", cleaned).casefold()
        if key in seen:
            continue
        seen.add(key)
        selected.append(cleaned)
        if len(selected) >= max_items:
            break
    return " ".join(selected)


def _collapse_repeated_sentences(text: str) -> str:
    """Remove exact repeated sentence fragments from generated paper summaries."""
    if not text:
        return ""
    chunks = [chunk.strip() for chunk in re.split(r"(?<=[。.!?])\s+|\n+", text) if chunk.strip()]
    if len(chunks) <= 1:
        return text
    compact: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        key = re.sub(r"\s+", " ", chunk).casefold()
        if key in seen:
            continue
        seen.add(key)
        compact.append(chunk)
    return " ".join(compact)
