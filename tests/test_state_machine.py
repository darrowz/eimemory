"""Test the candidate promotion state machine: sandbox -> canary -> active.

This is Task 1.4 of the Karpathy Loop Phase 1 plan. It enforces that a
candidate can never jump straight from sandbox to active — canary is the only
intermediate, and rollback is always reachable from any non-terminal state.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from eimemory.governance.state_machine import PromotionStateMachine, STATES


def test_state_machine_progression():
    with tempfile.TemporaryDirectory() as tmp:
        sm = PromotionStateMachine(root=Path(tmp))
        sm.create("rec_demo", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# demo\n")
        assert sm.current_state("rec_demo") == "sandbox"
        sm.promote("rec_demo", "canary", blast_radius_ok=True)
        assert sm.current_state("rec_demo") == "canary"
        sm.promote("rec_demo", "active", metrics_ok=True)
        assert sm.current_state("rec_demo") == "active"
        # File should have been moved into the active/ dir
        assert (Path(tmp) / "active" / "rec_demo.md").exists()


def test_state_machine_rejects_invalid_transition():
    with tempfile.TemporaryDirectory() as tmp:
        sm = PromotionStateMachine(root=Path(tmp))
        sm.create("rec_x", "AUTONOMOUS_LEARNING_CANDIDATE.md", "# x\n")
        # sandbox -> active (skip canary) should be rejected
        try:
            sm.promote("rec_x", "active", metrics_ok=True)
            assert False, "should reject"
        except ValueError:
            pass


def test_states_constant_exposes_terminal_set():
    assert "sandbox" in STATES
    assert "canary" in STATES
    assert "active" in STATES
    assert "rolled_back" in STATES
