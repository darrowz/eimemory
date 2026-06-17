"""Merged skill pipeline: skill_draft + skill_candidate -> single 3-stage flow.

Replaces the legacy two-pipeline model (a separate ``skill_draft`` ingestion
step and a later ``skill_candidate`` activation step) with one state machine
that any new skill must walk through:

    SHADOW  ->  CANARY  ->  ACTIVE

``ROLLED_BACK`` is a terminal exit reachable from any non-terminal stage.
Evidence thresholds and regression checks are enforced at every transition;
the full set of valid transitions lives in :data:`TRANSITIONS`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class SkillStage(str, Enum):
    """Stages of the unified skill pipeline."""

    SHADOW = "shadow"
    CANARY = "canary"
    ACTIVE = "active"
    ROLLED_BACK = "rolled_back"


# Minimum decisions in the prior stage required to advance to the next.
CANARY_MIN_DECISIONS = 100
ACTIVE_MIN_DECISIONS = 1000


TRANSITIONS: dict[SkillStage, set[SkillStage]] = {
    SkillStage.SHADOW: {SkillStage.CANARY, SkillStage.ROLLED_BACK},
    SkillStage.CANARY: {SkillStage.ACTIVE, SkillStage.ROLLED_BACK},
    SkillStage.ACTIVE: {SkillStage.ROLLED_BACK},
    SkillStage.ROLLED_BACK: set(),
}


@dataclass(slots=True)
class SkillCandidate:
    """A unified skill candidate walking the shadow -> canary -> active pipeline."""

    skill_name: str
    trigger: str
    source: str
    stage: SkillStage = SkillStage.SHADOW
    evidence: list[dict[str, Any]] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def promote_to(self, target: SkillStage, evidence: dict[str, Any]) -> None:
        """Promote this candidate to ``target`` if the transition and evidence are valid.

        Args:
            target: The destination stage.
            evidence: Required evidence payload. ``decisions`` must meet the
                minimum count for the destination; ``regression`` must be False.

        Raises:
            ValueError: If the transition is not allowed or the evidence does
                not meet the safety / volume bar.
        """
        if target not in TRANSITIONS[self.stage]:
            raise ValueError(f"invalid transition {self.stage.value} -> {target.value}")
        if target == SkillStage.CANARY and evidence.get("decisions", 0) < CANARY_MIN_DECISIONS:
            raise ValueError(f"canary requires >= {CANARY_MIN_DECISIONS} decisions in shadow")
        if target == SkillStage.ACTIVE and evidence.get("decisions", 0) < ACTIVE_MIN_DECISIONS:
            raise ValueError(f"active requires >= {ACTIVE_MIN_DECISIONS} decisions in canary")
        # Regression is a hard veto for *forward* promotions. It is NOT a veto
        # for ROLLED_BACK — rolling back *is* the correct response to regression.
        if target in {SkillStage.CANARY, SkillStage.ACTIVE} and evidence.get("regression", False):
            raise ValueError("regression detected, cannot promote")
        self.history.append({
            "from": self.stage.value,
            "to": target.value,
            "at": datetime.now(timezone.utc).isoformat(),
            "evidence": evidence,
        })
        self.stage = target
        self.evidence.append(evidence)


def unified_skill_candidate(*, skill_name: str, trigger: str, source: str) -> SkillCandidate:
    """Build a new unified skill candidate starting in the SHADOW stage.

    Args:
        skill_name: Stable identifier for the skill (e.g. ``"auto-recall"``).
        trigger: Description of the trigger condition for this skill.
        source: Origin pipeline (``"skill_draft"`` or ``"skill_candidate"``).

    Returns:
        A :class:`SkillCandidate` with ``stage == SkillStage.SHADOW``.
    """
    return SkillCandidate(skill_name=skill_name, trigger=trigger, source=source)