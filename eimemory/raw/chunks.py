from __future__ import annotations

import hashlib
from typing import Any


def normalize_raw_text(text: str) -> str:
    return " ".join(str(text or "").split())


def raw_text_hash(text: str) -> str:
    return hashlib.sha256(normalize_raw_text(text).encode("utf-8")).hexdigest()


def chunk_text(
    text: str,
    *,
    session_id: str,
    source_event_id: str,
    source_type: str = "conversation",
    turn_id: str = "",
    role: str = "",
    speaker: str = "",
    occurred_at: str = "",
    max_chars: int = 1200,
    overlap_chars: int = 160,
    extra: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized = normalize_raw_text(text)
    if not normalized:
        return []

    max_chars = max(1, int(max_chars))
    overlap_chars = max(0, min(int(overlap_chars), max_chars - 1))
    chunks: list[dict[str, Any]] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + max_chars)
        chunk = normalized[start:end].strip()
        if chunk:
            chunks.append(
                {
                    "text": chunk,
                    "source_event_id": str(source_event_id or ""),
                    "source_type": str(source_type or ""),
                    "session_id": str(session_id or ""),
                    "turn_id": str(turn_id or ""),
                    "role": str(role or ""),
                    "speaker": str(speaker or ""),
                    "chunk_index": len(chunks),
                    "prev_chunk_id": "",
                    "next_chunk_id": "",
                    "raw_text_hash": raw_text_hash(chunk),
                    "occurred_at": str(occurred_at or ""),
                    **dict(extra or {}),
                }
            )
        if end >= len(normalized):
            break
        start = max(0, end - overlap_chars)
    return chunks
