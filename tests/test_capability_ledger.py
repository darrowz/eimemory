from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_seeding import SEEDED_CAPABILITIES
from eimemory.governance.capability_ledger import build_capability_ledger, record_capability_score
from eimemory.models.records import RecordEnvelope, ScopeRef


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


def test_record_capability_score_counts_sequence_without_record_scan(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    record_capability_score(
        runtime,
        scope=scope,
        loop_id="learn_1",
        capability="memory.recall",
        score=0.5,
        evidence_record_ids=["a"],
    )

    def fail_record_scan(*_args, **_kwargs):
        raise AssertionError("capability score sequence must not scan record pages")

    monkeypatch.setattr(runtime.store, "list_records", fail_record_scan)
    second_id = record_capability_score(
        runtime,
        scope=scope,
        loop_id="learn_2",
        capability="memory.recall",
        score=0.8,
        evidence_record_ids=["b"],
    )

    second = runtime.store.get_by_id(second_id, scope=scope)
    assert second is not None
    assert second.meta["score_sequence"] == 2


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


def test_capability_ledger_ignores_legacy_scores_backed_only_by_not_run_replays(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    scope_ref = ScopeRef.from_dict(scope)
    record_capability_score(
        runtime,
        scope=scope,
        loop_id="verified-baseline",
        capability="memory.recall",
        score=0.84,
        evidence_record_ids=["verified-1", "verified-2", "verified-3"],
    )
    replay_ids = []
    for index in range(3):
        replay = runtime.store.append(
            RecordEnvelope.create(
                kind="replay_result",
                title=f"Unavailable replay {index}",
                summary="Contract-backed evidence was unavailable.",
                scope=scope_ref,
                source="eimemory.capability_replay",
                content={"verdict": "not_run"},
                meta={"report_type": "capability_replay_pack", "verdict": "not_run"},
            )
        )
        replay_ids.append(replay.record_id)
    record_capability_score(
        runtime,
        scope=scope,
        loop_id="legacy-not-run",
        capability="memory.recall",
        score=0.0,
        evidence_record_ids=replay_ids,
        evidence_sources=["capability_replay_pack"],
        meta={"kind": "capability_replay_pack", "pass_rate": 0.0},
    )

    item = build_capability_ledger(runtime, scope=scope, attribute_outcomes=False)["capabilities"]["memory.recall"]

    assert item["score"] == 0.84
    assert item["status"] == "active"
    assert set(item["evidence_record_ids"]) == {"verified-1", "verified-2", "verified-3"}


def test_capability_ledger_ignores_legacy_candidate_gate_failure_zero(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    record_capability_score(
        runtime,
        scope=scope,
        loop_id="verified-baseline",
        capability="proactive.judgment",
        score=0.84,
        evidence_record_ids=["verified-1", "verified-2", "verified-3"],
    )
    record_capability_score(
        runtime,
        scope=scope,
        loop_id="legacy-candidate-failure",
        capability="proactive.judgment",
        score=0.0,
        evidence_record_ids=["candidate", "failed-eval"],
        meta={
            "kind": "autonomous_learning_measured",
            "eval_verdict": "fail",
            "replay_gate_passed": False,
            "safety_gate_passed": True,
            "isolation_gate_passed": False,
        },
    )

    item = build_capability_ledger(runtime, scope=scope, attribute_outcomes=False)["capabilities"]["proactive.judgment"]

    assert item["score"] == 0.84
    assert item["status"] == "active"
    assert set(item["evidence_record_ids"]) == {"verified-1", "verified-2", "verified-3"}
