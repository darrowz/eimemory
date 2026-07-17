from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.capability_dashboard import _verified_code_patch_promotion
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
        _append(runtime, scope_ref, "promotion_request", "patch promoted", _verified_patch_evidence(), status="promoted")
        _append(runtime, scope_ref, "promotion_request", "patch rollback", {"promotion_target": "code_patch", "action": "rollback"}, status="rolled_back")
        _append(runtime, scope_ref, "learning_eval", "skill call", {"report_type": "eiskill_invocation", "skill_id": "skill-1"})
        runtime.upsert_intent_pattern(
            {
                "id": "dashboard-rollback",
                "pattern": "dashboard rollback",
                "default_event_type": "repair",
                "interpreted_intent": "verify dashboard rollback evidence",
                "confidence": 0.9,
                "status": "active",
            },
            scope=SCOPE,
        )
        runtime.rollback_intent_pattern("dashboard-rollback", scope=SCOPE, reason="verified dashboard rollback", auto=False)

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=True)

        assert metrics["ok"] is True
        assert metrics["metrics"]["recall_hit_rate"] == 0.5
        assert metrics["metrics"]["user_correction_rate"] == 0.5
        assert metrics["metrics"]["task_success_rate"] == 0.5
        assert metrics["metrics"]["auto_patch_success_rate"] == 1.0
        assert metrics["metrics"]["patch_promotion_success_rate"] == 1.0
        assert metrics["metrics"]["rollback_count"] == 1
        assert metrics["metrics"]["skill_reuse_count"] == 1
        assert metrics["metric_quality"]["task_success_rate"]["sample_count"] == 2
        assert metrics["metric_quality"]["task_success_rate"]["sufficient"] is False
        assert metrics["metric_quality"]["auto_patch_success_rate"]["sample_count"] == 1
        assert metrics["metric_quality"]["auto_patch_success_rate"]["sufficient"] is True
        assert metrics["persisted_record_id"]
    finally:
        runtime.close()


def test_capability_dashboard_rejects_generic_and_status_only_patch_success(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        _append(runtime, scope_ref, "promotion_request", "generic promoted", {"action": "promote"}, status="promoted")
        _append(
            runtime,
            scope_ref,
            "promotion_request",
            "status only patch",
            {"promotion_target": "code_patch", "action": "code_patch"},
            status="deployed",
        )
        runtime.store.sqlite.upsert_policy_rollout_ledger_payload(
            {
                "id": "dashboard-blocked-rollback",
                "scope": SCOPE,
                "action_type": "rollback",
                "promotion_id": "dashboard-blocked",
                "budget_decision": "blocked",
                "applied_pattern_id": "",
                "details": {"blocked": True},
            }
        )

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["sample_counts"]["patch_candidates"] == 1
    assert metrics["sample_counts"]["patch_promotions"] == 0
    assert metrics["metrics"]["patch_promotion_success_rate"] == 0.0
    assert metrics["metrics"]["auto_patch_success_rate"] == 0.0
    assert metrics["metrics"]["rollback_count"] == 0


def test_patch_metrics_use_latest_candidate_and_exclude_preflight_invalid_from_deployments(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        _append(
            runtime,
            scope_ref,
            "promotion_request",
            "candidate retry failed",
            _executed_patch_evidence(candidate_id="candidate-retry", commit="1" * 40, success=False),
            status="blocked",
            created_at="2026-07-11T01:00:00+00:00",
        )
        _append(
            runtime,
            scope_ref,
            "promotion_request",
            "candidate retry succeeded",
            _executed_patch_evidence(candidate_id="candidate-retry", commit="2" * 40, success=True),
            status="deployed",
            created_at="2026-07-11T01:00:01+00:00",
        )
        _append(
            runtime,
            scope_ref,
            "promotion_request",
            "candidate deployment failed",
            _executed_patch_evidence(candidate_id="candidate-failed", commit="3" * 40, success=False),
            status="blocked",
        )
        _append(
            runtime,
            scope_ref,
            "promotion_request",
            "candidate preflight invalid",
            {
                "candidate_id": "candidate-invalid",
                "promotion_target": "code_patch",
                "action": "code_patch",
                "gate": {"ok": False, "reason": "invalid_patch_contract"},
                "side_effect": {"ok": False, "production_applied": False},
            },
            status="blocked",
        )

        report = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert report["metrics"]["patch_candidate_validity_rate"] == 0.667
    assert report["sample_counts"]["patch_candidates"] == 3
    assert report["metrics"]["patch_deployment_success_rate"] == 0.5
    assert report["metrics"]["patch_promotion_success_rate"] == 0.5
    assert report["metrics"]["auto_patch_success_rate"] == 0.5
    assert report["sample_counts"]["patch_deployments"] == 2
    assert report["metric_quality"]["patch_deployment_success_rate"] == {
        "sample_count": 2,
        "minimum": 1,
        "sufficient": True,
    }
    assert report["metric_quality"]["auto_patch_success_rate"] == report["metric_quality"]["patch_deployment_success_rate"]


def test_one_complete_executed_deployment_is_sufficient_metric_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _append(
            runtime,
            ScopeRef.from_dict(SCOPE),
            "promotion_request",
            "single verified deployment",
            _executed_patch_evidence(candidate_id="candidate-one", commit="4" * 40, success=True),
            status="deployed",
        )

        report = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert report["metrics"]["patch_deployment_success_rate"] == 1.0
    assert report["metric_quality"]["patch_deployment_success_rate"]["sufficient"] is True


def test_content_gate_false_cannot_be_overridden_by_meta_for_candidate_validity(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    payload = _executed_patch_evidence(candidate_id="gate-conflict", commit="5" * 40, success=True)
    payload["gate"] = {"ok": False, "reason": "explicit_preflight_failure"}
    try:
        _append(
            runtime,
            ScopeRef.from_dict(SCOPE),
            "promotion_request",
            "conflicting gate candidate",
            payload,
            status="deployed",
            record_meta={"candidate_id": "gate-conflict", "promotion_target": "code_patch", "gate_ok": True},
        )
        report = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert report["metrics"]["patch_candidate_validity_rate"] == 0.0
    assert report["sample_counts"]["patch_deployments"] == 0


def test_content_gate_false_cannot_be_overridden_by_meta_for_patch_success() -> None:
    payload = _executed_patch_evidence(candidate_id="gate-conflict-success", commit="6" * 40, success=True)
    payload["gate"] = {"ok": False, "reason": "explicit_preflight_failure"}
    record = RecordEnvelope.create(
        kind="promotion_request",
        title="conflicting gate success",
        summary="must fail closed",
        scope=ScopeRef.from_dict(SCOPE),
        status="deployed",
        content=payload,
        meta={"candidate_id": "gate-conflict-success", "promotion_target": "code_patch", "gate_ok": True},
    )

    assert _verified_code_patch_promotion(record) is False


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


def test_dashboard_reads_real_outcome_trace_payload_and_excludes_nested_rehearsal(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        for trace_id, rehearsal in (("real-task", False), ("probe-task", True)):
            result = runtime.record_outcome_trace(
                {
                    "trace_id": trace_id,
                    "task_type": "coding",
                    "input_summary": trace_id,
                    "outcome": {"status": "success", "success": True, "rehearsal": rehearsal},
                    "verifier": {"passed": True, "method": "test", "evidence_refs": [trace_id]},
                },
                scope=SCOPE,
            )
            assert result["ok"] is True

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["sample_counts"]["task_outcomes"] == 1
    assert metrics["metrics"]["task_success_rate"] == 1.0


def test_dashboard_counts_verified_openclaw_tasks_and_failure_blame_separately(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        def bound_event(trace_id: str, task_type: str, *, success: bool) -> dict:
            event = runtime.store.record_event(
                {
                    "source": "openclaw.agent_end",
                    "hook": "agent_end",
                    "session_id": f"session-{trace_id}",
                    "event_type": task_type,
                    "outcome_trace_id": trace_id,
                    "outcome_trace_task_type": task_type,
                    "summary": trace_id,
                    "verification": "verified terminal result",
                },
                scope=scope_ref,
            )
            runtime.record_outcome(
                event["id"],
                {
                    "outcome": "good" if success else "bad",
                    "success": success,
                    "verified": True,
                    "source": "openclaw.agent_end",
                    "source_trust": "system_verified",
                },
                scope=SCOPE,
            )
            return event

        success_event = bound_event("real-success", "repo.deploy", success=True)
        failure_event = bound_event("real-failure", "research", success=False)
        for trace_id, task_type, success, evidence_id, extra in (
            ("real-success", "repo.deploy", True, success_event["id"], {}),
            ("real-failure", "research", False, failure_event["id"], {"stale_context": True}),
        ):
            result = runtime.record_outcome_trace(
                {
                    "source": "openclaw.agent_end",
                    "trace_id": trace_id,
                    "session_id": f"session-{trace_id}",
                    "task_type": task_type,
                    "input_summary": trace_id,
                    "outcome": {"status": "success" if success else "bad", "success": success, "rehearsal": False},
                    "verifier": {
                        "passed": success,
                        "method": "openclaw.agent_end",
                        "evidence_refs": [evidence_id],
                    },
                    **extra,
                },
                scope=SCOPE,
            )
            assert result["ok"] is True

        runtime.record_outcome_trace(
            {
                "source": "openclaw.agent_end",
                "trace_id": "forged",
                "task_type": "forged",
                "outcome": {"status": "success", "success": True, "rehearsal": False},
                "verifier": {"passed": True, "method": "openclaw.agent_end", "evidence_refs": ["missing"]},
            },
            scope=SCOPE,
        )
        runtime.record_outcome_trace(
            {
                "source": "openclaw.agent_end",
                "trace_id": "forged-existing-event",
                "session_id": "session-forged",
                "task_type": "repo.deploy",
                "outcome": {"status": "success", "success": True, "rehearsal": False},
                "verifier": {
                    "passed": True,
                    "method": "openclaw.made_up",
                    "evidence_refs": [success_event["id"]],
                },
            },
            scope=SCOPE,
        )
        runtime.record_outcome_trace(
            {
                "source": "openclaw.agent_end",
                "trace_id": "duplicate-event",
                "session_id": "session-real-success",
                "task_type": "repo.deploy",
                "outcome": {"status": "success", "success": True, "rehearsal": False},
                "verifier": {
                    "passed": True,
                    "method": "openclaw.agent_end",
                    "evidence_refs": [success_event["id"]],
                },
            },
            scope=SCOPE,
        )
        runtime.record_outcome_trace(
            {
                "source": "openclaw.agent_end",
                "trace_id": "rehearsal",
                "session_id": "session-real-success",
                "task_type": "probe",
                "outcome": {"status": "success", "success": True, "rehearsal": True},
                "verifier": {
                    "passed": True,
                    "method": "openclaw.agent_end",
                    "evidence_refs": [success_event["id"]],
                },
            },
            scope=SCOPE,
        )

        report = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert report["metrics"]["verified_real_task_success_rate"] == 0.5
    assert report["metric_quality"]["verified_real_task_success_rate"]["sample_count"] == 2
    assert report["sample_counts"]["verified_real_tasks"] == 2
    assert report["sample_counts"]["verified_real_task_types"] == 2
    assert report["sample_counts"]["current_deployment_acceptance"] == 0
    assert report["failure_blame_layers"] == {"memory": 1}


def test_dashboard_explicit_failure_overrides_success_signal(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _append(
            runtime,
            ScopeRef.from_dict(SCOPE),
            "reflection",
            "conflicting outcome",
            {
                "report_type": "outcome_trace",
                "success": True,
                "verified": False,
            },
        )
        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["sample_counts"]["task_outcomes"] == 1
    assert metrics["metrics"]["task_success_rate"] == 0.0


@pytest.mark.parametrize(
    "verification",
    [
        "failed: no evidence",
        "not_run: verifier unavailable",
        {"passed": False},
        {"ok": False},
        {"status": "failure"},
    ],
)
def test_dashboard_verification_failure_label_overrides_success(tmp_path, verification) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _append(
            runtime,
            ScopeRef.from_dict(SCOPE),
            "reflection",
            "verification conflict",
            {"report_type": "outcome_trace", "success": True, "verification": verification},
        )
        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["sample_counts"]["task_outcomes"] == 1
    assert metrics["metrics"]["task_success_rate"] == 0.0


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


def _append(
    runtime: Runtime,
    scope: ScopeRef,
    kind: str,
    title: str,
    meta: dict,
    *,
    status: str = "active",
    created_at: str = "",
    record_meta: dict | None = None,
) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=title,
        scope=scope,
        source="test.capability_dashboard",
        status=status,
        content=dict(meta),
        meta=dict(meta if record_meta is None else record_meta),
    )
    if created_at:
        record.time.created_at = created_at
        record.time.updated_at = created_at
    runtime.store.append(record)
    return record


def _verified_patch_evidence() -> dict:
    return _executed_patch_evidence(candidate_id="verified-patch", commit="a" * 40, success=True)


def _executed_patch_evidence(*, candidate_id: str, commit: str, success: bool) -> dict:
    release_path = f"/opt/eimemory/releases/{commit}"
    version = "1.9.16"
    prior_commit = "b" * 40
    return {
        "candidate_id": candidate_id,
        "promotion_target": "code_patch",
        "action": "code_patch",
        "gate": {"ok": True},
        "side_effect": {
            "ok": success,
            "production_applied": success,
            "deployment_executed": True,
            "verification": {"ok": True, "skipped": False},
            "deployment": {"ok": success, "skipped": False, "release_path": release_path},
            "post_deploy_health": {
                "ok": success,
                "skipped": False,
                "commit": commit,
                "version": version,
                "release_path": release_path,
            },
            "commit": {"ok": True, "commit_sha": commit},
            "release": {"version": version, "release_path": release_path},
            "rollback_evidence": {
                "service_name": "eimemory-rpc.service",
                "prior_commit_sha": prior_commit,
                "rollback_command": f"git reset --hard {prior_commit}",
            },
        },
    }
