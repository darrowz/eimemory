"""S1-1: capability_ledger attribution failures are structured + logged."""
from __future__ import annotations

from unittest.mock import MagicMock

from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.models.records import ScopeRef


def test_attribution_failure_is_structured(monkeypatch) -> None:
    runtime = MagicMock()
    runtime.store.list_capability_scores_compact.return_value = []
    # Force compact path used by _require_compact_capability_scores
    monkeypatch.setattr(
        "eimemory.governance.capability_attribution.attribute_capability_outcomes",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("attr_boom")),
    )
    # Some code paths require compact helper on store
    def _require(runtime, **kwargs):
        return []

    monkeypatch.setattr(
        "eimemory.governance.capability_ledger._require_compact_capability_scores",
        _require,
    )
    ledger = build_capability_ledger(
        runtime,
        scope=ScopeRef.from_dict({"agent_id": "a"}),
        attribute_outcomes=True,
    )
    assert ledger["attribution"]["ok"] is False
    assert ledger["attribution"]["error"] == "RuntimeError"
    assert ledger["ok"] is True
