from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.learning_eval import run_learning_eval


def test_learning_eval_rejects_unsafe_candidate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    result = run_learning_eval(
        runtime,
        {"candidate_id": "cand_1", "authority_tier": "L3", "unsafe": True},
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
    )

    assert result["ok"] is False
    assert "safety_below_threshold" in result["blocked_reasons"]


def test_learning_eval_accepts_safe_candidate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    result = run_learning_eval(
        runtime,
        {"candidate_id": "cand_1", "authority_tier": "L1", "source_record_ids": ["rec_1"]},
        scope={"agent_id": "hongtu"},
        loop_id="learn_test",
    )

    assert result["ok"] is True
    assert result["record_id"]
