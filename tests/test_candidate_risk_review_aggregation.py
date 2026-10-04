"""A replay group retains review whenever any member is not known low risk."""
from __future__ import annotations

import copy
from itertools import permutations

import pytest

from eimemory.governance.learning import candidate_search as cs


REVIEW_LEVELS = ("medium", "unknown", "unrecognized", "critical", "L5", "", " \t ", None)
LOW_LEVELS = ("low", "safe", "software", "L0", "L1", "L0-replay", "L1:software")
HIGH_LEVELS = tuple(sorted(cs.HIGH_RISK_LEVELS)) + ("L2-device", "L3:account", "L4/device")


def _cases(levels):
    return [
        {
            "risk_level": level,
            "task_type": "synthetic.review",
            "primary_label": "missing_tool_call",
            "signals": [],
            "expected_text": ["Inspect synthetic input"],
            "source_outcome_trace_id": f"synthetic-review-{index}",
        }
        for index, level in enumerate(levels)
    ]


def _candidate(cases):
    candidates = cs.generate_candidate_policies(cases)
    assert len(candidates) == 1
    return cs.score_proxy_candidates(candidates, cases)["top_candidate"]


def _assert_review(candidate, *, high_risk=False):
    assert not cs._is_low_risk(candidate["risk_level"])
    assert candidate["promotion_gate"]["requires_review"] is True
    assert candidate["promotion_gate"]["allow_auto_promote"] is False
    assert candidate["promotion_gate"]["requires_replay"] is True
    assert candidate["audit_meta"]["risk_level"] == candidate["risk_level"]
    assert candidate["audit_meta"]["promotion_gate"] == candidate["promotion_gate"]
    assert cs._is_high_risk(candidate["risk_level"]) is high_risk
    assert candidate["initial_status"] == ("candidate" if high_risk else "shadow")
    assert candidate["promotion_gate"]["blocked_reason"] == (
        "high_risk_or_unsafe" if high_risk else "risk_requires_review"
    )


@pytest.mark.parametrize("review", REVIEW_LEVELS)
@pytest.mark.parametrize("low", LOW_LEVELS)
@pytest.mark.parametrize("review_first", (False, True))
def test_any_non_low_member_preserves_review(review, low, review_first):
    levels = [review, low] if review_first else [low, review]
    cases = _cases(levels)
    original = copy.deepcopy(cases)
    candidate = _candidate(cases)
    _assert_review(candidate)
    assert candidate["proxy_eval"]["score"] == 24.0
    assert cases == original


@pytest.mark.parametrize("first", LOW_LEVELS)
@pytest.mark.parametrize("second", LOW_LEVELS)
def test_only_known_low_members_retain_original_low_contract(first, second):
    cases = _cases([first, second])
    original = copy.deepcopy(cases)
    candidate = _candidate(cases)
    assert candidate["risk_level"] == first.lower()
    assert candidate["initial_status"] == "shadow"
    assert candidate["promotion_gate"]["requires_review"] is False
    assert candidate["promotion_gate"]["allow_auto_promote"] is True
    assert candidate["proxy_eval"]["score"] == 24.0
    assert cases == original


@pytest.mark.parametrize("high", HIGH_LEVELS)
@pytest.mark.parametrize("review", ("medium", "unrecognized", None))
@pytest.mark.parametrize("high_first", (False, True))
def test_high_risk_still_dominates_review_only_and_low(high, review, high_first):
    levels = [high, review, "low"] if high_first else ["low", review, high]
    cases = _cases(levels)
    original = copy.deepcopy(cases)
    candidate = _candidate(cases)
    _assert_review(candidate, high_risk=True)
    assert candidate["proxy_eval"]["score"] == 31.0
    assert cases == original


@pytest.mark.parametrize("levels", tuple(permutations(("low", "medium", "unrecognized"))))
def test_review_requirement_is_order_independent(levels):
    _assert_review(_candidate(_cases(levels)))


@pytest.mark.parametrize("raw, normalized", (
    (" Unexpected ", "unexpected"),
    (" CUSTOM\tREVIEW  RISK ", "custom review risk"),
    ("L5", "l5"),
    ("safe-ish", "safe-ish"),
    ("需复核", "需复核"),
))
def test_unknown_nonempty_labels_are_preserved(raw, normalized):
    for levels in ([" LOW ", raw], [raw, " LOW "]):
        assert cs._max_risk_level(levels) == normalized
        _assert_review(_candidate(_cases(levels)))


@pytest.mark.parametrize("levels", ([], [""], [None], ["", " \n ", None]))
def test_absent_or_empty_risk_uses_existing_medium_default(levels):
    original = copy.deepcopy(levels)
    assert cs._max_risk_level(levels) == "medium"
    assert levels == original


def test_missing_risk_key_is_not_silently_ignored():
    cases = _cases(["low", "ignored"])
    del cases[1]["risk_level"]
    original = copy.deepcopy(cases)
    candidate = _candidate(cases)
    assert candidate["risk_level"] == "medium"
    _assert_review(candidate)
    assert cases == original


def test_high_marker_priority_with_missing_and_unknown_members_is_unchanged():
    levels = ["low", "", None, "custom", "privacy", "l2", "l3", "l4", "unsafe", "high"]
    assert cs._max_risk_level(levels) == "high"
    assert cs._max_risk_level(list(reversed(levels))) == "high"
