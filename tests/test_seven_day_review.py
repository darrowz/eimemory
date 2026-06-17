"""7-day review: auto-promote or rollback active candidates based on real metrics."""
import tempfile
from pathlib import Path

from eimemory.autonomous.seven_day_review import review_active_candidates, ReviewDecision


def test_active_candidate_promotes_to_higher_tier():
    """If hit@1 improved by 5%, promote L1 -> L2."""
    with tempfile.TemporaryDirectory() as tmp:
        active = Path(tmp) / "active"
        active.mkdir()
        (active / "rec_test.md").write_text("# test")
        metrics = {"rec_test": {"hit@1_before": 0.6, "hit@1_after": 0.65}}
        decisions = review_active_candidates(
            active_dir=active,
            metrics=metrics,
            promote_threshold=0.05,
        )
        assert any(d.decision == "promote_l2" for d in decisions)


def test_active_candidate_rolled_back():
    """If hit@1 decreased, roll back."""
    with tempfile.TemporaryDirectory() as tmp:
        active = Path(tmp) / "active"
        active.mkdir()
        (active / "rec_test.md").write_text("# test")
        metrics = {"rec_test": {"hit@1_before": 0.6, "hit@1_after": 0.55}}
        decisions = review_active_candidates(
            active_dir=active,
            metrics=metrics,
            rollback_threshold=-0.03,
        )
        assert any(d.decision == "rollback" for d in decisions)
