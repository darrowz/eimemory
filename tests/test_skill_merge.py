"""Tests for the merged 3-stage skill pipeline (Task 4.3 of the Karpathy Loop plan).

Merges the legacy ``skill_draft`` and ``skill_candidate`` flows into a single
3-stage state machine: ``shadow`` -> ``canary`` -> ``active`` (with
``rolled_back`` as a terminal exit from any non-terminal stage).

These tests verify the public factory ``unified_skill_candidate`` and the
state-machine transitions on ``SkillCandidate.promote_to``.
"""
from __future__ import annotations

import pytest

from eimemory.governance.skills.merged_pipeline import (
    SkillStage,
    unified_skill_candidate,
)


def test_unified_skill_starts_at_shadow() -> None:
    """A new unified skill candidate must always start in the SHADOW stage.

    The 3-stage pipeline is shadow -> canary -> active. Anything else is a
    regression to the old two-pipeline model.
    """
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    assert sc.stage == SkillStage.SHADOW


def test_skill_promotes_through_stages() -> None:
    """shadow -> canary -> active must work, recording history at each step."""
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    sc.promote_to(SkillStage.CANARY, evidence={"decisions": 100, "regression": False})
    assert sc.stage == SkillStage.CANARY
    sc.promote_to(SkillStage.ACTIVE, evidence={"decisions": 1000, "regression": False})
    assert sc.stage == SkillStage.ACTIVE


def test_canary_promotion_requires_minimum_decisions() -> None:
    """A skill cannot move to canary with fewer than 100 shadow decisions."""
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    with pytest.raises(ValueError):
        sc.promote_to(SkillStage.CANARY, evidence={"decisions": 50, "regression": False})


def test_active_promotion_requires_minimum_decisions() -> None:
    """A skill cannot move to active with fewer than 1000 canary decisions."""
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    sc.promote_to(SkillStage.CANARY, evidence={"decisions": 100, "regression": False})
    with pytest.raises(ValueError):
        sc.promote_to(SkillStage.ACTIVE, evidence={"decisions": 500, "regression": False})


def test_regression_blocks_promotion() -> None:
    """A skill with regression=True in evidence must not be promoted."""
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    with pytest.raises(ValueError):
        sc.promote_to(SkillStage.CANARY, evidence={"decisions": 5000, "regression": True})


def test_invalid_transition_is_rejected() -> None:
    """Cannot skip stages (shadow -> active is forbidden)."""
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    with pytest.raises(ValueError):
        sc.promote_to(SkillStage.ACTIVE, evidence={"decisions": 99999, "regression": False})


def test_rolled_back_is_terminal() -> None:
    """Once rolled back, no further transitions are allowed."""
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    sc.promote_to(SkillStage.CANARY, evidence={"decisions": 100, "regression": False})
    sc.promote_to(SkillStage.ROLLED_BACK, evidence={"regression": True})
    assert sc.stage == SkillStage.ROLLED_BACK
    with pytest.raises(ValueError):
        sc.promote_to(SkillStage.ACTIVE, evidence={"decisions": 1000, "regression": False})


def test_history_records_transitions() -> None:
    """Each promotion must append a history entry with from/to/evidence."""
    sc = unified_skill_candidate(skill_name="auto-recall", trigger="test", source="skill_draft")
    sc.promote_to(SkillStage.CANARY, evidence={"decisions": 100, "regression": False})
    sc.promote_to(SkillStage.ACTIVE, evidence={"decisions": 1000, "regression": False})
    assert len(sc.history) == 2
    assert sc.history[0]["from"] == "shadow"
    assert sc.history[0]["to"] == "canary"
    assert sc.history[1]["from"] == "canary"
    assert sc.history[1]["to"] == "active"
    assert all("at" in entry for entry in sc.history)
    assert all("evidence" in entry for entry in sc.history)
