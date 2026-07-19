from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.learning_state import append_learning_record_once
from eimemory.governance.learning_retention import compact_learning_records


def test_retention_disables_expired_learning_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    append_learning_record_once(
        runtime,
        kind="learning_goal",
        title="Old goal",
        summary="Expired",
        scope=scope,
        loop_id="learn_test",
        step_name="goal",
        semantic_key="old",
        meta={"expires_at": "2000-01-01T00:00:00+00:00"},
    )

    report = compact_learning_records(runtime, scope=scope, loop_id="learn_test", dry_run=False)

    assert report["expired_count"] == 1
    assert report["disabled_count"] == 1
    assert runtime.store.list_records(kinds=["learning_goal"], scope=scope, status="disabled", limit=10)


def test_retention_paginates_learning_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    for index in range(505):
        append_learning_record_once(
            runtime,
            kind="learning_goal",
            title=f"Goal {index}",
            summary="Expired",
            scope=scope,
            loop_id="learn_test",
            step_name="goal",
            semantic_key=f"old-{index}",
            meta={"expires_at": "2000-01-01T00:00:00+00:00"},
        )

    report = compact_learning_records(runtime, scope=scope, loop_id="learn_test", dry_run=False, max_records=600)

    assert report["expired_count"] == 505
    assert report["disabled_count"] == 505


def test_retention_does_not_materialize_immutable_capability_scores(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    original = runtime.store.list_records

    def reject_score_load(*args, **kwargs):
        assert kwargs.get("kinds") is not None
        assert "capability_score" not in list(kwargs["kinds"])
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.store, "list_records", reject_score_load)
    monkeypatch.setattr(
        runtime.store,
        "list_capability_scores_compact",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("retention loaded compact capability scores")
        ),
    )
    try:
        report = compact_learning_records(runtime, scope={"agent_id": "hongtu"}, dry_run=True)
    finally:
        runtime.close()

    assert report["ok"] is True
