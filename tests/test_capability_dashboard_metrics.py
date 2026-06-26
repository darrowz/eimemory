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
        assert metrics["persisted_record_id"]
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
