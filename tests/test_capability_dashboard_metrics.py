from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "agent-dashboard", "workspace_id": "capability-dashboard"}


def test_capability_dashboard_metrics_report_hard_numbers(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        _append(runtime, scope_ref, "replay_result", "recall hit", {"capability": "memory.recall", "hit": True, "verdict": "pass"})
        _append(runtime, scope_ref, "replay_result", "recall miss", {"capability": "memory.recall", "hit": False, "verdict": "fail"})
        _append(runtime, scope_ref, "feedback", "user correction", {"report_type": "user_correction", "capability": "memory.recall"})
        _append(runtime, scope_ref, "learning_eval", "task success", {"task_success": True, "verdict": "pass"})
        _append(runtime, scope_ref, "learning_eval", "task failed", {"task_success": False, "verdict": "fail"})
        _append(runtime, scope_ref, "promotion_request", "patch promoted", {"promotion_target": "code_patch", "action": "promote"}, status="promoted")
        _append(runtime, scope_ref, "promotion_request", "patch rollback", {"promotion_target": "code_patch", "action": "rollback"}, status="rolled_back")
        _append(runtime, scope_ref, "learning_eval", "skill call", {"report_type": "eiskill_invocation", "skill_id": "skill-1"})

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=True)

        assert metrics["ok"] is True
        assert metrics["metrics"]["recall_hit_rate"] == 0.5
        assert metrics["metrics"]["user_correction_rate"] == 0.5
        assert metrics["metrics"]["task_success_rate"] == 0.5
        assert metrics["metrics"]["auto_patch_success_rate"] == 0.5
        assert metrics["metrics"]["rollback_count"] == 1
        assert metrics["metrics"]["skill_reuse_count"] == 1
        assert metrics["metric_quality"]["task_success_rate"]["sample_count"] == 2
        assert metrics["metric_quality"]["task_success_rate"]["sufficient"] is False
        assert metrics["metric_quality"]["auto_patch_success_rate"]["sample_count"] == 2
        assert metrics["metric_quality"]["auto_patch_success_rate"]["sufficient"] is False
        assert metrics["persisted_record_id"]
    finally:
        runtime.close()


def test_capability_dashboard_metrics_include_real_task_outcome_traces(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        _append(
            runtime,
            scope_ref,
            "reflection",
            "successful outcome trace",
            {
                "report_type": "outcome_trace",
                "schema_version": "outcome_trace.v1",
                "task_success": True,
                "outcome": {"success": True},
            },
        )
        _append(
            runtime,
            scope_ref,
            "reflection",
            "failed outcome trace",
            {
                "report_type": "outcome_trace",
                "schema_version": "outcome_trace.v1",
                "task_success": False,
                "outcome": {"success": False},
            },
        )
        event = runtime.store.record_event(
            {
                "event_type": "agent_end",
                "summary": "task completed",
                "task_type": "coding",
            },
            scope=scope_ref,
        )
        runtime.record_outcome(event["id"], {"outcome": "good", "success": True, "verified": True}, scope=SCOPE)

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)

        assert metrics["metrics"]["task_success_rate"] == 0.667
        assert metrics["metric_quality"]["task_success_rate"]["sample_count"] == 3
        assert metrics["sample_counts"]["task_outcomes"] == 3
    finally:
        runtime.close()


def test_capability_dashboard_maps_real_completion_labels_to_success(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        _append(
            runtime,
            scope_ref,
            "reflection",
            "completed outcome trace",
            {
                "report_type": "outcome_trace",
                "schema_version": "outcome_trace.v1",
                "status": "completed",
                "verification": "health check passed",
            },
        )
        _append(
            runtime,
            scope_ref,
            "reflection",
            "delivered outcome trace",
            {
                "report_type": "outcome_trace",
                "schema_version": "outcome_trace.v1",
                "result": "delivered",
            },
        )
        _append(
            runtime,
            scope_ref,
            "reflection",
            "health ok outcome trace",
            {
                "report_type": "outcome_trace",
                "schema_version": "outcome_trace.v1",
                "outcome": "health_ok",
            },
        )
        completed_event = runtime.store.record_event(
            {"event_type": "agent_end", "summary": "runtime completed", "task_type": "ops"},
            scope=scope_ref,
        )
        missing_event = runtime.store.record_event(
            {"event_type": "agent_end", "summary": "verification missing", "task_type": "ops"},
            scope=scope_ref,
        )
        runtime.record_outcome(completed_event["id"], {"ok": True, "status": "completed"}, scope=SCOPE)
        runtime.record_outcome(missing_event["id"], {"outcome": "verification_missing", "success": True}, scope=SCOPE)

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)

        assert metrics["sample_counts"]["task_outcomes"] == 5
        assert metrics["metrics"]["task_success_rate"] == 0.8
    finally:
        runtime.close()


def test_capability_dashboard_counts_registry_reuse_when_invocation_records_are_compacted(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        _append(
            runtime,
            scope_ref,
            "learning_playbook",
            "registry skill",
            {
                "report_type": "eiskill_registry_entry",
                "skill_id": "skill-research-1",
                "reuse_count": 3,
            },
        )

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)

        assert metrics["metrics"]["skill_reuse_count"] == 3
        assert metrics["metric_quality"]["skill_reuse_count"]["sample_count"] == 3
        assert metrics["metric_quality"]["skill_reuse_count"]["sufficient"] is True
    finally:
        runtime.close()


def _append(runtime: Runtime, scope: ScopeRef, kind: str, title: str, meta: dict, *, status: str = "active") -> None:
    runtime.store.append(
        RecordEnvelope.create(
            kind=kind,
            title=title,
            summary=title,
            scope=scope,
            source="test.capability_dashboard",
            status=status,
            content=dict(meta),
            meta=dict(meta),
        )
    )
