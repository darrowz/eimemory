from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_seeding import SEEDED_CAPABILITIES
from eimemory.governance.capability_ledger import build_capability_ledger, record_capability_score


def test_capability_ledger_tracks_score_and_trend(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    record_capability_score(runtime, scope=scope, loop_id="learn_1", capability="tool.routing", score=0.5, evidence_record_ids=["a"])
    record_capability_score(runtime, scope=scope, loop_id="learn_2", capability="tool.routing", score=0.8, evidence_record_ids=["b"])

    ledger = build_capability_ledger(runtime, scope=scope)

    assert ledger["capabilities"]["tool.routing"]["score"] == 0.8
    assert ledger["capabilities"]["tool.routing"]["trend"] == 0.3
    assert ledger["capabilities"]["tool.routing"]["confidence"] == "low"
    assert ledger["capabilities"]["tool.routing"]["status"] == "needs_outcome_recalculation"
    assert ledger["capabilities"]["tool.routing"]["goal_gap_reason"] == "insufficient_outcome_evidence"


def test_build_capability_ledger_auto_includes_seeded_defaults(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}

    ledger = build_capability_ledger(runtime, scope=scope)

    for capability in SEEDED_CAPABILITIES:
        item = ledger["capabilities"].get(capability)
        assert item is not None
        assert item["score"] == 0.0
        assert item["average"] == 0.0
        assert item["trend"] == 0.0
        assert item["confidence"] == "none"
        assert item["status"] == "stale_unverified"
        assert item["needs_outcome_recalculation"] is True


def test_runtime_learning_ledger_is_read_only_by_default(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    calls = {"count": 0}

    def fake_attribute(*_args, **_kwargs):
        calls["count"] += 1
        return {"ok": True}

    monkeypatch.setattr("eimemory.governance.capability_attribution.attribute_capability_outcomes", fake_attribute)

    report = runtime.learning_ledger(scope=scope, limit=5)

    assert report["ok"] is True
    assert calls["count"] == 0


def test_capability_ledger_marks_low_outcome_score_for_recalculation(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    record_capability_score(
        runtime,
        scope=scope,
        loop_id="learn_uumit",
        capability="operations.uumit",
        score=0.425,
        evidence_record_ids=["outcome-1"],
    )

    ledger = build_capability_ledger(runtime, scope=scope)
    item = ledger["capabilities"]["operations.uumit"]

    assert item["score"] == 0.425
    assert item["confidence"] == "low"
    assert item["status"] == "needs_outcome_recalculation"
    assert item["needs_outcome_recalculation"] is True
