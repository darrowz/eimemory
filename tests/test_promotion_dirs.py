"""Require the canary/active/rolled_back promotion dirs under state/autonomous_learning/.

These three dirs are the destination of the L1 -> canary -> active state machine
(Task 1.4 in the 2026-06-17 plan). If they are missing, the promotion pipeline
silently degrades into the old `reviewable_patches/` flow, so we test for them
at the directory level: any missing dir = fail.
"""
from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
PROMOTION_ROOT = REPO_ROOT / "state" / "autonomous_learning"
REQUIRED_DIRS = ("canary", "active", "rolled_back")


def _required(name: str) -> Path:
    return PROMOTION_ROOT / name


def test_canary_dir_exists():
    assert _required("canary").is_dir(), f"missing dir: {_required('canary')}"


def test_active_dir_exists():
    assert _required("active").is_dir(), f"missing dir: {_required('active')}"


def test_rolled_back_dir_exists():
    assert _required("rolled_back").is_dir(), f"missing dir: {_required('rolled_back')}"


def test_all_three_promotion_dirs_present():
    missing = [d for d in REQUIRED_DIRS if not _required(d).is_dir()]
    assert not missing, f"missing promotion dirs: {missing} under {PROMOTION_ROOT}"
