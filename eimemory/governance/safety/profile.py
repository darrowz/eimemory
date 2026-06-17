"""Autonomy profile: gates which phases may run, with hard file-level rule.

Defines the per-installation profile that the rest of the safety/governance
stack checks before allowing phase escalation (Phase 2 onward). The profile
is stored in a single ``eimemory.ini`` file so it can be inspected and edited
by humans without writing Python.

Profiles (least to most permissive):
    - ``conservative`` (default on a fresh install)
    - ``learning``
    - ``progressive``
    - ``autonomous``

Only ``learning`` and above may run Phase 2 (the Karpathy loop main body).
"""
from __future__ import annotations

import configparser
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ProfileChangeError(Exception):
    """Raised when a profile transition is not allowed (e.g. skipping a step)."""


class AutonomyProfile(str, Enum):
    CONSERVATIVE = "conservative"
    LEARNING = "learning"
    PROGRESSIVE = "progressive"
    AUTONOMOUS = "autonomous"

    def can_run_phase2(self) -> bool:
        """Phase 2 (Karpathy loop main body) is only safe to run at learning+."""
        return self in {
            AutonomyProfile.LEARNING,
            AutonomyProfile.PROGRESSIVE,
            AutonomyProfile.AUTONOMOUS,
        }


@dataclass(slots=True)
class ProfileState:
    profile: AutonomyProfile
    started_at: str
    profile_history_path: Path

    def can_run_phase2(self) -> bool:
        """Delegate to the underlying profile's phase-2 gating."""
        return self.profile.can_run_phase2()


def load_profile(ini_path: Path) -> ProfileState:
    """Load a profile state from an ini file. Defaults to conservative on missing fields."""
    cfg = configparser.ConfigParser()
    cfg.read(ini_path, encoding="utf-8")
    name = cfg.get("autonomy", "profile", fallback="conservative")
    return ProfileState(
        profile=AutonomyProfile(name),
        started_at=cfg.get("autonomy", "started_at", fallback=""),
        profile_history_path=Path(
            cfg.get(
                "autonomy",
                "profile_history",
                fallback="/var/lib/eimemory/state/autonomy/profile_history.jsonl",
            )
        ),
    )


def save_profile(state: ProfileState | AutonomyProfile, ini_path: Path) -> None:
    """Write a profile (or a full state) to an ini file. Creates parent dirs if needed."""
    if isinstance(state, AutonomyProfile):
        profile_value = state.value
        started_at = ""
        history_path = Path("/var/lib/eimemory/state/autonomy/profile_history.jsonl")
    else:
        profile_value = state.profile.value
        started_at = state.started_at
        history_path = state.profile_history_path
    cfg = configparser.ConfigParser()
    cfg.add_section("autonomy")
    cfg.set("autonomy", "profile", profile_value)
    cfg.set("autonomy", "started_at", started_at)
    cfg.set("autonomy", "profile_history", str(history_path))
    ini_path.parent.mkdir(parents=True, exist_ok=True)
    ini_path.write_text(
        "[autonomy]\nprofile = {}\nstarted_at = {}\nprofile_history = {}\n".format(
            profile_value, started_at, history_path
        ),
        encoding="utf-8",
    )
