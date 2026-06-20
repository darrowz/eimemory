from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.world_watchers import SourceWatch, collect_world_signals
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_disabled_watchers_write_nothing(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    report = collect_world_signals(
        runtime,
        scope={"agent_id": "hongtu"},
        watches=[SourceWatch(name="repo", kind="local_repo", enabled=False)],
        dry_run=False,
    )

    assert report["signal_count"] == 0
    assert report["skipped_disabled_count"] == 1
    assert runtime.store.list_records(kinds=["world_signal"], scope={"agent_id": "hongtu"}, limit=10) == []


def test_dry_run_returns_signals_without_persisting(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    runtime.evolution.log_reflection(tag="memory.recall", miss="Recall missed", fix="Improve ranking", scope=scope)
    trace = RecordEnvelope.create(
        kind="reflection",
        title="Outcome trace",
        summary="Tool routing failed",
        scope=ScopeRef.from_dict(scope),
        source="test",
        meta={"report_type": "outcome_trace", "schema_version": "outcome_trace.v1", "primary_label": "missing_tool_call"},
    )
    runtime.store.append(trace)

    report = collect_world_signals(
        runtime,
        scope=scope,
        watches=[SourceWatch(name="outcomes", kind="local_outcome_trace", enabled=True, dry_run=False)],
        dry_run=True,
    )

    assert report["signal_count"] == 1
    assert report["persisted_record_ids"] == []
    assert runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=10) == []


def test_world_signal_dedupe_by_hash(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Outcome trace",
            summary="Tool routing failed",
            scope=ScopeRef.from_dict(scope),
            source="test",
            meta={"report_type": "outcome_trace", "schema_version": "outcome_trace.v1", "primary_label": "missing_tool_call"},
        )
    )
    watch = SourceWatch(name="outcomes", kind="local_outcome_trace", enabled=True, dry_run=False)

    first = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_test")
    second = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_test")

    assert first["signal_count"] == 1
    assert second["signal_count"] == 0
    assert second["duplicate_count"] == 1
    assert len(runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=10)) == 1


def test_repeated_bad_outcomes_increment_signal_repeat_count(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    for _ in range(2):
        runtime.store.append(
            RecordEnvelope.create(
                kind="reflection",
                title="Outcome trace",
                summary="Tool routing failed",
                scope=ScopeRef.from_dict(scope),
                source="test",
                meta={"report_type": "outcome_trace", "schema_version": "outcome_trace.v1", "primary_label": "missing_tool_call"},
            )
        )
    watch = SourceWatch(name="outcomes", kind="local_outcome_trace", enabled=True, dry_run=False)

    first = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_test")
    second = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_test")
    stored = runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=10)

    assert first["signal_count"] == 1
    assert first["signals"][0]["repeat_count"] == 2
    assert second["signal_count"] == 0
    assert second["updated_record_ids"] == []
    assert stored[0].meta["repeat_count"] == 2


def test_repeated_bad_outcomes_merge_only_new_source_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    for _ in range(2):
        runtime.store.append(
            RecordEnvelope.create(
                kind="reflection",
                title="Outcome trace",
                summary="Tool routing failed",
                scope=ScopeRef.from_dict(scope),
                source="test",
                meta={"report_type": "outcome_trace", "schema_version": "outcome_trace.v1", "primary_label": "missing_tool_call"},
            )
        )
    watch = SourceWatch(name="outcomes", kind="local_outcome_trace", enabled=True, dry_run=False)
    first = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_test")

    runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Outcome trace",
            summary="Tool routing failed",
            scope=ScopeRef.from_dict(scope),
            source="test",
            meta={"report_type": "outcome_trace", "schema_version": "outcome_trace.v1", "primary_label": "missing_tool_call"},
        )
    )
    second = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_test")
    stored = runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=10)

    assert first["signal_count"] == 1
    assert second["signal_count"] == 0
    assert second["updated_record_ids"] == [stored[0].record_id]
    assert stored[0].meta["repeat_count"] == 3


def test_world_signals_truncate_dedupe_and_classify_capability(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    long_summary = "RPC /health timeout on 8091. " + "assistant: old answer user: old prompt " * 20
    for index in range(2):
        runtime.store.append(
            RecordEnvelope.create(
                kind="unknown",
                title=f"Health check timeout {index}",
                summary=long_summary,
                scope=ScopeRef.from_dict(scope),
                source="test",
            )
        )
    watch = SourceWatch(name="recall gaps", kind="local_recall_gap", enabled=True, dry_run=False)

    report = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=True, loop_id="learn_test")

    assert report["signal_count"] == 1
    signal = report["signals"][0]
    assert signal["target_capability"] == "ops.health"
    assert signal["summary_truncated"] is True
    assert len(signal["summary"]) <= 360
    assert report["duplicate_count"] == 1
