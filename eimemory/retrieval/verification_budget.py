"""Request-local completion/final-authority budget, never supplied by a record.

Collection keeps its original deadline. Only an actual, bounded model call in
this request may grant a short final authority-read window after completion.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import isfinite
from time import perf_counter

FINAL_AUTHORITY_SECONDS = 0.75


@dataclass(frozen=True)
class _Budget:
    calls: int = 0
    authority_until: float = 0.0


_BUDGET: ContextVar[_Budget | None] = ContextVar("recall_completion_budget", default=None)


@contextmanager
def verification_budget_scope():
    token = _BUDGET.set(_Budget())
    try:
        yield
    finally:
        _BUDGET.reset(token)


@contextmanager
def bounded_verification_call(timeout_seconds: float):
    """Grant a fence only after a completed, in-budget call, at most once."""
    budget = _BUDGET.get()
    if budget is None:
        # Direct verifier users have no enclosing recall authority clock.
        yield
        return
    timeout = float(timeout_seconds)
    if not isfinite(timeout) or not 0 < timeout <= 600 or budget.calls:
        raise ValueError("invalid_or_repeated_verification_budget")
    started = perf_counter()
    _BUDGET.set(_Budget(calls=1))
    yield  # Exceptions deliberately do not grant a final-read extension.
    finished = perf_counter()
    if finished <= started + timeout:
        _BUDGET.set(_Budget(calls=1, authority_until=finished + FINAL_AUTHORITY_SECONDS))


def final_authority_deadline(collection_deadline: float) -> float:
    budget = _BUDGET.get()
    return max(collection_deadline, budget.authority_until) if budget else collection_deadline
