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
    assert second["duplicate_count"] == 0
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


def test_world_watch_uses_incremental_cursor_and_fixed_supervisor_summary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu"}
    watch = SourceWatch(name="outcomes", kind="local_outcome_trace", enabled=True, dry_run=False)
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

    first = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_watch")
    second = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_watch")

    assert first["signal_count"] == 1
    assert second["signal_count"] == 0
    assert second["duplicate_count"] == 0
    assert second["watcher_cursors"][0]["watch_name"] == "outcomes"
    assert second["watcher_cursors"][0]["last_seen"]
    assert second["watcher_cursors"][0]["high_watermark"]
    for key in ("last_success_at", "last_error_at", "duration_ms", "memory_peak", "produced_count", "promoted_count", "rolled_back_count"):
        assert key in second["supervisor_summary"]
    assert second["supervisor_summary"]["duration_ms"] < 15000
    assert second["supervisor_summary"]["memory_peak"] < 250 * 1024 * 1024

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
    third = collect_world_signals(runtime, scope=scope, watches=[watch], dry_run=False, loop_id="learn_watch")
    stored = runtime.store.list_records(kinds=["world_signal"], scope=scope, limit=10)

    assert third["signal_count"] == 0
    assert third["updated_record_ids"] == [stored[0].record_id]
    assert stored[0].meta["repeat_count"] == 2


def test_world_watch_builds_incremental_magma_memory_edges(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "graph"}
    scope_ref = ScopeRef.from_dict(scope)
    cause = runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Rollback plan missing",
            summary="The eimemory deployment failure root cause was an empty rollback command.",
            scope=scope_ref,
            source="test",
            meta={"service": "eimemory-rpc"},
        )
    )
    symptom = runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="eimemory-rpc deploy failed",
            summary="Health check on 8091 failed after deployment.",
            scope=scope_ref,
            source="test",
            content={"cause_record_id": cause.record_id, "service": "eimemory-rpc"},
        )
    )

    first = collect_world_signals(
        runtime,
        scope=scope,
        watches=[SourceWatch(name="disabled", kind="local_state", enabled=False)],
        dry_run=False,
        loop_id="learn_watch",
    )
    second = collect_world_signals(
        runtime,
        scope=scope,
        watches=[SourceWatch(name="disabled", kind="local_state", enabled=False)],
        dry_run=False,
        loop_id="learn_watch",
    )
    edges = runtime.store.list_memory_edges(scope=scope, edge_types=["causal", "entity", "temporal"], record_ids=[symptom.record_id], limit=10)

    assert first["edge_builder"]["scanned_count"] >= 2
    assert first["edge_builder"]["batch_limit"] <= 24
    assert first["edge_builder"]["reference_limit"] <= 160
    assert first["edge_builder"]["edge_counts"]["causal"] >= 1
    assert second["edge_builder"]["scanned_count"] == 0
    assert any(edge.edge_type == "causal" and edge.from_id == cause.record_id and edge.to_id == symptom.record_id for edge in edges)


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
