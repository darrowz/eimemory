from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "eibrain", "workspace_id": "roi-ledger"}

def _append_record(
    runtime: Runtime,
    *,
    kind: str,
    title: str,
    meta: dict,
    content: dict | None = None,
    status: str = "active",
) -> RecordEnvelope:
    record = RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=title,
        scope=ScopeRef.from_dict(SCOPE),
        source="test.roi_ledger",
        status=status,
        meta=meta,
        content=content or {},
    )
    return runtime.store.append(record)


def test_roi_ledger_credits_active_rules_and_passing_eval_reports(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.evolution.store_rule(
        title="Active ROI rule",
        summary="Active rules are durable positive evidence",
        task_type="brain.respond",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=SCOPE,
        status="active",
    )
    _append_record(
        runtime,
        kind="replay_result",
        title="Real task replay passed",
        meta={
            "replay_source": "real_task_replay",
            "report_type": "real_task_replay",
            "pass_rate": 1.0,
            "pass_count": 3,
            "fail_count": 0,
        },
    )
    _append_record(
        runtime,
        kind="replay_result",
        title="Production recall passed",
        meta={
            "report_type": "production_recall_eval",
            "pass_rate": 0.95,
            "pass_count": 19,
            "fail_count": 1,
        },
    )

    report = runtime.evolution.build_roi_report(scope=SCOPE)

    assert report["active_rule_count"] == 1
    assert report["replay_pass_count"] == 1
    assert report["roi_breakdown"]["counts"]["active_rules"] == 1
    assert report["roi_breakdown"]["counts"]["eval_pass_reports"] == 1
    assert report["roi_breakdown"]["positive"]["active_rules"] > 0
    assert report["roi_breakdown"]["positive"]["eval_pass_reports"] > 0
    assert report["roi_breakdown"]["positive"]["replay_passes"] > 0
    assert report["roi_signal"] > 0


def test_roi_ledger_does_not_count_dataset_only_replay_report_as_pass(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _append_record(
        runtime,
        kind="replay_result",
        title="Dataset-only replay export",
        meta={
            "schema_version": "real_task_replay.v1",
            "dataset_size": 4,
        },
    )

    report = runtime.evolution.build_roi_report(scope=SCOPE)

    assert report["replay_count"] == 1
    assert report["replay_pass_count"] == 0
    assert report["roi_breakdown"]["counts"]["replay_passes"] == 0
    assert report["roi_breakdown"]["counts"]["eval_pass_reports"] == 0
    assert report["roi_signal"] == 0


def test_roi_ledger_counts_learning_eval_status_in_roi_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _append_record(
        runtime,
        kind="learning_eval",
        title="Learning eval accepted",
        status="passed",
        meta={"verdict": "pass"},
        content={"scores": {"capability": 0.9, "safety": 1.0}},
    )
    _append_record(
        runtime,
        kind="learning_eval",
        title="Learning eval rejected",
        status="rejected",
        meta={"verdict": "fail"},
        content={"scores": {"capability": 0.2, "safety": 0.2}},
    )

    report = runtime.evolution.build_roi_report(scope=SCOPE)

    assert report["roi_breakdown"]["counts"]["eval_pass_reports"] == 1
    assert report["roi_breakdown"]["counts"]["eval_fail_reports"] == 1
    assert report["roi_breakdown"]["positive"]["eval_pass_reports"] > 0
    assert report["roi_breakdown"]["negative"]["eval_fail_reports"] > 0


def test_roi_ledger_treats_operational_incidents_as_partial_penalty(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime.evolution.observe(
        signal_type="incident",
        payload={"incident_type": "replay_timeout", "title": "Replay timeout", "summary": "Cron timeout while replaying."},
        scope=SCOPE,
    )
    runtime.evolution.observe(
        signal_type="incident",
        payload={"incident_type": "storage_failure", "title": "Storage failure", "summary": "Persistent write failure in replay engine."},
        scope=SCOPE,
    )

    report = runtime.evolution.build_roi_report(scope=SCOPE)

    assert report["incident_count"] == 2
    assert report["operational_incident_count"] == 1
    assert report["incident_penalty_count"] == 1.25
    assert report["roi_breakdown"]["counts"]["incident_penalty_count"] == 1.25


def test_roi_ledger_counts_failed_replay_and_eval_reports_as_negative(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _append_record(
        runtime,
        kind="replay_result",
        title="Replay failed",
        meta={"verdict": "fail", "pass_rate": 0.2},
    )
    _append_record(
        runtime,
        kind="reflection",
        title="Memory eval failed",
        meta={
            "report_type": "memory_eval_ci",
            "pass_rate": 0.25,
            "passed_threshold": False,
            "fail_count": 3,
        },
    )

    report = runtime.evolution.build_roi_report(scope=SCOPE)

    assert report["roi_breakdown"]["counts"]["replay_failures"] == 1
    assert report["roi_breakdown"]["counts"]["eval_fail_reports"] == 1
    assert report["roi_breakdown"]["negative"]["replay_failures"] > 0
    assert report["roi_breakdown"]["negative"]["eval_fail_reports"] > 0
    assert report["roi_signal"] < 0


def test_roi_ledger_does_not_double_count_eval_report_with_replay_verdict(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    _append_record(
        runtime,
        kind="replay_result",
        title="Persisted real task replay passed",
        meta={
            "report_type": "real_task_replay",
            "replay_source": "real_task_replay",
            "verdict": "pass",
            "pass_rate": 1.0,
            "sample_count": 2,
        },
    )

    report = runtime.evolution.build_roi_report(scope=SCOPE)

    assert report["replay_pass_count"] == 1
    assert report["roi_breakdown"]["counts"]["replay_passes"] == 1
    assert report["roi_breakdown"]["counts"]["eval_pass_reports"] == 0
    assert report["roi_signal"] == 1.0
