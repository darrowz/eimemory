from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning_state import (
    append_learning_record_once,
    complete_learning_loop,
    mark_step,
    start_learning_loop,
)
from eimemory.models.records import VALID_KINDS


def test_autonomous_learning_record_kinds_are_registered() -> None:
    expected = {
        "learning_loop",
        "source_watch",
        "world_signal",
        "capability_model",
        "weakness",
        "learning_goal",
        "research_task",
        "research_note",
        "learning_experiment",
        "learning_eval",
        "capability_candidate",
        "promotion_request",
        "capability_score",
        "regression_watch",
        "learning_playbook",
    }
    assert expected.issubset(VALID_KINDS)


def test_start_learning_loop_blocks_duplicate_active_loop(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    loop = start_learning_loop(runtime, scope=scope, trigger="nightly")

    with pytest.raises(RuntimeError, match=loop.record_id):
        start_learning_loop(runtime, scope=scope, trigger="nightly")


def test_learning_loop_can_complete_and_restart(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    loop = start_learning_loop(runtime, scope=scope, trigger="nightly")
    complete_learning_loop(runtime, loop, status="completed")

    next_loop = start_learning_loop(runtime, scope=scope, trigger="nightly")

    assert next_loop.record_id != loop.record_id
    assert next_loop.status == "running"


def test_start_learning_loop_recovers_stale_active_loop(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "personal"}
    stale = start_learning_loop(runtime, scope=scope, trigger="nightly")
    stale.time.created_at = "2000-01-01T00:00:00+00:00"
    stale.time.updated_at = "2000-01-01T00:00:00+00:00"
    stale.meta["status"] = "running"
    runtime.store.rewrite(stale)

    next_loop = start_learning_loop(runtime, scope=scope, trigger="nightly")
    recovered = runtime.store.get_by_id(stale.record_id)

    assert next_loop.record_id != stale.record_id
    assert next_loop.status == "running"
    assert recovered.status == "failed"
    assert recovered.meta["status"] == "failed"
    assert recovered.meta["stale_recovered_reason"] == "stale_learning_loop"


def test_mark_step_is_idempotent_for_same_step(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    loop = start_learning_loop(runtime, scope={"agent_id": "hongtu"}, trigger="manual")

    first = mark_step(runtime, loop, step_name="observe", status="running", record_ids=["a"])
    second = mark_step(runtime, first, step_name="observe", status="completed", record_ids=["a", "b"])

    assert second.record_id == loop.record_id
    assert len(second.content["steps"]) == 1
    assert second.content["steps"][0]["status"] == "completed"
    assert second.content["steps"][0]["record_ids"] == ["a", "b"]


def test_append_learning_record_once_returns_existing_record(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    loop = start_learning_loop(runtime, scope=scope, trigger="manual")
    loop_id = str(loop.meta["loop_id"])

    first = append_learning_record_once(
        runtime,
        kind="learning_goal",
        title="Improve tool routing",
        summary="Route stable personal facts through memory first",
        scope=scope,
        loop_id=loop_id,
        step_name="goals",
        semantic_key="tool.routing",
    )
    second = append_learning_record_once(
        runtime,
        kind="learning_goal",
        title="Improve tool routing duplicate",
        summary="Should not create a second record",
        scope=scope,
        loop_id=loop_id,
        step_name="goals",
        semantic_key="tool.routing",
    )

    assert second.record_id == first.record_id
    assert len(runtime.store.list_records(kinds=["learning_goal"], scope=scope, limit=10)) == 1


def test_append_learning_record_once_uses_indexed_idempotency_lookup(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    loop = start_learning_loop(runtime, scope=scope, trigger="manual")
    loop_id = str(loop.meta["loop_id"])
    first = append_learning_record_once(
        runtime,
        kind="replay_result",
        title="Capability replay",
        summary="Replay once",
        scope=scope,
        loop_id=loop_id,
        step_name="capability_replay",
        semantic_key="memory.recall.case",
    )

    def fail_paged_lookup(*_args, **_kwargs):
        raise AssertionError("idempotency lookup must not page through records")

    monkeypatch.setattr(runtime.store, "list_records", fail_paged_lookup)
    second = append_learning_record_once(
        runtime,
        kind="replay_result",
        title="Capability replay duplicate",
        summary="Replay duplicate",
        scope=scope,
        loop_id=loop_id,
        step_name="capability_replay",
        semantic_key="memory.recall.case",
    )

    assert second.record_id == first.record_id


def test_append_learning_record_once_does_not_scan_on_indexed_idempotency_miss(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    loop = start_learning_loop(runtime, scope=scope, trigger="manual")
    loop_id = str(loop.meta["loop_id"])

    def fail_paged_lookup(*_args, **_kwargs):
        raise AssertionError("idempotency miss must not page through records")

    monkeypatch.setattr(runtime.store, "list_records", fail_paged_lookup)
    record = append_learning_record_once(
        runtime,
        kind="replay_result",
        title="New capability replay",
        summary="Replay miss should still append",
        scope=scope,
        loop_id=loop_id,
        step_name="capability_replay",
        semantic_key="memory.recall.new-case",
    )

    assert record.kind == "replay_result"
