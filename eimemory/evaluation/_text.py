"""Shared text-extraction helpers for evaluation adapters.

The LoCoMo / LongMemEval converters both produce turn objects in this
nested shape::

    {
        "turn_id": "D1:1",
        "messages": [
            {"role": "Caroline", "content": "Hey Mel!"},
            {"role": "Melanie", "content": "Hey Caroline!"},
        ],
    }

Adapters that were written for the older *flat* shape (where a turn is
just ``{"speaker": ..., "text": ...}``) silently produced empty text for
the nested shape, dropping every chunk and yielding ``R@5 == 0``.

The :func:`extract_text_from_turn` helper below accepts both shapes and
returns a single newline-joined string. It is the single source of truth
for turn-text extraction across the evaluation adapters; do not
reimplement the look-up in each adapter.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def extract_text_from_turn(turn: Any) -> str:
    """Return the textual content of a benchmark turn, regardless of shape.

    Supports three encodings, tried in order:

    1. **Flat content** — ``turn["content"]`` / ``turn["text"]`` /
       ``turn["message"]`` holds the full text directly.
    2. **Nested messages** — ``turn["messages"]`` is a list of message
       dicts (e.g. ``[{"role": ..., "content": ...}, ...]``). Each
       message contributes ``"role: text"`` (role omitted when absent);
       lines are joined with newlines.
    3. **Empty / unknown** — returns ``""``.

    Non-mapping inputs (e.g. ``None``, a bare string) yield ``""`` so
    callers can pass through raw ingest data without first validating
    its type.
    """
    if not isinstance(turn, Mapping):
        return ""

    msgs = turn.get("messages")
    if isinstance(msgs, list) and msgs:
        parts: list[str] = []
        for m in msgs:
            if not isinstance(m, Mapping):
                continue
            value = m.get("content", m.get("text", m.get("message", "")))
            role = str(m.get("role") or m.get("speaker") or "").strip()
            text = str(value or "").strip()
            if text:
                parts.append(f"{role}: {text}" if role else text)
        return "\n".join(parts)

    value = turn.get("content", turn.get("text", turn.get("message", "")))
    role = str(turn.get("role") or turn.get("speaker") or "").strip()
    text = str(value or "").strip()
    if not text:
        return ""
    return f"{role}: {text}" if role else text
