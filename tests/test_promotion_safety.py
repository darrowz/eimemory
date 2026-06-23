"""L3+ safety wire enforcement in promotion_manager.

Verifies that ``_check_safety_wire`` rejects L3/L4 candidates whose
``safety_wire`` does not declare every required governance module
(kill_switch, circuit_breaker, spend_guard, audit_verifier) and that
tiers below L3 pass through unchanged.
"""
from __future__ import annotations

import pytest

from eimemory.governance.promotion_manager import _check_safety_wire


def test_check_safety_wire_rejects_l3_missing_modules() -> None:
    with pytest.raises(ValueError, match="safety_wire"):
        _check_safety_wire(
            authority_tier="L3",
            safety_wire=("kill_switch",),  # missing the other 3
        )


def test_check_safety_wire_accepts_l3_with_all_four() -> None:
    _check_safety_wire(
        authority_tier="L3",
        safety_wire=("kill_switch", "circuit_breaker", "spend_guard", "audit_verifier"),
    )


def test_check_safety_wire_skips_below_l3() -> None:
    # L2 and below do not require the wire
    _check_safety_wire(authority_tier="L2", safety_wire=())
    _check_safety_wire(authority_tier="L0", safety_wire=())