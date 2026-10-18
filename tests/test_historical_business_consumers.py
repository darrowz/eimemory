"""Business acceptance and ongoing online collection stay separate at entry points."""
from copy import deepcopy
import json

import pytest

from deploy.diagnose_recall_release import diagnose
from eimemory.api.runtime import Runtime
from eimemory.cli.main import _nightly_cli_summary
from eimemory.governance.learning.quality_gap_intake import (
    _collection_evidence_wait, ingest_quality_gate_reports,
)
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_nightly_controlled_production_acceptance import _natural_wait_report


SCOPE = {"tenant_id": "default", "agent_id": "hongrui", "workspace_id": "test-business", "user_id": "operator"}


def _reports(*, cli=False):
    nightly = _natural_wait_report()
    diagnostics = nightly_result_diagnostics(nightly, [])
    release = nightly["controlled_production_acceptance"]["release_identity"]
    nightly.update(ok=True, nightly_diagnostics=diagnostics, supervisor_summary={
        "ok": True, "run_id": "run-current", "record_id": "supervisor-current",
        "nightly_diagnostics": diagnostics, **release})
    ending = {"schema": "nightly_command_outcome.v1", "status": "recorded",
        "record_id": "ending-current", "run_id": "run-current", "supervisor_record_id": "supervisor-current",
        "ok": True, "scheduler_ok": True, "scheduler_execution_ok": True, "identity_repair_ok": True,
        "identity_repair": {"ok": True, "exception_raised": False}, "exit_code": 0,
        "first_failed_phase": "", **release}
    if cli:
        nightly = _nightly_cli_summary(nightly)
    nightly["command_execution"] = ending
    return {"nightly": nightly, "release_source": {"ok": True, **release},
        "health": {"ok": True, **release}, "binding": {"ok": True, **release},
        "lineage": {"ok": True, "validated": True, "compatible": True, **release}}


def _diagnose(reports):
    return diagnose(expected_commit="b" * 40, expected_receipt="receipt", expected_session="session", reports=reports)


def _boundary(result, name):
    return next(row for row in result["boundaries"] if row["boundary"] == name)


def test_cli_exposes_primary_business_counts_and_preserves_online_compatibility_fields():
    raw = _reports()["nightly"]
    snapshot = deepcopy(raw)
    result = _nightly_cli_summary(raw)
    business = result["business_recall_quality"]
    assert business["accepted"] is True
    assert business["acceptance_basis"] == "historical_business_memory_controlled"
    assert business["evidence"]["sample_count"] == business["evidence"]["pass_count"] == 12
    assert all(row["sample_count"] == row["pass_count"] == 3 for row in business["per_agent"]["agents"].values())
    assert result["recall_quality_gate"] == result["natural_recall_quality_gate"]
    assert result["recall_quality_gate"]["ok"] is False
    assert result["natural_recall_quality_accepted"] is False
    assert all(row["sample_count"] == 0 for row in result["natural_recall_quality_per_agent"]["agents"].values())
    assert "label_trust_accepted" not in business["evidence"]
    assert raw == snapshot


def test_cli_legacy_missing_diagnostics_does_not_invent_business_qualification():
    result = _nightly_cli_summary({"ok": True})
    assert result["business_recall_quality"]["accepted"] is None
    assert result["business_recall_quality"]["acceptance_basis"] == "not_reported"
    assert result["natural_recall_quality_accepted"] is None


@pytest.mark.parametrize("cli", [False, True])
def test_diagnosis_uses_current_verified_historical_business_basis_without_blocking_on_online_collection(cli):
    result = _diagnose(_reports(cli=cli))
    quality = _boundary(result, "recall_quality")
    assert result["first_blocking_boundary"] == ""
    assert quality["state"] == "reported_pass"
    assert quality["acceptance_basis"] == "historical_business_memory_controlled"
    assert quality["sample_count"] == 12 and quality["natural_online_waiting"] is True
    assert quality["natural_online_accepted"] is False
    assert result["closure_certified"] is False
    assert result["independent_authority_verified"] is False


@pytest.mark.parametrize("damage", ["not_verified", "wrong_purpose", "wrong_record", "wrong_policy",
    "old_commit", "old_receipt", "old_session", "wrong_total", "bool_count", "incomplete_agent",
    "missing_evidence", "failed_steps"])
def test_diagnosis_does_not_promote_unbound_or_incomplete_business_metadata(damage):
    reports = _reports()
    diagnostics = reports["nightly"]["nightly_diagnostics"]
    controlled = diagnostics["controlled_production_acceptance"]
    qualification = controlled["business_qualification"]
    if damage == "not_verified":
        controlled["accepted"] = False
    elif damage == "wrong_purpose":
        controlled["acceptance_purpose"] = "operational"
    elif damage == "wrong_record":
        qualification["record_id"] = "different"
    elif damage == "wrong_policy":
        qualification["policy_digest"] = "c" * 64
    elif damage.startswith("old_"):
        key = {"old_commit": "release_commit", "old_receipt": "deployment_receipt_id",
               "old_session": "release_session_id"}[damage]
        qualification["release_identity"][key] = "c" * 40 if damage == "old_commit" else "old"
    elif damage == "wrong_total":
        qualification["pass_count"] = 11
    elif damage == "bool_count":
        qualification["fail_count"] = False
    elif damage == "incomplete_agent":
        qualification["agents"][0]["complete"] = False
    elif damage == "missing_evidence":
        diagnostics["recall_quality_evidence"] = {}
    else:
        diagnostics["failed_steps"] = ["source_validation"]
    result = _diagnose(reports)
    assert _boundary(result, "recall_quality")["state"] != "reported_pass"
    assert result["first_blocking_boundary"]


@pytest.mark.parametrize("failure", ["raw_execution", "gate_authority", "gate_error", "measured_quality", "external_source"])
def test_diagnosis_historical_metadata_cannot_hide_execution_source_or_measured_quality_failure(failure):
    reports = _reports()
    nightly = reports["nightly"]
    if failure == "raw_execution":
        nightly["storage_maintenance"] = {"ok": False, "error": "storage_failed"}
    elif failure == "gate_authority":
        nightly["recall_quality_gate"]["release_source_validation"] = {"ok": False}
    elif failure == "gate_error":
        nightly["recall_quality_gate"]["error"] = "source_version_changed"
    elif failure == "measured_quality":
        nightly["recall_quality_gate"].update(blocked_reason="false_recall_rate_above_threshold",
                                              blocking_metrics={"false_recall_rate": 1})
    else:
        reports["release_source"] = {"ok": False, "release_commit": "b" * 40, "reason": "release_bytecode_pollution"}
    result = _diagnose(reports)
    assert result["first_failed_boundary"]
    if failure != "external_source":
        assert _boundary(result, "nightly_execution")["state"] == "failed"
        assert _boundary(result, "recall_quality")["reason"] != "historical_business_quality_reported_not_release_certification"


def test_online_collection_wait_creates_no_repair_gap_and_does_not_resolve_an_existing_quality_failure(tmp_path):
    with Runtime.create(root=tmp_path / "runtime") as runtime:
        bad = {"report_type": "recall_quality_report", "sample_count": 3, "quality_gate": {
            "ok": False, "blocked_reason": "recall_quality_gate_failed", "blocking_metrics": {"noise_rate": 1}}}
        first = ingest_quality_gate_reports(runtime, reports={"production_recall": bad}, scope=SCOPE)
        gap_id = first["created_record_ids"][0]
        original = runtime.store.get_by_id(gap_id, scope=SCOPE).to_dict()
        waiting = _natural_wait_report()["production_recall"]
        # Historical qualification cannot be used as a natural passing gate.
        waiting["business_qualification"] = {"accepted": True, "sample_count": 12}
        result = ingest_quality_gate_reports(runtime, reports={"production_recall": waiting}, scope=SCOPE)
        assert result["collection_wait_count"] == 1
        assert result["collection_waits"] == [{"report_name": "production_recall",
                                              "reason": "recall_quality_evidence_incomplete"}]
        assert result["created_count"] == result["deduplicated_count"] == result["resolved_count"] == 0
        assert result["findings"] == []
        assert runtime.store.get_by_id(gap_id, scope=SCOPE).to_dict() == original


@pytest.mark.parametrize("damage", ["error", "errors", "blocking", "not_executed", "execution_unknown",
    "source_authority", "gate_authority", "evidence_execution", "actual_metric", "child_error", "other_report"])
def test_collection_wait_does_not_swallow_errors_or_unverified_execution(damage):
    report = _natural_wait_report()["production_recall"]
    name = "production_recall"
    if damage == "error":
        report["error"] = "source_failed"
    elif damage == "errors":
        report["errors"] = ["source_failed"]
    elif damage == "blocking":
        report["blocking_metrics"] = {"leakage": 1}
    elif damage == "not_executed":
        report["execution_ok"] = False
    elif damage == "execution_unknown":
        report.pop("execution_ok")
    elif damage == "source_authority":
        report["release_source_validation"] = {"ok": False}
    elif damage == "gate_authority":
        report["quality_gate"]["release_source_validation"] = {"ok": False}
    elif damage == "evidence_execution":
        report["quality_gate"]["recall_quality_evidence"]["execution_ok"] = False
    elif damage == "actual_metric":
        report["quality_gate"]["blocking_metrics"] = {"cross_scope_leakage_count": 1}
    elif damage == "child_error":
        report["reports"] = [{"ok": False, "error": "retrieval_failed"}]
    else:
        name = "memory_eval_ci"
    assert _collection_evidence_wait(name, report) is False


def test_business_summary_and_diagnosis_keep_private_source_material_out():
    reports = _reports()
    nightly = reports["nightly"]
    nightly["private_query"] = "PRIVATE-QUERY"
    nightly["controlled_production_acceptance"]["agents"][0]["query"] = "PRIVATE-QUERY"
    summary = _nightly_cli_summary(nightly)
    reports["nightly"] = summary
    assert "PRIVATE-QUERY" not in json.dumps(summary)
    assert "PRIVATE-QUERY" not in json.dumps(_diagnose(reports))
