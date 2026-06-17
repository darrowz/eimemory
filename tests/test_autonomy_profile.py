"""Tests for the autonomy profile config (Task 0.2 of the 2026-06-17 karpathy-loop plan)."""
from __future__ import annotations

import configparser
from pathlib import Path

from eimemory.governance.safety.profile import (
    AutonomyProfile,
    ProfileChangeError,
    load_profile,
    save_profile,
)


def test_load_profile_default(tmp_path: Path) -> None:
    """A minimal ini with profile=conservative loads with can_run_phase2()=False."""
    cfg = configparser.ConfigParser()
    cfg.add_section("autonomy")
    cfg.set("autonomy", "profile", "conservative")
    p = tmp_path / "eimemory.ini"
    p.write_text("[autonomy]\nprofile = conservative\n", encoding="utf-8")
    prof = load_profile(p)
    assert prof.profile == AutonomyProfile.CONSERVATIVE
    assert prof.can_run_phase2() is False


def test_profile_phase2_gating() -> None:
    """Only learning/progressive/autonomous can run Phase 2; conservative cannot."""
    for name, can_run in [
        ("conservative", False),
        ("learning", True),
        ("progressive", True),
        ("autonomous", True),
    ]:
        prof = AutonomyProfile(name)
        assert prof.can_run_phase2() is can_run, (
            f"{name} should {'' if can_run else 'NOT '}run Phase 2"
        )


def test_profile_save_roundtrip(tmp_path: Path) -> None:
    """A saved learning profile reloads as learning."""
    p = tmp_path / "eimemory.ini"
    prof = AutonomyProfile("learning")
    save_profile(prof, p)
    prof2 = load_profile(p)
    assert prof2.profile == AutonomyProfile.LEARNING
