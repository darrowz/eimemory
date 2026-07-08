from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_record_memory_usage_persists_feedback_record(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "repo-x"}

    feedback = runtime.record_memory_usage(
        query_id="query-1",
        query="openclaw memory recall",
        scope=scope,
        used_record_ids=["mem-used", "mem-used"],
        rejected_record_ids=["mem-rejected"],
        source="test.gateway",
    )
    duplicate = runtime.record_memory_usage(
        query_id="query-1",
        query="openclaw memory recall",
        scope=scope,
        used_record_ids=["different"],
        rejected_record_ids=[],
    )

    records = runtime.store.list_records(kinds=["feedback"], scope=scope, limit=10)
    assert feedback.record_id == duplicate.record_id
    assert len(records) == 1
    assert records[0].content["report_type"] == "memory_usage_telemetry"
    assert records[0].content["used_record_ids"] == ["mem-used"]
    assert records[0].content["rejected_record_ids"] == ["mem-rejected"]
    assert records[0].meta["report_type"] == "memory_usage_telemetry"
    assert records[0].meta["schema_version"] == "memory_usage_telemetry.v1"


def test_memory_usage_telemetry_adjusts_recall_scoring(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="main", workspace_id="repo-x")
    used = RecordEnvelope.create(
        kind="memory",
        title="Telemetry promoted memory",
        summary="OpenClaw recall telemetry should learn from actually used memories.",
        scope=scope,
        meta={"quality": {"salience_score": 0.5, "capture_decision": "accept"}},
    )
    rejected = RecordEnvelope.create(
        kind="memory",
        title="Telemetry rejected memory",
        summary="OpenClaw recall telemetry should learn from rejected memories.",
        scope=scope,
        meta={"quality": {"salience_score": 0.5, "capture_decision": "accept"}},
    )
    runtime.store.append(rejected)
    runtime.store.append(used)
    runtime.record_memory_usage(
        query_id="query-2",
        query="OpenClaw recall telemetry memories",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        used_record_ids=[used.record_id],
        rejected_record_ids=[rejected.record_id],
    )

    bundle = runtime.memory.recall(
        query="OpenClaw recall telemetry memories",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    scoring = {entry["record_id"]: entry for entry in bundle.explanation["scoring"]}
    assert scoring[used.record_id]["telemetry_adjustment"] > 0
    assert scoring[used.record_id]["quality_score"] > scoring[used.record_id]["base_quality_score"]
    assert scoring[rejected.record_id]["telemetry_adjustment"] < 0
    assert scoring[rejected.record_id]["quality_score"] < scoring[rejected.record_id]["base_quality_score"]
    assert bundle.explanation["memory_telemetry"]["selected_adjusted_count"] == 2


def test_recall_explanation_reports_pipeline_phases(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.memory.ingest(
        text="OpenClaw recall pipeline explanations should expose deterministic phases.",
        memory_type="fact",
        title="Recall pipeline telemetry",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
    )

    bundle = runtime.memory.recall(
        query="OpenClaw recall pipeline explanations",
        scope={"agent_id": "main", "workspace_id": "repo-x"},
        task_context={"task_type": "chat.reply"},
        limit=5,
    )

    pipeline = bundle.explanation["pipeline"]
    assert pipeline["schema_version"] == "recall_pipeline.v1"
    assert pipeline["phase_names"] == ["prepare", "retrieve", "graph_expand", "score_filter", "package"]
    assert [phase["name"] for phase in pipeline["phases"]] == pipeline["phase_names"]
    assert pipeline["phases"][-1]["selected_count"] == len(bundle.items)
