from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.closure_rehearsal import _weak_replay_gate
from eimemory.governance.live_task_acceptance import LIVE_ACCEPTANCE_CASE_IDS, live_acceptance_task_type
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


SCOPE = {"agent_id": "hongtu", "workspace_id": "l5-closure", "user_id": "darrow"}


def test_l5_closure_rehearsal_opens_success_skill_and_rollback_metrics(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_executed_deployment(runtime)
        before = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
        assert before["metrics"]["task_success_rate"] == 0.0
        assert before["metrics"]["skill_reuse_count"] == 0
        assert before["metrics"]["rollback_count"] == 0
        _seed_verified_live_tasks(runtime)

        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)

        assert report["ok"] is True
        assert report["closure_complete"] is True
        assert report["blocked_reasons"] == []
        assert report["sequence"] == [
            "acceptance",
            "replay",
            "skill_rollback",
            "l5_observation_assessment",
            "dashboard",
            "readiness",
        ]
        assert report["capability_acceptance"]["all_passed"] is True
        assert report["correction_replay"]["ground_truth_rule_id"]
        assert report["pre_answer_gate"]["matched_rule_count"] == 1
        assert report["weak_capability_replay"]["capabilities"] == [
            "search.discovery",
            "research.synthesis",
            "operations.uumit",
            "device.control",
        ]
        assert report["weak_capability_replay"]["persisted_replay_count"] == 12
        acceptance_probe_ids = set(report["capability_acceptance"]["probe_record_ids"])
        replay_probe_ids = {
            result["probe_source_id"]
            for pack in report["weak_capability_replay"]["packs"]
            for result in pack["case_results"]
        }
        assert replay_probe_ids == acceptance_probe_ids
        assert report["skill_call"]["ok"] is True
        assert report["skill_call"]["record_id"]
        assert report["rollback"]["status"] in {"rolled_back", "quarantined"}
        assert report["l5_observation"]["apply"] is False
        assert report["l5_observation"]["assessment"]["complete"] is True

        metrics = report["capability_dashboard"]["metrics"]
        assert metrics["task_success_rate"] == 1.0
        assert metrics["verified_live_task_success_rate"] == 1.0
        assert metrics["skill_reuse_count"] >= 1
        assert metrics["rollback_count"] >= 1
        assert report["outcome_trace"]["outcome"]["rehearsal"] is True
        weak_gaps = {
            gap["capability"]
            for gap in report["l5_readiness"]["capability_gaps"]
            if gap["capability"] in {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
        }
        assert weak_gaps == set()
        assert report["l5_readiness"]["current_stage"] == "L5"
    finally:
        runtime.close()


def test_l5_closure_rehearsal_fails_closed_without_executed_deployment(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert "l5_readiness_not_l5" in report["blocked_reasons"]
    assert report["capability_acceptance"]["all_passed"] is True
    assert report["skill_call"]["ok"] is True
    assert report["rollback"]["status"] == "rolled_back"
    assert report["l5_observation"]["assessment"]["complete"] is True
    assert report["outcome_trace"]["status"] == "not_run"
    assert metrics["metrics"]["task_success_rate"] == 0.0
    assert metrics["metrics"]["skill_reuse_count"] >= 1
    assert metrics["metrics"]["rollback_count"] >= 1


def test_l5_closure_rejects_l5_stage_below_full_readiness_score(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_executed_deployment(runtime)
    real_readiness = runtime.build_l5_readiness_report

    def lower_score_after_real_readiness(**kwargs):
        report = real_readiness(**kwargs)
        return {**report, "current_stage": "L5", "readiness_score": 0.99}

    monkeypatch.setattr(runtime, "build_l5_readiness_report", lower_score_after_real_readiness)
    try:
        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["blocked_reasons"] == ["l5_readiness_not_l5"]
    assert report["l5_readiness"]["current_stage"] == "L5"
    assert report["l5_readiness"]["readiness_score"] == 0.99
    assert report["outcome_trace"]["status"] == "not_run"


def test_l5_closure_rehearsal_cli_fails_closed_without_deployment_receipt(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main(["learn", "closure-rehearsal", "--scope-agent", "hongtu", "--scope-workspace", "l5-closure", "--scope-user", "darrow"]) == 1

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["pre_answer_gate"]["matched_rule_count"] == 1
    assert output["weak_capability_replay"]["persisted_replay_count"] == 12
    assert output["l5_readiness"]["evidence_counts"]["rollback_or_quarantine"] >= 1
    weak_gaps = {
        gap["capability"]
        for gap in output["l5_readiness"]["capability_gaps"]
        if gap["capability"] in {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
    }
    assert weak_gaps == set()
    assert output["capability_dashboard"]["metrics"]["skill_reuse_count"] >= 1
    assert output["capability_dashboard"]["metrics"]["rollback_count"] >= 1
    assert "l5_readiness_not_l5" in output["blocked_reasons"]


def test_l5_closure_stops_after_failed_acceptance_without_downstream_success(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    real_acceptance = runtime.run_capability_acceptance

    def fail_after_real_acceptance(**kwargs):
        report = real_acceptance(**kwargs)
        return {**report, "ok": False, "all_passed": False, "failed_case_ids": ["search.discovery.basic"]}

    monkeypatch.setattr(runtime, "run_capability_acceptance", fail_after_real_acceptance)
    try:
        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)
        replay_records = [
            record
            for record in runtime.store.list_records(kinds=["replay_result"], scope=SCOPE, limit=100)
            if str(record.meta.get("report_type") or "") == "capability_replay_pack"
        ]
        skill_records = runtime.store.list_records(kinds=["skill_candidate"], scope=SCOPE, limit=100)
        assessment_records = runtime.store.list_records(kinds=["l5_assessment"], scope=SCOPE, limit=100)
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["sequence"] == ["acceptance"]
    assert report["blocked_reasons"] == ["capability_acceptance_failed"]
    assert report["weak_capability_replay"]["status"] == "not_run"
    assert report["skill_call"]["status"] == "not_run"
    assert report["l5_observation"]["status"] == "not_run"
    assert report["capability_dashboard"]["status"] == "not_run"
    assert report["l5_readiness"]["status"] == "not_run"
    assert replay_records == []
    assert skill_records == []
    assert assessment_records == []


def test_l5_closure_stops_inside_skill_stage_before_skill_and_rollback_success(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    real_gate = runtime.build_ground_truth_pre_answer_gate

    def fail_after_real_gate(**kwargs):
        report = real_gate(**kwargs)
        return {**report, "matched_rule_count": 0, "matched_rule_ids": []}

    monkeypatch.setattr(runtime, "build_ground_truth_pre_answer_gate", fail_after_real_gate)
    try:
        report = runtime.run_l5_closure_rehearsal(scope=SCOPE, persist=True)
        skill_records = runtime.store.list_records(kinds=["skill_candidate"], scope=SCOPE, limit=100)
        rollback_ledger = runtime.get_policy_rollout_ledger(scope=SCOPE, action="rollback", limit=100)
    finally:
        runtime.close()

    assert report["sequence"] == ["acceptance", "replay", "skill_rollback"]
    assert report["blocked_reasons"] == ["ground_truth_rule_not_matched"]
    assert report["skill_call"]["status"] == "not_run"
    assert report["rollback"]["status"] == "not_run"
    assert report["l5_observation"]["status"] == "not_run"
    assert skill_records == []
    assert rollback_ledger == []


def test_weak_replay_gate_requires_each_named_capability_once() -> None:
    pack = {
        "capability": "search.discovery",
        "cases": [{"case_id": "one", "threshold": 1.0}],
        "case_results": [{"case_id": "one", "verdict": "pass", "hit": True, "observed": "verified"}],
        "pass_rate": 1.0,
    }

    gate = _weak_replay_gate({"ok": True, "packs": [dict(pack) for _ in range(4)]})

    assert gate["ok"] is False
    assert "weak_capability_replay_invalid" in gate["blocked_reasons"]


def _seed_executed_deployment(runtime: Runtime) -> None:
    scope = ScopeRef.from_dict(SCOPE)
    commit = "a" * 40
    prior = "b" * 40
    version = "1.9.16"
    release_path = f"/opt/eimemory/releases/{commit}"
    payload = {
        "report_type": "deployment_receipt",
        "candidate_id": f"deployment:{commit}",
        "promotion_target": "code_patch",
        "action": "code_patch",
        "gate": {"ok": True, "receipt_verified": True},
        "side_effect": {
            "ok": True,
            "production_applied": True,
            "deployment_executed": True,
            "verification": {"ok": True, "skipped": False},
            "deployment": {"ok": True, "skipped": False, "release_path": release_path},
            "post_deploy_health": {
                "ok": True,
                "skipped": False,
                "commit": commit,
                "version": version,
                "release_path": release_path,
                "import_root": f"{release_path}/eimemory",
                "package_tree_digest": runtime_package_tree_digest(),
                "checks": {"ready": True},
            },
            "commit": {"ok": True, "commit_sha": commit},
            "release": {"version": version, "release_path": release_path},
            "rollback_evidence": {
                "prior_commit_sha": prior,
                "rollback_command": f"git reset --hard {prior}",
            },
        },
    }
    runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Verified closure deployment",
            summary="Executed deployment receipt",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
            content=payload,
            meta={
                "report_type": "deployment_receipt",
                "candidate_id": payload["candidate_id"],
                "promotion_target": "code_patch",
                "action": "code_patch",
                "gate_ok": True,
                "commit_sha": commit,
                "version": version,
                "release_path": release_path,
            },
        )
    )


def _seed_verified_live_tasks(runtime: Runtime) -> None:
    scope = ScopeRef.from_dict(SCOPE)
    commit = "a" * 40
    runtime._test_runtime_commit = commit
    receipts = [
        record
        for record in runtime.store.list_records(kinds=["promotion_request"], scope=scope, limit=20)
        if record.source == "eimemory.deployment_receipt" and str(record.meta.get("commit_sha") or "") == commit
    ]
    assert receipts
    version = str(receipts[0].meta.get("version") or "")
    for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
        task_type = live_acceptance_task_type(case_id)
        observation_digest = f"{index:064x}"
        trace_id = f"live-acceptance:{commit}:{case_id}:{observation_digest[:12]}"
        payload = {
            "report_type": "live_task_acceptance_case",
            "schema_version": "live_task_acceptance.v1",
            "case_id": case_id,
            "task_type": task_type,
            "trace_id": trace_id,
            "passed": True,
            "deployment_commit": commit,
            "deployment_version": version,
            "release_path": f"/opt/eimemory/releases/{commit}",
            "promotion_request_id": receipts[0].record_id,
            "observation_digest": observation_digest,
        }
        evidence = runtime.store.append(
            RecordEnvelope.create(
                kind="learning_eval",
                title=f"Closure live task {index}",
                summary="passed",
                scope=scope,
                source="eimemory.live_task_acceptance",
                content=payload,
                meta=payload,
            )
        )
        result = runtime.record_outcome_trace(
            {
                "source": "eimemory.live_task_acceptance",
                "trace_id": trace_id,
                "task_type": task_type,
                "outcome": {"status": "success", "success": True, "rehearsal": False},
                "verifier": {"passed": True, "method": "eimemory.live_task_acceptance", "evidence_refs": [evidence.record_id]},
                "deployment_commit": commit,
                "deployment_version": version,
                "release_path": f"/opt/eimemory/releases/{commit}",
                "acceptance_case_id": case_id,
            },
            scope=SCOPE,
        )
        assert result["ok"] is True
