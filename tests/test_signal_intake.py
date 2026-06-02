from __future__ import annotations

from eimemory.governance.signal_intake import rank_learning_signals


def test_repeated_first_party_failure_ranks_above_fresh_public_signal() -> None:
    signals = [
        {
            "source_kind": "research_feed",
            "signal_type": "new_paper",
            "title": "New prompt trick",
            "summary": "Interesting external idea",
            "evidence_tier": "T4",
            "confidence": 0.6,
        },
        {
            "source_kind": "local_outcome_trace",
            "signal_type": "bad_outcome",
            "title": "Repeated tool routing failure",
            "summary": "Tool routing failed twice",
            "repeat_count": 3,
            "evidence_tier": "T0",
            "confidence": 0.8,
        },
    ]
    self_model = {"weaknesses": [{"kind": "tool.routing", "capability": "tool.routing", "lesson": "tool routing failed"}]}

    ranked = rank_learning_signals(signals, self_model, [], max_items=2)

    assert ranked[0]["source_kind"] == "local_outcome_trace"
    assert ranked[0]["score"] > ranked[1]["score"]
