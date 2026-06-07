from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.evaluation.locomo import normalize_locomo_dataset, run_locomo


def test_locomo_normalization_preserves_turn_ids() -> None:
    dataset = _locomo_dataset(turn_count=3)

    normalized = normalize_locomo_dataset(dataset)

    chunks = normalized["cases"][0]["chunks"]
    assert [chunk["turn_id"] for chunk in chunks[:3]] == ["D1:1", "D1:2", "D1:3"]
    assert normalized["cases"][0]["evidence_turn_ids"] == ["D1:2"]


def test_locomo_turn_retrieval_expands_adjacent_turns(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    try:
        report = run_locomo(
            runtime,
            _locomo_dataset(turn_count=4),
            mode="raw",
            granularity="turn",
            limit=5,
        )
    finally:
        runtime.close()

    sample = report["samples"][0]
    assert sample["rank"] > 0
    assert "D1:2" in sample["returned_ids"][:5]


def test_locomo_raw_retrieval_fills_topk_for_sparse_queries(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)

    try:
        report = run_locomo(
            runtime,
            _locomo_dataset(turn_count=12, question="zzzz unavailable clue"),
            mode="raw",
            granularity="turn",
            limit=10,
        )
    finally:
        runtime.close()

    assert len(report["samples"][0]["returned_ids"]) == 10


def _locomo_dataset(*, turn_count: int, question: str = "What happened after pottery class?") -> dict:
    turns = [
        {"speaker": "Caroline", "turn_id": "D1:1", "text": "Caroline discussed pottery class all afternoon."},
        {"speaker": "Melanie", "turn_id": "D1:2", "text": "The kiln broke right after class."},
        {"speaker": "Caroline", "turn_id": "D1:3", "text": "They scheduled repairs for the studio."},
        {"speaker": "Melanie", "turn_id": "D1:4", "text": "The studio reopened on Friday."},
    ]
    for index in range(5, turn_count + 1):
        turns.append(
            {
                "speaker": "Caroline" if index % 2 else "Melanie",
                "turn_id": f"D1:{index}",
                "text": f"Background conversation item {index}.",
            }
        )
    return {
        "name": "locomo-smoke",
        "cases": [
            {
                "case_id": "locomo-turn-smoke",
                "question": question,
                "answer": "The kiln broke.",
                "question_type": "1",
                "sessions": [{"session_id": "D1", "turns": turns}],
                "evidence_session_ids": ["D1"],
                "evidence_turn_ids": ["D1:2"],
            }
        ],
    }
