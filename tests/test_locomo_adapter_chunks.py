"""Regression tests for the LoCoMo nested-messages chunking fix.

Background
----------

The LoCoMo converter emits turns in the nested shape::

    {"turn_id": "D1:1", "messages": [{"role": "...", "content": "..."}]}

The previous ``_turn_text()`` implementation only knew about the older
flat shape (``{"speaker": ..., "text": ...}``) and silently returned an
empty string for nested turns. That dropped every chunk produced by the
converter, leaving the LoCoMo adapter with zero chunks per case and
``R@5 == 0.0`` even on the trivially easy first sample.

These tests pin the corrected behaviour: the adapter must produce
non-empty chunks for both shapes, and a 1-case dataset must round-trip
through ``run_locomo`` with at least one retrieved turn that overlaps
the evidence turn IDs.

A paranoid test also pins the LME path to byte-identical output so the
LoCoMo refactor cannot silently regress the working ``longmemeval.py``
path.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation._text import extract_text_from_turn
from eimemory.evaluation.locomo import (
    _turn_text,
    normalize_locomo_dataset,
    run_locomo,
)
from eimemory.evaluation.longmemeval import (
    _messages_text,
    normalize_longmemeval_dataset,
)


DATA_DIR = Path(r"E:\eimemory\data")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _nested_messages_dataset(turn_count: int = 5) -> dict:
    """Build a LoCoMo-shape dataset that mirrors the converter output."""
    turns = []
    for index in range(1, turn_count + 1):
        turns.append(
            {
                "turn_id": f"D1:{index}",
                "messages": [
                    {"role": "Caroline", "content": f"Caroline line {index} about pottery."},
                    {"role": "Melanie", "content": f"Melanie reply {index} about the kiln."},
                ],
            }
        )
    return {
        "name": "locomo-nested-smoke",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "cases": [
            {
                "case_id": "locomo-nested-1",
                "question": "What happened with the kiln?",
                "answer": "The kiln broke right after class.",
                "question_type": "1",
                "haystack_sessions": [
                    {
                        "session_id": "conv0-s1",
                        "turns": turns,
                    }
                ],
                "evidence_session_ids": ["conv0-s1"],
                "evidence_turn_ids": ["D1:2"],
            }
        ],
    }


def _flat_content_dataset(turn_count: int = 4) -> dict:
    """Build a LoCoMo-shape dataset using the older flat text shape."""
    turns = [
        {
            "speaker": "Caroline" if index % 2 else "Melanie",
            "turn_id": f"D1:{index}",
            "text": f"Conversation line {index} about pottery.",
        }
        for index in range(1, turn_count + 1)
    ]
    return {
        "name": "locomo-flat-smoke",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "cases": [
            {
                "case_id": "locomo-flat-1",
                "question": "What happened with the kiln?",
                "answer": "Broken.",
                "question_type": "1",
                "haystack_sessions": [{"session_id": "D1", "turns": turns}],
                "evidence_session_ids": ["D1"],
                "evidence_turn_ids": ["D1:2"],
            }
        ],
    }


def _lme_dataset() -> dict:
    """Build a minimal LongMemEval-shape dataset for the paranoia test."""
    return {
        "name": "lme-paranoia",
        "scope": {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        "cases": [
            {
                "case_id": "lme-1",
                "question": "Which cache?",
                "question_type": "preference",
                "haystack_sessions": [
                    {
                        "session_id": "sess-cache",
                        "turns": [
                            {
                                "turn_id": "turn-cache-1",
                                "messages": [
                                    {"role": "user", "content": "I prefer Redis for cache."}
                                ],
                            }
                        ],
                    }
                ],
                "evidence_session_ids": ["sess-cache"],
                "evidence_turn_ids": ["turn-cache-1"],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Unit tests on the helper + the LoCoMo _turn_text wrapper
# ---------------------------------------------------------------------------


def test_extract_text_from_turn_nested_messages_format() -> None:
    turn = {
        "turn_id": "D1:1",
        "messages": [
            {"role": "Caroline", "content": "Caroline moved to Denver."},
            {"role": "Melanie", "content": "She started a robotics club."},
        ],
    }
    text = extract_text_from_turn(turn)
    assert "Caroline moved to Denver" in text
    assert "robotics club" in text
    assert text.splitlines()[0] == "Caroline: Caroline moved to Denver."


def test_extract_text_from_turn_flat_content_format() -> None:
    turn = {"speaker": "Caroline", "turn_id": "D1:1", "text": "I like pottery."}
    text = extract_text_from_turn(turn)
    assert text == "Caroline: I like pottery."


def test_extract_text_from_turn_empty_yields_empty() -> None:
    assert extract_text_from_turn({}) == ""
    assert extract_text_from_turn({"messages": []}) == ""
    assert extract_text_from_turn(None) == ""
    assert extract_text_from_turn("not a mapping") == ""


# ---------------------------------------------------------------------------
# LoCoMo adapter behaviour
# ---------------------------------------------------------------------------


def test_locomo_normalize_produces_nonempty_chunks() -> None:
    """A real LoCoMo case (nested shape) must yield non-empty chunks."""
    normalized = normalize_locomo_dataset(_nested_messages_dataset())
    chunks = normalized["cases"][0]["chunks"]
    assert chunks, "expected non-empty chunks for nested-messages case"
    for chunk in chunks:
        assert chunk["text"].strip(), f"chunk {chunk.get('chunk_id')} has empty text"
        assert chunk["turn_id"], f"chunk {chunk.get('chunk_id')} missing turn_id"
        assert chunk["session_id"], f"chunk {chunk.get('chunk_id')} missing session_id"


def test_locomo_handles_nested_messages_format() -> None:
    normalized = normalize_locomo_dataset(_nested_messages_dataset(turn_count=3))
    chunks = normalized["cases"][0]["chunks"]
    # 3 input turns each with 2 messages -> 3 chunks (one per turn, multi-line)
    assert len(chunks) == 3
    first = chunks[0]
    assert "Caroline: Caroline line 1 about pottery." in first["text"]
    assert "Melanie: Melanie reply 1 about the kiln." in first["text"]


def test_locomo_handles_flat_content_format() -> None:
    """Flat-shape turns must still produce chunks (no regression)."""
    normalized = normalize_locomo_dataset(_flat_content_dataset(turn_count=4))
    chunks = normalized["cases"][0]["chunks"]
    assert len(chunks) == 4
    assert "Conversation line 1 about pottery." in chunks[0]["text"]


def test_locomo_run_roundtrip_returns_nonempty(tmp_path) -> None:
    """End-to-end: a 1-case nested dataset must retrieve overlapping turns."""
    runtime = Runtime.create(root=tmp_path)
    try:
        report = run_locomo(
            runtime,
            _nested_messages_dataset(turn_count=5),
            mode="raw",
            granularity="turn",
            limit=10,
        )
    finally:
        runtime.close()

    sample = report["samples"][0]
    assert sample["returned_ids"], "expected non-empty returned_ids from raw retrieval"
    expected = set(sample["expected_ids"])
    overlap = set(sample["returned_ids"]) & expected
    assert overlap, f"expected overlap with evidence_turn_ids, got {sample['returned_ids']!r}"


# ---------------------------------------------------------------------------
# Paranoia: LME path is unchanged by the refactor
# ---------------------------------------------------------------------------


def test_lme_unaffected_by_refactor() -> None:
    """The LoCoMo refactor must not touch the LME normalisation path."""
    lme = _lme_dataset()
    # Normalize twice and compare chunk text byte-for-byte to a snapshot
    # string captured from the pre-refactor implementation. We freeze the
    # expected text here rather than calling into longmemeval twice so a
    # regression in longmemeval itself would surface as a diff in this
    # literal, not as a silent pass.
    normalized = normalize_longmemeval_dataset(lme)
    chunks = normalized["cases"][0]["chunks"]
    assert len(chunks) == 1
    assert chunks[0]["text"] == "user: I prefer Redis for cache."
    assert chunks[0]["turn_id"] == "turn-cache-1"
    # The LME helper produces identical output for the same input.
    # _messages_text takes a *list* of message dicts (not a chunk).
    turn = next(iter(lme["cases"][0]["haystack_sessions"][0]["turns"]))
    assert _messages_text(turn["messages"]) == "user: I prefer Redis for cache."


# ---------------------------------------------------------------------------
# Real-data integration check (uses the actual converted locomo10 file).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (DATA_DIR / "locomo10_eimemory.json").exists(),
    reason="locomo10_eimemory.json not present",
)
def test_locomo_real_data_produces_hundreds_of_chunks() -> None:
    """Sanity check against the actual converted LoCoMo10 dataset.

    Before the fix this returned 0 chunks per case; the fix should produce
    hundreds (each conversation has dozens of turns).
    """
    data = json.loads((DATA_DIR / "locomo10_eimemory.json").read_text(encoding="utf-8"))
    case0 = data["cases"][0]
    normalized = normalize_locomo_dataset(
        {"name": data["name"], "scope": data["scope"], "cases": [case0]}
    )
    chunks = normalized["cases"][0]["chunks"]
    assert len(chunks) > 50, f"expected >50 chunks, got {len(chunks)}"
    assert chunks[0]["text"].strip()


# ---------------------------------------------------------------------------
# _turn_text wrapper still delegates correctly
# ---------------------------------------------------------------------------


def test_locomo_turn_text_delegates_to_helper() -> None:
    nested = {
        "turn_id": "D1:1",
        "messages": [{"role": "a", "content": "hello"}],
    }
    flat = {"speaker": "a", "text": "hello", "turn_id": "D1:1"}
    assert _turn_text(nested) == "a: hello"
    assert _turn_text(flat) == "a: hello"
