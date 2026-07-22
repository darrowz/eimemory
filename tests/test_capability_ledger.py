from __future__ import annotations

import json
from hashlib import sha256

import pytest

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


def test_archived_capability_scores_use_compact_projection_when_sequence_counter_is_unavailable(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    compact_calls: list[dict[str, object]] = []
    try:
        runtime.store.sqlite.payload_archive_inline_bytes = 256
        record_capability_score(
            runtime,
            scope=scope,
            loop_id="archived-sequence-1",
            capability="memory.recall",
            score=0.5,
            evidence_record_ids=["archive-evidence-" + ("x" * 400)],
        )
        pointer_count = runtime.store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM records WHERE kind='capability_score' AND payload_pointer_json!=''"
        ).fetchone()[0]
        assert pointer_count == 1

        original_compact = runtime.store.list_capability_scores_compact

        def tracked_compact(*args, **kwargs):
            compact_calls.append(dict(kwargs))
            return original_compact(*args, **kwargs)

        def reject_full_load(*_args, **_kwargs):
            raise AssertionError("governance must not hydrate archived capability-score payloads")

        monkeypatch.setattr(runtime.store, "count_records_by_meta_value", lambda **_kwargs: None)
        monkeypatch.setattr(runtime.store, "list_capability_scores_compact", tracked_compact)
        monkeypatch.setattr(runtime.store, "list_records", reject_full_load)
        monkeypatch.setattr(runtime.store.sqlite.payload_segments, "read", reject_full_load)

        second_id = record_capability_score(
            runtime,
            scope=scope,
            loop_id="archived-sequence-2",
            capability="memory.recall",
            score=0.8,
            evidence_record_ids=["archive-evidence-2-" + ("y" * 400)],
        )
        ledger = build_capability_ledger(
            runtime,
            scope=scope,
            attribute_outcomes=False,
        )
        stored_meta = json.loads(
            runtime.store.sqlite.conn.execute(
                "SELECT meta_json FROM records WHERE record_id=?",
                (second_id,),
            ).fetchone()[0]
        )
    finally:
        runtime.close()

    assert stored_meta["score_sequence"] == 2
    assert ledger["capabilities"]["memory.recall"]["score"] == 0.8
    assert [call["limit"] for call in compact_calls] == [500, 500]


def test_build_capability_ledger_fails_closed_without_compact_projection(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    list_calls = 0

    def tracked_full_load(*_args, **_kwargs):
        nonlocal list_calls
        list_calls += 1
        return []

    try:
        monkeypatch.setattr(runtime.store, "list_capability_scores_compact", None)
        monkeypatch.setattr(runtime.store, "list_records", tracked_full_load)
        with pytest.raises(RuntimeError, match="compact capability-score projection is unavailable"):
            build_capability_ledger(
                runtime,
                scope={"agent_id": "hongtu"},
                attribute_outcomes=False,
            )
    finally:
        runtime.close()

    assert list_calls == 0


def test_build_capability_ledger_uses_compact_score_projection(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    compact_calls = 0
    try:
        record_capability_score(
            runtime,
            scope=scope,
            loop_id="compact-ledger",
            capability="memory.recall",
            score=0.9,
            evidence_record_ids=["trace-1", "trace-2", "trace-3"],
            evidence_sources=["outcome_trace"],
        )
        original_compact = runtime.store.list_capability_scores_compact

        def tracked_compact(*args, **kwargs):
            nonlocal compact_calls
            compact_calls += 1
            records = original_compact(*args, **kwargs)
            assert all("evidence_items" not in record.content for record in records)
            return records

        def reject_full_capability_score_load(*_args, **_kwargs):
            raise AssertionError("ledger loaded records outside the compact capability-score projection")

        monkeypatch.setattr(runtime.store, "list_capability_scores_compact", tracked_compact)
        monkeypatch.setattr(runtime.store, "list_records", reject_full_capability_score_load)
        ledger = build_capability_ledger(runtime, scope=scope, attribute_outcomes=False)
    finally:
        runtime.close()

    assert compact_calls == 1
    assert ledger["capabilities"]["memory.recall"]["score"] == 0.9
    assert ledger["capabilities"]["memory.recall"]["evidence_source_counts"] == {"outcome_trace": 3}


def test_record_capability_score_compacts_unbounded_evidence_payload(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    evidence_items = [
        {
            "source_id": f"trace-{index}",
            "source_kind": "outcome_trace",
            "evidence_tier": "T0",
            "score": 0.9,
            "summary": "verified outcome " + ("s" * 5_000),
            "observation": {"unbounded": "x" * 200_000},
            "policy_attribution": {"nested": "y" * 200_000},
            "contract_verified": True,
        }
        for index in range(5)
    ]

    try:
        record_id = record_capability_score(
            runtime,
            scope=scope,
            loop_id="bounded-evidence",
            capability="memory.recall",
            score=0.9,
            evidence_record_ids=[f"trace-{index}" for index in range(5)],
            evidence_items=evidence_items,
            evidence_sources=["outcome_trace"],
        )
        stored = runtime.store.get_by_id(record_id, scope=scope)
    finally:
        runtime.close()

    assert stored is not None
    assert len(json.dumps(stored.to_dict(), ensure_ascii=False)) < 100_000
    assert stored.meta["evidence_items_input_count"] == 5
    assert stored.meta["evidence_source_counts"] == {"outcome_trace": 5}
    assert len(stored.meta["evidence_items_digest"]) == 64
    assert stored.meta["evidence_items_fields_filtered"] is True
    assert stored.meta["evidence_items_dropped_field_count"] > 0
    assert all("observation" not in item and "policy_attribution" not in item for item in stored.content["evidence_items"])


def test_record_capability_score_bounds_identifier_lists_and_preserves_full_digests(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    evidence_record_ids = [f"trace-{index}-" + ("x" * 5_000) for index in range(600)]
    evidence_tiers = ["tier-" + ("t" * 5_000) for _ in range(200)]
    evidence_sources = ["source-" + ("s" * 5_000) for _ in range(200)]
    try:
        record_id = record_capability_score(
            runtime,
            scope=scope,
            loop_id="bounded-identifiers",
            capability="memory.recall",
            score=0.9,
            evidence_record_ids=evidence_record_ids,
            evidence_tiers=evidence_tiers,
            evidence_sources=evidence_sources,
        )
        stored = runtime.store.get_by_id(record_id, scope=scope)
    finally:
        runtime.close()

    assert stored is not None
    assert len(json.dumps(stored.to_dict(), ensure_ascii=False)) < 500_000
    assert stored.meta["evidence_record_ids_input_count"] == 600
    assert stored.meta["evidence_record_ids_stored_count"] == 500
    assert stored.meta["evidence_record_ids_truncated"] is True
    assert stored.meta["evidence_record_ids_digest"] == sha256(
        json.dumps(evidence_record_ids, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    assert max(map(len, stored.content["evidence_record_ids"])) == 512
    assert len(stored.content["evidence_tiers"]) == 100
    assert len(stored.content["evidence_sources"]) == 100


def test_record_capability_score_rejects_oversized_caller_meta(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        with pytest.raises(ValueError, match="caller meta exceeds"):
            record_capability_score(
                runtime,
                scope={"agent_id": "hongtu"},
                loop_id="oversized-meta",
                capability="memory.recall",
                score=0.9,
                meta={"unbounded": "x" * 200_000},
            )
    finally:
        runtime.close()


def test_record_capability_score_distinguishes_rejected_items_from_trimmed_fields(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    try:
        record_id = record_capability_score(
            runtime,
            scope=scope,
            loop_id="rejected-evidence-item",
            capability="memory.recall",
            score=0.9,
            evidence_items=[{"source_id": "x" * 300_000, "observation": {"large": True}}],
        )
        stored = runtime.store.get_by_id(record_id, scope=scope)
    finally:
        runtime.close()

    assert stored is not None
    assert stored.meta["evidence_items_rejected_count"] == 1
    assert stored.meta["evidence_items_fields_filtered"] is False
    assert stored.meta["evidence_items_dropped_field_count"] == 0
    assert stored.content["evidence_items"] == []


def test_record_capability_score_canonical_metadata_cannot_be_spoofed(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    try:
        record_id = record_capability_score(
            runtime,
            scope=scope,
            loop_id="canonical-metadata",
            capability="memory.recall",
            score=0.9,
            evidence_record_ids=["trace-1"],
            meta={
                "capability": "spoofed.capability",
                "score": -99,
                "score_sequence": 999,
                "evidence_count": 999,
                "regression_count": 999,
            },
        )
        stored = runtime.store.get_by_id(record_id, scope=scope)
    finally:
        runtime.close()

    assert stored is not None
    assert stored.meta["capability"] == "memory.recall"
    assert stored.meta["score"] == 0.9
    assert stored.meta["score_sequence"] == 1
    assert stored.meta["evidence_count"] == 1
    assert stored.meta["regression_count"] == 0


def test_record_capability_score_rejects_unbounded_source_kind_labels(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        with pytest.raises(ValueError, match="source_kind"):
            record_capability_score(
                runtime,
                scope={"agent_id": "hongtu"},
                loop_id="oversized-source-kind",
                capability="memory.recall",
                score=0.9,
                evidence_items=[{"source_kind": "x" * 200_000, "source_id": "trace-1"}],
            )
    finally:
        runtime.close()


def test_record_capability_score_bounds_single_source_count_label(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    try:
        record_id = record_capability_score(
            runtime,
            scope=scope,
            loop_id="bounded-single-source",
            capability="memory.recall",
            score=0.9,
            evidence_record_ids=["trace-1"],
            evidence_sources=["source-" + ("x" * 200_000)],
        )
        stored = runtime.store.get_by_id(record_id, scope=scope)
    finally:
        runtime.close()

    assert stored is not None
    assert max(map(len, stored.meta["evidence_source_counts"])) == 128
    assert len(json.dumps(stored.to_dict(), ensure_ascii=False)) < 50_000


def test_record_capability_score_counts_single_source_by_items_when_items_exist(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}
    try:
        record_id = record_capability_score(
            runtime,
            scope=scope,
            loop_id="single-source-item-count",
            capability="memory.recall",
            score=0.9,
            evidence_record_ids=["trace-1", "trace-2", "trace-3"],
            evidence_items=[{"source_id": "trace-1", "summary": "one evidence item"}],
            evidence_sources=["outcome_trace"],
            meta={
                "evidence_source_counts": {"untrusted": 999},
                "evidence_items_fields_filtered": True,
            },
        )
        stored = runtime.store.get_by_id(record_id, scope=scope)
    finally:
        runtime.close()

    assert stored is not None
    assert stored.meta["evidence_source_counts"] == {"outcome_trace": 1}
    assert stored.meta["evidence_items_fields_filtered"] is False


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
