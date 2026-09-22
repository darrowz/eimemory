"""Incremental GovernanceRuntime Protocol (A1).

Start replacing hottest ``runtime: Any`` annotations in promotion_manager /
promotion_watch. This is a structural contract, not a full Runtime substitute.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GovernanceStore(Protocol):
    """Minimal store surface used by governance promotion / watch paths."""

    root: Any
    sqlite: Any
    _lock: Any

    def get_by_id(self, record_id: str, *, scope: Any = None) -> Any: ...
    def mutate_records_atomically(self, mutation: Any) -> Any: ...
    def flush_exports(self) -> Any: ...


@runtime_checkable
class GovernanceRuntime(Protocol):
    """Runtime subset required by promotion / watch governance."""

    store: GovernanceStore

    def get_policy_rollout_ledger(self, *, scope: Any = None, limit: int = 100) -> Any: ...
