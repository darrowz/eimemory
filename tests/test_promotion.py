"""Profile promotion gate — tests (RED-first).

Verifies that the gate enforces the 14-day zero-incident floor + human
approval.md before allowing an autonomy profile promotion.
"""
import tempfile
from pathlib import Path

from eimemory.governance.safety.promotion import PromotionGate


def test_promotion_requires_approval_md():
    with tempfile.TemporaryDirectory() as tmp:
        gate = PromotionGate(
            state_dir=Path(tmp),
            current_profile="learning",
            target_profile="progressive",
            days_in_current=14,
        )
        # No approval.md yet — should be blocked
        assert gate.is_approved() is False
        # Create approval.md with the right line
        (Path(tmp) / "approval.md").write_text(
            "APPROVE: progressive  by: hongtu  at: 2026-06-30T00:00:00+08:00\n"
        )
        assert gate.is_approved() is True


def test_promotion_requires_14_days():
    with tempfile.TemporaryDirectory() as tmp:
        gate = PromotionGate(
            state_dir=Path(tmp),
            current_profile="learning",
            target_profile="progressive",
            days_in_current=10,
        )
        (Path(tmp) / "approval.md").write_text(
            "APPROVE: progressive  by: hongtu  at: 2026-06-30T00:00:00+08:00\n"
        )
        assert gate.is_approved() is False  # only 10 days


def test_promotion_rejects_unknown_target():
    with tempfile.TemporaryDirectory() as tmp:
        gate = PromotionGate(
            state_dir=Path(tmp),
            current_profile="learning",
            target_profile="platinum",
            days_in_current=30,
        )
        (Path(tmp) / "approval.md").write_text(
            "APPROVE: platinum  by: hongtu  at: 2026-06-30T00:00:00+08:00\n"
        )
        assert gate.is_approved() is False  # unknown target