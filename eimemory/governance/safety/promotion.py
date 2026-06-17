"""Profile promotion gate. Requires 14-day zero-incident floor + human approval.md.

This module is part of Task 0.7 in
``docs/superpowers/plans/2026-06-17-eimemory-karpathy-loop.md``.

Note: ``AutonomyProfile`` is defined locally as a minimal stand-in. Task 0.2
will eventually move this enum to ``eimemory/governance/safety/profile.py``;
the gate's promotion logic itself depends only on the four profile names.
"""
from __future__ import annotations

import re
from enum import Enum
from pathlib import Path


class AutonomyProfile(str, Enum):
    """Autonomy profile levels (also defined by Task 0.2; mirrored here for self-containment)."""

    CONSERVATIVE = "conservative"
    LEARNING = "learning"
    PROGRESSIVE = "progressive"
    AUTONOMOUS = "autonomous"


ALLOWED_PROMOTIONS: dict[AutonomyProfile, set[AutonomyProfile]] = {
    AutonomyProfile.CONSERVATIVE: {AutonomyProfile.LEARNING},
    AutonomyProfile.LEARNING: {AutonomyProfile.PROGRESSIVE},
    AutonomyProfile.PROGRESSIVE: {AutonomyProfile.AUTONOMOUS},
    AutonomyProfile.AUTONOMOUS: set(),
}
MIN_DAYS_IN_CURRENT = 14


class PromotionGate:
    """Gate that decides whether a profile promotion is allowed.

    Args:
        state_dir: Directory containing ``approval.md`` (operator-supplied).
        current_profile: Current autonomy profile name.
        target_profile: Desired next profile name.
        days_in_current: Days the system has been in ``current_profile``.
    """

    def __init__(
        self,
        *,
        state_dir: Path,
        current_profile: str,
        target_profile: str,
        days_in_current: int,
    ) -> None:
        self.state_dir = Path(state_dir)
        # Resolve current_profile eagerly — it must be a known profile.
        self.current = AutonomyProfile(current_profile)
        # Resolve target_profile lazily inside is_approved() so unknown
        # targets yield False (instead of crashing in __init__).
        self._target_raw = target_profile
        self.days = days_in_current
        self.approval_path = self.state_dir / "approval.md"

    def is_approved(self) -> bool:
        """Return True iff the promotion is structurally allowed.

        Checks (in order):
          1. Target profile is reachable from the current profile (per
             ``ALLOWED_PROMOTIONS``). Unknown target names → False.
          2. The system has spent at least ``MIN_DAYS_IN_CURRENT`` days in the
             current profile (the 14-day zero-incident floor).
          3. ``state_dir/approval.md`` exists and contains a matching
             ``APPROVE: <target>  by: <actor>  at: <timestamp>`` line.
        """
        try:
            target = AutonomyProfile(self._target_raw)
        except ValueError:
            return False
        if target not in ALLOWED_PROMOTIONS.get(self.current, set()):
            return False
        if self.days < MIN_DAYS_IN_CURRENT:
            return False
        if not self.approval_path.exists():
            return False
        text = self.approval_path.read_text(encoding="utf-8")
        pattern = rf"^APPROVE:\s*{re.escape(target.value)}\s+by:\s*\S+\s+at:\s*\S+"
        return bool(re.search(pattern, text, re.MULTILINE))