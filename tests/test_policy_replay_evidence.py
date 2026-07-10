from __future__ import annotations

from eimemory.governance.policy_replay import evaluate_replay_gate


def test_replay_case_definition_is_not_execution_evidence() -> None:
    report = evaluate_replay_gate(
        {
            "query": "fix the failing module",
            "expected_text": ["verification passes"],
            "negative_expected_text": [],
            "regression_seed_patterns": [],
        }
    )

    assert report["ok"] is True
    assert report["case_valid"] is True
    assert report["executed"] is False
    assert report["evidence_kind"] == "case_definition"
    assert report["verdict"] == "defined"
