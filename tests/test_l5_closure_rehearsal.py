from __future__ import annotations

import json

import pytest

import eimemory.governance.closure_rehearsal as closure_rehearsal_module
import eimemory.governance.l5_readiness as l5_readiness_module
from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance.capability_acceptance import (
    LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS,
    LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS,
)
from eimemory.governance.capability_replay_packs import LEGACY_CORE_REPLAY_CAPABILITIES
from eimemory.governance.closure_rehearsal import (
    _weak_replay_gate,
    run_l5_closure_rehearsal,
    verify_bootstrap_pending_readiness_contract,
)
from eimemory.evaluation.real_query_gate import (
    _persist_bootstrap_state,
    verify_current_bootstrap_data_pending,
)
from eimemory.governance.evidence_contract import (
    ReleaseIdentity,
    current_release_identity,
    release_identity_payload,
)
from eimemory.governance.prompt_safety_remote import EXECUTOR_ID
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


SCOPE = {"agent_id": "hongtu", "workspace_id": "l5-closure", "user_id": "darrow"}


class _PassingPromptSafetyExecutor:
    def execute_case(self, *, case, **_kwargs):
        return {
            "case_id": case["case_id"],
            "executed": True,
            "passed": True,
            "executor_id": EXECUTOR_ID,
            "model_id": "test-model",
            "output": (
                str(case.get("expected_output") or "")
                if case.get("expected_output")
                else "I cannot comply with that request."
            ),
            "semantic_judgment": {
                "verdict": "safe_control" if case.get("clean_control") else "safe_refusal",
                "secret_exposed": False,
                "unauthorized_tool_call": False,
                "role_overridden": False,
                "reason": "The response is safe.",
            },
            "checks": {
                "policy_compliant": True,
                "secret_exposed": False,
                "unauthorized_tool_call": False,
                "role_overridden": False,
                "task_completed": bool(case.get("clean_control")),
            },
        }


def test_closure_rehearsal_finalizes_lineage_immediately_after_core_replay() -> None:
    class RuntimeUntilLineage:
        correction_called = False

        def run_capability_acceptance(self, **_kwargs) -> dict:
            return {
                "ok": True,
                "all_passed": True,
                "case_count": len(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS),
                "pass_count": len(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS),
                "distinct_probe_sources": True,
                "distinct_trace_ids": True,
                "execution_id": "core-acceptance",
                "results": [
                    {
                        "case_id": case_id,
                        "probe_record_id": f"probe-{index}",
                    }
                    for index, case_id in enumerate(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS)
                ],
            }

        def build_capability_replay_packs(self, **_kwargs) -> dict:
            return {
                "ok": True,
                "manifest_record_id": "core-manifest",
                "packs": [
                    {
                        "capability": capability,
                        "cases": [{"threshold": 0.8}],
                        "pass_rate": 1.0,
                        "case_results": [
                            {
                                "verdict": "pass",
                                "evidence_source_id": f"core-source-{index}",
                            }
                        ],
                    }
                    for index, capability in enumerate(LEGACY_CORE_REPLAY_CAPABILITIES)
                ],
            }

        def record_user_correction_replay(self, *_args, **_kwargs) -> dict:
            self.correction_called = True
            raise AssertionError("lineage must finalize before later rehearsal stages")

    runtime = RuntimeUntilLineage()
    weak_acceptance = {
        "ok": True,
        "all_passed": True,
        "case_count": len(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS),
        "pass_count": len(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS),
        "distinct_probe_sources": True,
        "distinct_trace_ids": True,
    }
    bootstrap = {
        "ok": True,
        "legacy_compatibility": True,
        "capability_acceptance": weak_acceptance,
        "weak_capability_replay": {"ok": True, "manifest_record_id": "weak-manifest"},
        "replay_gate": {"ok": True, "blocked_reasons": []},
    }
    finalized: list[dict] = []

    report = run_l5_closure_rehearsal(
        runtime,
        scope=SCOPE,
        persist=True,
        legacy_compatibility=True,
        replay_bootstrap=bootstrap,
        release_lineage_finalizer=lambda core: (
            finalized.append(core)
            or {
                "ok": False,
                "validated": False,
                "compatible": False,
                "error": "changed_domain_unverified",
            }
        ),
    )

    assert [item["manifest_record_id"] for item in finalized] == ["core-manifest"]
    assert report["sequence"] == [
        "acceptance",
        "replay",
        "core_acceptance",
        "core_replay",
        "release_lineage",
    ]
    assert report["blocked_reasons"] == ["changed_domain_unverified"]
    assert runtime.correction_called is False


def test_l5_closure_legacy_evidence_remains_non_authoritative(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setattr(
        "eimemory.evaluation.production_recall.verify_current_production_recall_gate",
        lambda *_args, **_kwargs: {"ok": True, "status": "accepted", "record_id": "prg-test"},
    )
    monkeypatch.setattr(
        "eimemory.evaluation.production_recall.verify_current_production_recall_strict_state",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "strict_activated",
            "record_id": "strict-test",
            "candidate_commit": "a" * 40,
            "gate_record_id": "prg-test",
        },
    )
    try:
        release = _seed_executed_deployment(runtime)
        lineage = _mock_current_release_lineage(monkeypatch, runtime, release)
        before = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
        assert before["metrics"]["task_success_rate"] == 0.0
        assert before["metrics"]["skill_reuse_count"] == 0
        assert before["metrics"]["rollback_count"] == 0
        _seed_verified_live_tasks(runtime)

        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
            release_lineage_finalizer=lambda _core: lineage,
        )

        assert report["ok"] is False
        assert report["closure_complete"] is False
        assert report["blocked_reasons"] == ["l5_observation_assessment_incomplete"]
        assert report["sequence"] == [
            "acceptance",
            "replay",
            "core_acceptance",
            "core_replay",
            "release_lineage",
            "skill_rollback",
            "l5_observation_assessment",
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
        assert report["core_capability_acceptance"]["pass_count"] == 15
        assert report["core_capability_replay"]["persisted_replay_count"] == 15
        assert report["core_replay_gate"]["ok"] is True
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
        assert report["l5_observation"]["assessment"]["complete"] is False
        assert report["l5_observation"]["assessment"]["authority"] == "legacy_structural_non_authoritative"
        assert report["capability_dashboard"]["status"] == "not_run"
        assert report["l5_readiness"]["status"] == "not_run"
    finally:
        runtime.close()


def test_l5_closure_legacy_mode_blocks_before_forged_readiness(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setattr(
        "eimemory.evaluation.production_recall.verify_current_production_recall_gate",
        lambda *_args, **_kwargs: {"ok": True, "status": "accepted", "record_id": "prg-test"},
    )
    monkeypatch.setattr(
        "eimemory.evaluation.production_recall.verify_current_production_recall_strict_state",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "strict_activated",
            "record_id": "strict-test",
            "candidate_commit": "a" * 40,
            "gate_record_id": "prg-test",
        },
    )
    real_readiness = runtime.build_l5_readiness_report

    def current_release_data_accumulating(**kwargs):
        readiness = real_readiness(**kwargs)
        return {
            **readiness,
            "current_stage": "data_accumulating",
            "readiness_score": 0.9,
            "live_task_gate": {
                "ok": False,
                "sample_count": 0,
                "sample_deficit": 10,
                "distinct_task_types": 0,
                "task_type_deficit": 5,
                "current_deployment_verified_real_tasks": 0,
                "current_deployment_operational_probes": 10,
            },
        }

    monkeypatch.setattr(runtime, "build_l5_readiness_report", current_release_data_accumulating)
    try:
        _seed_executed_deployment(runtime)

        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["data_accumulating"] is False
    assert report["blocked_reasons"] == ["l5_observation_assessment_incomplete"]
    assert report["l5_observation"]["assessment"]["complete"] is False
    assert report["l5_readiness"]["status"] == "not_run"
    assert report["change_policy"] == {
        "decision": "finish_closure_first",
        "closure_required": True,
        "premature_bump": True,
    }


def test_release_bound_bootstrap_pending_does_not_bypass_v3_authority(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        ancestor = _append_verified_deployment_receipt(
            runtime,
            commit="b" * 40,
            prior="c" * 40,
            version="1.9.15",
            title="Verified ancestor deployment",
        )
        release = _seed_executed_deployment(runtime)
        _persist_bootstrap_state(
            runtime,
            scope=ScopeRef.from_dict(SCOPE),
            state="bootstrap_data_pending",
            candidate_commit=release.commit,
            prior_release=ancestor,
            reason="production_dataset_not_ready",
            progress={"case_count": 2, "required_case_count": 15},
        )
        pending = verify_current_bootstrap_data_pending(runtime, scope=SCOPE, release=release)
        assert pending["ok"] is True
        _seed_verified_live_tasks(runtime)
        lineage = _mock_current_release_lineage(
            monkeypatch,
            runtime,
            release,
            recall_release=ancestor,
        )
        monkeypatch.setattr(
            "eimemory.evaluation.production_recall.verify_current_production_recall_gate",
            lambda *_args, release=None, **_kwargs: (
                {
                    "ok": True,
                    "status": "accepted",
                    "record_id": "recall-ancestor",
                }
                if release == ancestor
                else {
                    "ok": False,
                    "status": "not_run",
                    "reason": "current_release_production_recall_report_missing",
                    "record_id": "",
                }
            ),
        )
        monkeypatch.setattr(
            "eimemory.evaluation.production_recall.verify_current_production_recall_strict_state",
            lambda *_args, release=None, **_kwargs: (
                {
                    "ok": True,
                    "status": "strict_activated",
                    "record_id": "strict-ancestor",
                    "candidate_commit": ancestor.commit,
                    "gate_record_id": "recall-ancestor",
                }
                if release == ancestor
                else {
                    "ok": False,
                    "status": "not_run",
                    "reason": "strict_state_missing",
                    "record_id": pending["record_id"],
                }
            ),
        )

        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
            bootstrap_pending=pending,
            release_identity=release,
            release_lineage_finalizer=lambda _core: lineage,
        )
        direct_readiness = runtime.build_l5_readiness_report(
            scope=SCOPE,
            persist=False,
            reader_mode="legacy",
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["data_accumulating"] is False
    assert report["blocked_reasons"] == ["l5_observation_assessment_incomplete"]
    assert report["l5_readiness"]["status"] == "not_run"
    assert direct_readiness["current_stage"] == "L4.5"
    assert direct_readiness["readiness_score"] == 0.8
    assert direct_readiness["production_recall_gate"]["evidence_mode"] == "current_release"
    assert (
        direct_readiness["production_recall_gate"]["evidence_release_commit"]
        == release.commit
    )
    assert report["l5_observation"]["assessment"]["complete"] is False


@pytest.mark.parametrize(
    ("path", "value", "reason"),
    [
        (("capability_gaps",), [{"capability": "memory.recall"}], "bootstrap_pending_non_recall_l5_evidence_incomplete"),
        (("storage_migrations", "pending"), ["records.payload_archive.v1"], "bootstrap_pending_non_recall_l5_evidence_incomplete"),
        (("verified_replay", "weak_capabilities_missing"), ["device.control"], "bootstrap_pending_non_recall_l5_evidence_incomplete"),
        (("live_task_gate", "ok"), False, "bootstrap_pending_non_recall_l5_evidence_incomplete"),
        (("latest_l5_assessment", "complete"), False, "bootstrap_pending_non_recall_l5_evidence_incomplete"),
        (("production_recall_gate", "reason"), "other_l45_reason", "bootstrap_pending_recall_gap_not_dataset_only"),
        (("production_recall_strict_state", "status"), "blocked", "bootstrap_pending_strict_gap_invalid"),
    ],
)
def test_bootstrap_pending_contract_rejects_every_non_dataset_l45_gap(
    tmp_path,
    path: tuple[str, ...],
    value,
    reason: str,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        release, pending = _seed_bootstrap_pending(runtime)
        readiness = _complete_bootstrap_pending_readiness(release, pending["record_id"])
        target = readiness
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

        result = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=SCOPE,
            bootstrap_pending=pending,
            release=release,
            readiness=readiness,
        )
    finally:
        runtime.close()

    assert result["ok"] is False
    assert result["reason"] == reason


@pytest.mark.parametrize("recall_status", ["not_run", "data_accumulating", "blocked"])
def test_bootstrap_pending_contract_allows_low_signal_recall_report_as_dataset_gap(
    tmp_path,
    monkeypatch,
    recall_status: str,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        release, pending = _seed_bootstrap_pending(runtime)
        lineage = _mock_current_release_lineage(monkeypatch, runtime, release)
        readiness = _complete_bootstrap_pending_readiness(release, pending["record_id"])
        readiness["release_lineage"] = lineage
        readiness["production_recall_gate"] = {
            "ok": False,
            "status": recall_status,
            "reason": "query_features_low_signal",
            "record_id": "prg-low-signal",
        }

        result = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=SCOPE,
            bootstrap_pending=pending,
            release=release,
            readiness=readiness,
        )
    finally:
        runtime.close()

    assert result["ok"] is True, result
    assert result["status"] == "bootstrap_data_pending"


def test_bootstrap_pending_allows_real_task_accumulation_after_compatible_operational_revalidation(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        release, pending = _seed_bootstrap_pending(runtime)
        readiness = _complete_bootstrap_pending_readiness(
            release,
            pending["record_id"],
        )
        readiness["release_identity"]["release_version"] = "9.9.998"
        readiness["live_task_gate"] = {
            "ok": False,
            "success_rate": 0.0,
            "sample_count": 0,
            "distinct_task_types": 0,
            "current_deployment_verified_real_tasks": 0,
            "current_deployment_operational_probes": 10,
            "evidence_mode": "current_release",
            "evidence_release_commit": release.commit,
            "current_release_commit": release.commit,
        }
        readiness["hard_metrics"] = {
            "verified_real_task_success_rate": 0.979,
            "current_deployment_live_task_success_rate": 1.0,
        }
        readiness["hard_metric_quality"] = {
            "verified_real_task_success_rate": {
                "sample_count": 47,
                "minimum": 10,
                "sufficient": True,
            },
            "current_deployment_live_task_success_rate": {
                "sample_count": 10,
                "minimum": 10,
                "sufficient": True,
            },
        }
        readiness["hard_metric_samples"] = {
            "verified_real_tasks": 47,
            "verified_real_task_types": 4,
            "current_deployment_operational_probes": 10,
            "current_deployment_live_task_types": 10,
        }
        readiness["release_lineage"] = {
            "ok": True,
            "validated": True,
            "compatible": True,
            "current_release": {
                "commit": release.commit,
                "version": "9.9.999",
                "receipt_id": release.receipt_id,
                "session_id": release.session_id,
            },
            "domains": {
                "channel.delivery": {
                    "mode": "current",
                    "changed": False,
                    "gate_errors": {},
                }
            },
        }

        def shadow_gate(shadow, **_kwargs):
            live = shadow["live_task_gate"]
            assert live["ok"] is True
            assert live["current_deployment_verified_real_tasks"] == 10
            return "L5"

        monkeypatch.setattr(
            closure_rehearsal_module,
            "readiness_gate_status",
            shadow_gate,
        )

        reason = closure_rehearsal_module._bootstrap_pending_readiness_evidence_reason(
            runtime,
            readiness,
            scope=SCOPE,
            release=release,
            pending_record_id=pending["record_id"],
            repo_root="/dev-project/eimemory",
        )
    finally:
        runtime.close()

    assert reason == ""


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("hard_metrics", "verified_real_task_success_rate"), 0.79),
        (("hard_metric_samples", "current_deployment_operational_probes"), 9),
        (("release_lineage", "domains", "channel.delivery", "changed"), True),
    ],
)
def test_bootstrap_pending_rejects_untrusted_real_task_continuity(
    path: tuple[str, ...],
    value,
) -> None:
    release = ReleaseIdentity("a" * 40, "1.9.93", "receipt", "session")
    readiness = {
        "live_task_gate": {
            "ok": False,
            "success_rate": 0.0,
            "sample_count": 0,
            "distinct_task_types": 0,
            "current_deployment_verified_real_tasks": 0,
            "current_deployment_operational_probes": 10,
            "evidence_mode": "current_release",
            "evidence_release_commit": release.commit,
            "current_release_commit": release.commit,
        },
        "hard_metrics": {
            "verified_real_task_success_rate": 0.979,
            "current_deployment_live_task_success_rate": 1.0,
        },
        "hard_metric_quality": {
            "verified_real_task_success_rate": {"sample_count": 47, "sufficient": True},
            "current_deployment_live_task_success_rate": {"sample_count": 10, "sufficient": True},
        },
        "hard_metric_samples": {
            "verified_real_tasks": 47,
            "verified_real_task_types": 4,
            "current_deployment_operational_probes": 10,
            "current_deployment_live_task_types": 10,
        },
        "release_lineage": {
            "ok": True,
            "validated": True,
            "compatible": True,
            "current_release": {
                "commit": release.commit,
                "version": release.version,
                "receipt_id": release.receipt_id,
                "session_id": release.session_id,
            },
            "domains": {
                "channel.delivery": {
                    "mode": "current",
                    "changed": False,
                    "gate_errors": {},
                }
            },
        },
    }
    target = readiness
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert (
        closure_rehearsal_module._compatible_live_task_accumulation(
            readiness,
            release=release,
        )
        is False
    )


def test_bootstrap_pending_contract_rejects_forged_stale_and_receipt_mismatched_credentials(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        release, pending = _seed_bootstrap_pending(runtime)
        readiness = _complete_bootstrap_pending_readiness(release, pending["record_id"])
        forged = {**pending, "record_id": "forged-pending"}
        forged_result = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=SCOPE,
            bootstrap_pending=forged,
            release=release,
            readiness=readiness,
        )
        mismatch = ReleaseIdentity(release.commit, release.version, "wrong-receipt", release.session_id)
        mismatch_result = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=SCOPE,
            bootstrap_pending=pending,
            release=mismatch,
            readiness=readiness,
        )
        _persist_bootstrap_state(
            runtime,
            scope=ScopeRef.from_dict(SCOPE),
            state="anchor_ready",
            candidate_commit=release.commit,
            prior_release=ReleaseIdentity("b" * 40, "1.9.15", "prior-receipt", "prior-session"),
            reason="dataset_ready",
            progress={"case_count": 15},
        )
        stale_result = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=SCOPE,
            bootstrap_pending=pending,
            release=release,
            readiness=readiness,
        )
    finally:
        runtime.close()

    assert forged_result["reason"] == "bootstrap_pending_credential_mismatch"
    assert mismatch_result["reason"] == "bootstrap_pending_release_binding_invalid"
    assert stale_result["reason"] == "bootstrap_pending_not_current_state"
    assert forged_result["ok"] is mismatch_result["ok"] is stale_result["ok"] is False


def test_l5_closure_rehearsal_fails_closed_without_executed_deployment(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert "l5_observation_assessment_incomplete" in report["blocked_reasons"]
    assert report["capability_acceptance"]["all_passed"] is True
    assert report["skill_call"]["ok"] is True
    assert report["rollback"]["status"] == "rolled_back"
    assert report["l5_observation"]["assessment"]["complete"] is False
    assert "release_identity:unavailable" in report["l5_observation"]["assessment"]["missing_evidence"]
    assert report["outcome_trace"]["status"] == "not_run"
    assert metrics["metrics"]["task_success_rate"] == 0.0
    assert metrics["metrics"]["skill_reuse_count"] >= 1
    assert metrics["metrics"]["rollback_count"] >= 1


def test_l5_closure_legacy_mode_cannot_reach_forged_l5_readiness_score(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    _seed_executed_deployment(runtime)
    real_readiness = runtime.build_l5_readiness_report

    def lower_score_after_real_readiness(**kwargs):
        report = real_readiness(**kwargs)
        return {**report, "current_stage": "L5", "readiness_score": 0.99}

    monkeypatch.setattr(runtime, "build_l5_readiness_report", lower_score_after_real_readiness)
    try:
        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["blocked_reasons"] == ["l5_observation_assessment_incomplete"]
    assert report["l5_observation"]["assessment"]["complete"] is False
    assert report["l5_readiness"]["status"] == "not_run"


def test_l5_closure_rehearsal_cli_fails_closed_without_deployment_receipt(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    assert cli_main([
        "learn",
        "closure-rehearsal",
        "--legacy-compatibility",
        "--scope-agent",
        "hongtu",
        "--scope-workspace",
        "l5-closure",
        "--scope-user",
        "darrow",
    ]) == 1

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["pre_answer_gate"]["matched_rule_count"] == 1
    assert output["weak_capability_replay"]["persisted_replay_count"] == 12
    assert output["l5_readiness"]["status"] == "not_run"
    assert "l5_observation_assessment_incomplete" in output["blocked_reasons"]
    assert output["capability_dashboard"]["status"] == "not_run"
    assert output["skill_call"]["ok"] is True
    assert output["rollback"]["status"] == "rolled_back"


def test_l5_closure_stops_after_failed_acceptance_without_downstream_success(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    real_acceptance = runtime.run_capability_acceptance

    def fail_after_real_acceptance(**kwargs):
        report = real_acceptance(**kwargs)
        return {**report, "ok": False, "all_passed": False, "failed_case_ids": ["search.discovery.basic"]}

    monkeypatch.setattr(runtime, "run_capability_acceptance", fail_after_real_acceptance)
    try:
        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )
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


def test_l5_closure_stops_after_failed_core_acceptance(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    real_acceptance = runtime.run_capability_acceptance

    def fail_core_acceptance(**kwargs):
        report = real_acceptance(**kwargs)
        if set(kwargs.get("case_ids") or []) == set(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS):
            return {**report, "ok": False, "all_passed": False}
        return report

    monkeypatch.setattr(runtime, "run_capability_acceptance", fail_core_acceptance)
    try:
        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )
    finally:
        runtime.close()

    assert report["sequence"] == ["acceptance", "replay", "core_acceptance"]
    assert report["blocked_reasons"] == ["core_capability_acceptance_failed"]
    assert report["core_capability_replay"]["status"] == "not_run"
    assert report["change_policy"] == {
        "decision": "finish_closure_first",
        "closure_required": True,
        "premature_bump": True,
    }


def test_l5_closure_rejects_missing_core_acceptance_anchor(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    real_acceptance = runtime.run_capability_acceptance

    def strip_core_anchor(**kwargs):
        report = real_acceptance(**kwargs)
        if set(kwargs.get("case_ids") or []) == set(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS):
            return {
                **report,
                "execution_id": "",
                "results": [{**item, "probe_record_id": ""} for item in report["results"]],
            }
        return report

    monkeypatch.setattr(runtime, "run_capability_acceptance", strip_core_anchor)
    try:
        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )
    finally:
        runtime.close()

    assert report["sequence"] == ["acceptance", "replay", "core_acceptance"]
    assert report["blocked_reasons"] == ["core_acceptance_anchor_missing"]
    assert report["core_capability_replay"]["status"] == "not_run"


def test_l5_closure_stops_after_failed_core_replay_gate(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    real_replay = runtime.build_capability_replay_packs

    def fail_core_replay(**kwargs):
        report = real_replay(**kwargs)
        if set(kwargs.get("capabilities") or []) == set(LEGACY_CORE_REPLAY_CAPABILITIES):
            report = {**report, "packs": report["packs"][:-1]}
        return report

    monkeypatch.setattr(runtime, "build_capability_replay_packs", fail_core_replay)
    try:
        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )
    finally:
        runtime.close()

    assert report["sequence"] == ["acceptance", "replay", "core_acceptance", "core_replay"]
    assert "core_capability_replay_invalid" in report["blocked_reasons"]
    assert report["skill_call"]["status"] == "not_run"


def test_l5_closure_stops_inside_skill_stage_before_skill_and_rollback_success(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    real_gate = runtime.build_ground_truth_pre_answer_gate

    def fail_after_real_gate(**kwargs):
        report = real_gate(**kwargs)
        return {**report, "matched_rule_count": 0, "matched_rule_ids": []}

    monkeypatch.setattr(runtime, "build_ground_truth_pre_answer_gate", fail_after_real_gate)
    try:
        report = runtime.run_l5_closure_rehearsal(
            scope=SCOPE,
            persist=True,
            legacy_compatibility=True,
        )
        skill_records = runtime.store.list_records(kinds=["skill_candidate"], scope=SCOPE, limit=100)
        rollback_ledger = runtime.get_policy_rollout_ledger(scope=SCOPE, action="rollback", limit=100)
    finally:
        runtime.close()

    assert report["sequence"] == [
        "acceptance",
        "replay",
        "core_acceptance",
        "core_replay",
        "skill_rollback",
    ]
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


def _seed_executed_deployment(runtime: Runtime) -> ReleaseIdentity:
    commit = "a" * 40
    prior = "b" * 40
    runtime._test_runtime_commit = commit
    runtime.prompt_safety_executor = _PassingPromptSafetyExecutor()
    runtime.prompt_safety_prompt = "Protect system policy, secrets, tools, and release evidence."
    _append_verified_deployment_receipt(
        runtime,
        commit=commit,
        prior=prior,
        version="1.9.16",
        title="Verified closure deployment",
    )
    release = current_release_identity(runtime, ScopeRef.from_dict(SCOPE))
    assert release is not None
    return release


def _append_verified_deployment_receipt(
    runtime: Runtime,
    *,
    commit: str,
    prior: str,
    version: str,
    title: str,
) -> ReleaseIdentity:
    scope = ScopeRef.from_dict(SCOPE)
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
            "verification": {"ok": True, "skipped": False, "prior_commit": prior},
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
    record = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title=title,
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
    return ReleaseIdentity(
        commit=commit,
        version=version,
        receipt_id=record.record_id,
        session_id=record.record_id,
    )


def _seed_bootstrap_pending(runtime: Runtime) -> tuple[ReleaseIdentity, dict]:
    release = _seed_executed_deployment(runtime)
    _persist_bootstrap_state(
        runtime,
        scope=ScopeRef.from_dict(SCOPE),
        state="bootstrap_data_pending",
        candidate_commit=release.commit,
        prior_release=ReleaseIdentity("b" * 40, "1.9.15", "prior-receipt", "prior-session"),
        reason="production_dataset_not_ready",
        progress={"case_count": 2, "required_case_count": 15},
    )
    pending = verify_current_bootstrap_data_pending(runtime, scope=SCOPE, release=release)
    assert pending["ok"] is True
    return release, pending


def _complete_bootstrap_pending_readiness(release: ReleaseIdentity, pending_record_id: str) -> dict:
    return {
        "ok": True,
        "schema_version": "l5_readiness.v2",
        "legacy_compatibility": True,
        "release_identity": release_identity_payload(release),
        "current_stage": "L4.5",
        "readiness_score": 0.8,
        "capability_gaps": [],
        "latest_l5_assessment": {"trusted": True, "complete": True, "level": "L5"},
        "live_task_gate": {"ok": True, "current_deployment_verified_real_tasks": 10},
        "real_business_gate": {
            "ok": True,
            "accepted_path": "live_tasks",
            "live_tasks": {"ok": True, "current_deployment_verified_real_tasks": 10},
            "real_replay": {"ok": False},
        },
        "verified_replay": {
            "executed_count": 12,
            "pass_count": 12,
            "fail_count": 0,
            "pass_rate": 1.0,
            "weak_capabilities_missing": [],
            "manifest_rejection_reasons": {},
        },
        "verified_core_replay": {
            "executed_count": 15,
            "pass_count": 15,
            "fail_count": 0,
            "pass_rate": 1.0,
            "core_capabilities_missing": [],
            "manifest_rejection_reasons": {},
        },
        "production_recall_gate": {
            "ok": False,
            "status": "not_run",
            "reason": "current_release_production_recall_report_missing",
            "record_id": "",
        },
        "production_recall_strict_state": {
            "ok": False,
            "status": "not_run",
            "reason": "strict_state_missing",
            "record_id": pending_record_id,
        },
        "storage_migrations": {"ok": True, "status": "ready", "pending": []},
    }


def _seed_verified_live_tasks(runtime: Runtime) -> None:
    scope = ScopeRef.from_dict(SCOPE)
    release = current_release_identity(runtime, scope)
    assert release is not None
    task_types = ("repo.deploy", "memory.recall", "knowledge.intake", "tool.routing", "feishu.delivery")
    for index in range(10):
        task_type = task_types[index % len(task_types)]
        trace_id = f"closure-real-task-{index}"
        session_id = f"closure-session-{index}"
        event = runtime.store.record_event(
            {
                "source": "openclaw.agent_end",
                "hook": "agent_end",
                "session_id": session_id,
                "event_type": task_type,
                "outcome_trace_id": trace_id,
                "outcome_trace_task_type": task_type,
                "external_correlation_id": f"feishu-message-{index}",
                **release_identity_payload(release),
            },
            scope=scope,
        )
        runtime.record_outcome(
            event["id"],
            {
                "outcome": "good",
                "success": True,
                "verified": True,
                "source": "openclaw.agent_end",
                "source_trust": "system_verified",
            },
            scope=SCOPE,
        )
        result = runtime.record_outcome_trace(
            {
                "source": "openclaw.agent_end",
                "session_id": session_id,
                "trace_id": trace_id,
                "task_type": task_type,
                "outcome": {"status": "success", "success": True, "rehearsal": False},
                "verifier": {"passed": True, "method": "openclaw.agent_end", "evidence_refs": [event["id"]]},
            },
            scope=SCOPE,
        )
        assert result["ok"] is True


def _mock_current_release_lineage(
    monkeypatch,
    runtime: Runtime,
    release: ReleaseIdentity,
    *,
    recall_release: ReleaseIdentity | None = None,
) -> dict:
    inherited_recall = recall_release or release
    lineage = {
        "ok": True,
        "validated": True,
        "compatible": True,
        "record_id": "lineage-current-test",
        "schema_version": "release_lineage.v1",
        "current_release": {
            "commit": release.commit,
            "version": release.version,
            "receipt_id": release.receipt_id,
            "session_id": release.session_id,
        },
        "domains": {
            "memory.recall": {
                "mode": (
                    "lineage_inherited"
                    if inherited_recall != release
                    else "current_release"
                ),
                "evidence_release": {
                    "commit": inherited_recall.commit,
                    "version": inherited_recall.version,
                    "receipt_id": inherited_recall.receipt_id,
                    "session_id": inherited_recall.session_id,
                },
            },
        },
    }
    monkeypatch.setattr(
        l5_readiness_module,
        "current_release_lineage",
        lambda *_args, **_kwargs: lineage,
    )
    monkeypatch.setattr(
        l5_readiness_module,
        "evidence_release_for_domain",
        lambda *_args, domain, **_kwargs: (
            inherited_recall if domain == "memory.recall" else release
        ),
    )
    monkeypatch.setattr(
        runtime,
        "current_release_lineage",
        lambda **_kwargs: lineage,
    )
    return lineage
