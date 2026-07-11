"""Tests for the per-action hourly circuit breaker (Task 0.3).

These tests verify the fail-closed contract: once a per-action-class
budget is exhausted, any further ``consume()`` call must raise
``BudgetExceeded`` instead of silently allowing the action.

Note on action-class names: the plan's draft used ``"intent_pattern_upsert"``
together with ``default_budget=5`` to express "5 calls allowed before trip".
Because the implementation looks that name up in ``DEFAULT_BUDGETS`` (where
it carries budget=10), the ``default_budget`` value would be ignored and the
trip would never fire. To exercise the ``default_budget`` branch we use
action-class names that are NOT in ``DEFAULT_BUDGETS`` so the constructor
budget applies. The known-class behavior is covered implicitly by the
``test_circuit_breaker_allows_under_budget`` test (which uses
``intent_pattern_upsert`` and 10 consumes — its DEFAULT_BUDGETS value).
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from eimemory.governance.safety.circuit_breaker import (
    BudgetExceeded,
    CircuitBreaker,
)


def test_circuit_breaker_allows_under_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cb = CircuitBreaker(root=Path(tmp), default_budget=10)
        for _ in range(10):
            cb.consume("intent_pattern_upsert")
        assert cb.remaining("intent_pattern_upsert") == 0


def test_circuit_breaker_blocks_over_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cb = CircuitBreaker(root=Path(tmp), default_budget=5)
        # Use an action class not in DEFAULT_BUDGETS so default_budget applies.
        action = "test_over_budget_action"
        for _ in range(5):
            cb.consume(action)
        with pytest.raises(BudgetExceeded) as excinfo:
            cb.consume(action)
        assert excinfo.value.action_class == action


def test_circuit_breaker_resets_hourly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        cb = CircuitBreaker(root=Path(tmp), default_budget=5)
        action = "test_hourly_reset_action"
        for _ in range(5):
            cb.consume(action)
        # Simulate hour passing by rewinding reset_at to the past.
        cb.state[action]["reset_at"] = (
            datetime.now(timezone.utc) - timedelta(hours=1)
        ).isoformat()
        cb._save()
        # Should not raise — the window has rolled over, count back to 1.
        cb.consume(action)
        assert cb.remaining(action) == 4
