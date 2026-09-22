"""B2: memory_rule watch must initialize with ok and persist watch metadata."""
from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.promotion_watch import (
    WATCH_STATUS,
    check_promotion_watch_orphans,
    initialize_promotion_watch,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_initialize_promotion_watch_supports_memory_rule_record_id(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "ws", "user_id": "u"}
    rule = runtime.evolution.store_rule(
        title="Rule",
        summary="Prefer exact refs",
        task_type="memory.recall",
        retrieval_policy={"learned_policy": "exact"},
        scope=scope,
        status="shadow",
    )
    candidate = runtime.store.append(
        RecordEnvelope.create(
            kind="capability_candidate",
            title="cand",
            summary="cand",
            scope=ScopeRef.from_dict(scope),
            status=WATCH_STATUS,
            meta={"applied_artifact_ids": [rule.record_id], "promotion_target": "memory_rule"},
            content={"promotion_target": "memory_rule"},
        )
    )
    watch = initialize_promotion_watch(
        runtime,
        candidate=candidate,
        scope=scope,
        promotion_request_id="promo-1",
        applied_pattern_ids=[rule.record_id],
    )
    assert watch.get("ok") is True
    assert watch["patterns"][0]["artifact_kind"] == "memory_rule"
    reloaded = runtime.store.get_by_id(rule.record_id, scope=scope)
    assert reloaded is not None
    assert reloaded.content["post_promotion_watch"]["status"] == WATCH_STATUS
    orphans = check_promotion_watch_orphans(runtime, scope=scope)
    assert orphans["ok"] is True
    assert orphans["orphan_count"] == 0


def test_initialize_promotion_watch_fail_closed_when_artifact_missing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "ws", "user_id": "u"}
    candidate = runtime.store.append(
        RecordEnvelope.create(
            kind="capability_candidate",
            title="cand",
            summary="cand",
            scope=ScopeRef.from_dict(scope),
            status="candidate",
            meta={"applied_artifact_ids": ["missing-rule"]},
        )
    )
    watch = initialize_promotion_watch(
        runtime,
        candidate=candidate,
        scope=scope,
        promotion_request_id="promo-2",
        applied_pattern_ids=["missing-rule"],
    )
    assert watch.get("ok") is False
    assert watch["missing_artifact_ids"] == ["missing-rule"]
