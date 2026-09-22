"""S7-2: promotion watch observation errors degrade structurally."""
from __future__ import annotations

from unittest.mock import MagicMock

from eimemory.api.runtime import Runtime


def test_record_outcome_survives_watch_observation_failure(monkeypatch) -> None:
    runtime = Runtime.__new__(Runtime)
    runtime.store = MagicMock()
    runtime.store.record_outcome.return_value = {
        "id": "outcome-1",
        "event_id": "evt-1",
        "outcome": "good",
        "production_eligible": True,
    }

    def boom(*_a, **_k):
        raise RuntimeError("simulated_watch_failure")

    monkeypatch.setattr(
        "eimemory.governance.promotion_watch.record_outcome_observations",
        boom,
    )
    monkeypatch.setattr(
        "eimemory.governance.closed_loop.lightweight_outcome_learning_hook",
        lambda *a, **k: {"mode": "lightweight"},
    )
    out = runtime.record_outcome("evt-1", {"outcome": "good"}, scope={"agent_id": "a"})
    reports = out.get("post_promotion_watch") or []
    assert reports and reports[0].get("watch_failed") is True
    assert reports[0].get("error") == "RuntimeError"
    assert out.get("id") == "outcome-1"
