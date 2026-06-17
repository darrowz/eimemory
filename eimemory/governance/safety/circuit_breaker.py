"""Per-action-class hourly budget circuit breaker. Fails closed on overflow.

Each call site that performs a side-effect (intent-pattern upsert, memory-rule
activation, code-patch write, web fetch, outbound comm) registers a
``consume(action_class)`` call. The breaker counts calls per action class
within a rolling 1-hour window and raises :class:`BudgetExceeded` once the
per-class budget is exhausted.

The state is persisted to ``circuit_breaker.json`` in ``root`` so a restarted
process does not reset the count. The fail-closed contract is non-negotiable:
on budget exhaustion, callers MUST see an exception, never a silent allow.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Atomically write JSON to ``path`` (write to temp, fsync, replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Clean up the temp file on any failure so we don't leave junk behind.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class BudgetExceeded(Exception):
    """Raised when a per-action-class hourly budget has been exhausted."""

    def __init__(self, action_class: str) -> None:
        self.action_class = action_class
        super().__init__(f"circuit_breaker_trip: {action_class}")


class CircuitBreaker:
    """Hourly, per-action-class counter with persistent state."""

    DEFAULT_BUDGETS: dict[str, int] = {
        "intent_pattern_upsert": 10,
        "memory_rule_activate": 5,
        "code_patch_write": 3,
        "web_fetch": 30,
        "outbound_comm": 20,
    }

    def __init__(self, root: Path, default_budget: int = 10) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "circuit_breaker.json"
        self.state: dict[str, dict[str, object]] = self._load()
        self.default_budget = default_budget

    def _load(self) -> dict[str, dict[str, object]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        _atomic_write_json(self.path, self.state)

    def _budget_for(self, action_class: str) -> int:
        return self.DEFAULT_BUDGETS.get(action_class, self.default_budget)

    def _maybe_reset(self, action_class: str) -> None:
        entry = self.state.get(action_class)
        if entry is None:
            return
        reset_at_raw = entry.get("reset_at")
        if not isinstance(reset_at_raw, str):
            return
        reset_at = datetime.fromisoformat(reset_at_raw)
        if datetime.now(timezone.utc) >= reset_at:
            self.state[action_class] = {
                "count": 0,
                "reset_at": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            }

    def consume(self, action_class: str) -> None:
        """Charge one call to ``action_class``. Raises on overflow."""
        if action_class not in self.state:
            self.state[action_class] = {
                "count": 0,
                "reset_at": (
                    datetime.now(timezone.utc) + timedelta(hours=1)
                ).isoformat(),
            }
        self._maybe_reset(action_class)
        budget = self._budget_for(action_class)
        current = self.state[action_class].get("count", 0)
        if not isinstance(current, int):
            current = 0
        if current >= budget:
            raise BudgetExceeded(action_class)
        self.state[action_class]["count"] = current + 1
        self._save()

    def remaining(self, action_class: str) -> int:
        """Return the remaining budget for ``action_class`` in this window."""
        self._maybe_reset(action_class)
        budget = self._budget_for(action_class)
        entry = self.state.get(action_class)
        if not isinstance(entry, dict):
            return budget
        current = entry.get("count", 0)
        if not isinstance(current, int):
            current = 0
        return max(0, budget - current)
