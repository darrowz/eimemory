from copy import deepcopy
import json
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from eimemory.scheduler.controlled_acceptance_diagnostics import controlled_acceptance_diagnostics
from eimemory.scheduler.result_contract import _aggregate_nightly_ok, nightly_result_diagnostics


def accepted_receipt():
    value = {
        "ok": True, "accepted": True, "status": "accepted",
        "schema": "controlled_production_acceptance.v1",
        "mode": "controlled_existing_memory",
        "evidence_class": "operator_controlled_existing_memory",
        "certifies_natural_queries": False, "natural_samples_created": 0,
        "policy_digest": "a" * 64,
        "release_identity": {"release_commit": "b" * 40,
            "release_version": "1.0.0", "deployment_receipt_id": "receipt",
            "release_session_id": "session"},
        "record_id": "controlled-receipt",
        "agents": [{"agent_id": "one", "sample_count": 3, "pass_count": 3,
                    "scope": {"user_id": "PRIVATE-USER"}, "query": "PRIVATE-QUERY"}],
    }
    value["verification"] = {key: deepcopy(item) for key, item in value.items()
                              if key not in {"agents", "verification"}}
    value["runtime_authority_verified"] = True
    return value


def test_operator_acceptance_does_not_certify_missing_natural_quality():
    report = {"controlled_production_acceptance": accepted_receipt(),
              "recall_quality_gate": {"ok": False,
                  "blocked_reason": "recall_quality_evidence_incomplete", "blocking_metrics": {},
                  "recall_quality_evidence": {"execution_ok": True}}}
    result = nightly_result_diagnostics(report, [])
    assert result["production_operational_accepted"] is True
    assert result["recall_quality_accepted"] is False
    assert result["production_acceptance_basis"] == "controlled_existing_memory"
    assert result["evidence_waits"] == ["recall_quality_gate"]
    assert "PRIVATE-" not in json.dumps(result)


@pytest.mark.parametrize("changes", [
    {"runtime_authority_verified": False}, {"certifies_natural_queries": True},
    {"natural_samples_created": 1}, {"natural_samples_created": False},
    {"policy_digest": "invalid"}, {"record_id": ""}, {"agents": []},
    {"ok": 1}, {"accepted": 1}, {"mode": "natural"},
])
def test_unverified_or_contradictory_receipt_cannot_be_displayed_as_accepted(changes):
    value = accepted_receipt()
    value.update(changes)
    assert controlled_acceptance_diagnostics(value)["accepted"] is False


@pytest.mark.parametrize("key,value", [
    ("record_id", "other"), ("policy_digest", "c" * 64),
    ("release_identity", {"release_commit": "c" * 40}), ("ok", False),
    ("certifies_natural_queries", True), ("schema", "other.v1"),
])
def test_independent_verification_must_bind_the_same_receipt_policy_and_release(key, value):
    receipt = accepted_receipt()
    receipt["verification"][key] = value
    assert controlled_acceptance_diagnostics(receipt)["accepted"] is False


def test_controlled_pass_cannot_hide_a_different_execution_failure():
    report = {"controlled_production_acceptance": accepted_receipt(),
              "storage_maintenance": {"ok": False, "error": "storage_failed"}}
    assert _aggregate_nightly_ok(report, []) is False
    result = nightly_result_diagnostics(report, [])
    assert result["production_operational_accepted"] is False
    assert "storage_maintenance" in result["failed_steps"]


def test_malformed_present_controlled_report_is_a_nightly_failure():
    report = {"controlled_production_acceptance": {"ok": "true"}}
    assert _aggregate_nightly_ok(report, []) is False
    assert "controlled_production_acceptance" in nightly_result_diagnostics(report, [])["failed_steps"]


def test_disabled_policy_does_not_change_natural_acceptance():
    value = {"ok": True, "accepted": False, "status": "not_configured"}
    result = nightly_result_diagnostics({"controlled_production_acceptance": value}, [])
    assert result["production_operational_accepted"] is False
    assert result["recall_quality_accepted"] is False
    assert result["failed_steps"] == []


def test_unconfigured_policy_does_not_reject_legacy_partial_scopes():
    from eimemory.evaluation.controlled_production_acceptance import load_controlled_policy
    assert load_controlled_policy(object(), {"agent_id": "legacy"}) is None


def test_native_run_requires_successful_second_verification(tmp_path):
    from eimemory.scheduler.jobs import _run_controlled_production_acceptance
    module = "eimemory.evaluation.controlled_production_acceptance"
    report = accepted_receipt()
    report.pop("runtime_authority_verified")
    report.pop("verification")
    authority = tmp_path / 'sqlite.db'
    authority.touch()
    runtime = SimpleNamespace(store=SimpleNamespace(sqlite=SimpleNamespace(path=authority)))
    with patch(module + ".load_controlled_policy", return_value={}), \
         patch(module + ".run_controlled_production_acceptance", return_value=report), \
         patch(module + ".verify_controlled_production_acceptance",
               return_value={"ok": False, "accepted": False}):
        result = _run_controlled_production_acceptance(runtime, scope={}, stable_before="")
    assert result["ok"] is False and result["accepted"] is False
    assert result["runtime_authority_verified"] is False
    assert result["error"] == "controlled_production_receipt_invalid"


def historical_business_receipt():
    value = accepted_receipt()
    value.update(persisted=True, acceptance_purpose="historical_business_qualification",
                 certifies_no_answer=False, host_delivery_certified=False, memory_contribution_certified=False)
    value["agents"] = [
        {"agent_id": agent, "scope_digest": str(index) * 64, "source_ids": ["hermes"],
         "sample_count": 3, "pass_count": 3, "fail_count": 0,
         "accepted": True, "complete": True, "blocked_reason": ""}
        for index, agent in enumerate(("hongrui", "hongtai", "hongxin", "xiaomage"), 1)
    ]
    value["business_qualification"] = {
        "schema": "historical_business_qualification.v1",
        "purpose": "historical_business_qualification", "accepted": True,
        "business_origin": "historical_business", "validation_method": "controlled_recall",
        "sample_count": 12, "pass_count": 12, "fail_count": 0,
        **{key: deepcopy(value[key]) for key in ("agents", "policy_digest", "record_id", "release_identity")},
    }
    value["verification"] = {key: deepcopy(item) for key, item in value.items()
                             if key not in {"verification", "runtime_authority_verified"}}
    return value


def _natural_wait_report():
    gate = {"ok": False, "blocked_reason": "recall_quality_evidence_incomplete",
            "blocking_metrics": {}, "recall_quality_evidence": {"execution_ok": True}}
    return {
        "controlled_production_acceptance": historical_business_receipt(),
        "recall_quality_gate": gate,
        "production_recall": {"ok": False, "execution_ok": True,
            "blocked_reason": "recall_quality_evidence_incomplete", "quality_gate": deepcopy(gate)},
        "recall_quality_per_agent": {"agents": {
            agent: {"ok": False, "sample_count": 0, "blocked_reason": "recall_quality_evidence_incomplete"}
            for agent in ("hongtai", "hongxin", "xiaomage")}, "combined": {"ok": False}},
    }


def test_qualified_historical_business_counts_are_main_business_evidence_with_online_progress_separate():
    report = _natural_wait_report()
    original = deepcopy(report)
    report["dynamic_capability_evolution"] = {"ok": False, "enabled": True, "status": "blocked",
        "execution": {"ok": False, "applied_count": 0, "results": [
            {"status": "blocked", "reason": "hypothesis_missing_or_ambiguous", "candidate_hypothesis_count": 0}]}}
    result = nightly_result_diagnostics(report, [])
    assert result["execution_ok"] and result["recall_quality_accepted"]
    assert result["recall_quality_acceptance_basis"] == "historical_business_memory_controlled"
    assert result["production_operational_accepted"]
    assert result["production_acceptance_basis"] == "controlled_existing_memory"
    assert result["natural_recall_quality_accepted"] is False
    assert result["natural_evidence_waits"] == ["production_recall", "recall_quality_gate"]
    assert "production_recall" not in result["evidence_waits"]
    assert "recall_quality_gate" not in result["evidence_waits"]
    assert result["evidence_waits"] == ["dynamic_capability_evolution"]
    main = result["recall_quality_per_agent"]
    assert main["combined"] == {"ok": True, "blocked_reason": "", "agent_count": 4,
                                "sample_count": 12, "pass_count": 12, "fail_count": 0}
    assert all(row["sample_count"] == row["pass_count"] == 3 for row in main["agents"].values())
    assert all(row["sample_count"] == 0 for row in result["natural_recall_quality_per_agent"]["agents"].values())
    assert result["recall_quality_evidence"]["formal_business_evidence"] is True
    assert result["recall_quality_evidence"]["validation_method"] == "controlled_recall"
    assert result["natural_recall_quality_evidence"] == {"execution_ok": True}
    assert result["controlled_production_acceptance"]["certifies_natural_queries"] is False
    assert result["controlled_production_acceptance"]["natural_samples_created"] == 0
    assert "label_trust_accepted" not in result["recall_quality_evidence"]
    assert all(report[key] == value for key, value in original.items())


@pytest.mark.parametrize("key,value", [
    ("schema", "foreign.v1"), ("purpose", "operational"), ("accepted", False),
    ("accepted", 1), ("business_origin", "generated"), ("validation_method", "natural"),
    ("sample_count", 11), ("pass_count", 11), ("fail_count", False), ("fail_count", 1),
    ("record_id", "foreign"), ("policy_digest", "c" * 64),
    ("release_identity", {"release_commit": "c" * 40}),
    ("unknown", "PRIVATE-UNKNOWN"),
])
def test_historical_business_qualification_must_match_its_native_receipt(key, value):
    receipt = historical_business_receipt()
    receipt["business_qualification"][key] = value
    receipt["verification"]["business_qualification"][key] = deepcopy(value)
    diagnostic = controlled_acceptance_diagnostics(receipt)
    assert diagnostic["accepted"] is True, "operational receipt is a separate claim"
    assert diagnostic["business_qualification"]["accepted"] is False


@pytest.mark.parametrize("key,value", [
    ("sample_count", 2), ("sample_count", True), ("pass_count", 2),
    ("fail_count", False), ("fail_count", 1), ("accepted", False),
    ("complete", False), ("blocked_reason", "retrieval_incomplete"),
    ("scope_digest", "invalid"), ("source_ids", []),
])
def test_historical_business_agents_require_three_complete_passes_each(key, value):
    receipt = historical_business_receipt()
    for agents in (receipt["agents"], receipt["business_qualification"]["agents"],
                   receipt["verification"]["agents"], receipt["verification"]["business_qualification"]["agents"]):
        agents[0][key] = deepcopy(value)
    assert controlled_acceptance_diagnostics(receipt)["business_qualification"]["accepted"] is False


@pytest.mark.parametrize("damage", ["purpose", "persisted", "verification_missing", "agents_mismatch",
                                   "business_mismatch", "duplicate_agent", "duplicate_scope"])
def test_historical_business_acceptance_requires_independent_matching_verified_projection(damage):
    receipt = historical_business_receipt()
    if damage == "purpose":
        receipt["acceptance_purpose"] = "operational"
    elif damage == "persisted":
        receipt["persisted"] = False
    elif damage == "verification_missing":
        receipt["verification"].pop("business_qualification")
    elif damage == "agents_mismatch":
        receipt["verification"]["agents"][0]["sample_count"] = 4
    elif damage == "business_mismatch":
        receipt["verification"]["business_qualification"]["pass_count"] = 11
    else:
        field = "agent_id" if damage == "duplicate_agent" else "scope_digest"
        for agents in (receipt["agents"], receipt["business_qualification"]["agents"],
                       receipt["verification"]["agents"], receipt["verification"]["business_qualification"]["agents"]):
            agents[1][field] = agents[0][field]
    assert controlled_acceptance_diagnostics(receipt)["business_qualification"]["accepted"] is False


@pytest.mark.parametrize("failure", ["storage", "authority", "quality", "step"])
def test_historical_business_evidence_does_not_hide_current_execution_or_source_failures(failure):
    report = _natural_wait_report()
    steps = []
    if failure == "storage":
        report["storage_maintenance"] = {"ok": False, "error": "storage_failed"}
    elif failure == "authority":
        report["recall_quality_gate"]["release_source_validation"] = {"ok": False}
    elif failure == "quality":
        report["recall_quality_gate"] = {"ok": False, "blocked_reason": "false_recall_rate_above_threshold",
                                        "blocking_metrics": {"false_recall_rate": 1}}
    else:
        steps = [{"step": "source_version_check", "ok": False}]
    result = nightly_result_diagnostics(report, steps)
    assert result["execution_ok"] is False
    assert result["recall_quality_accepted"] is False
    assert result["production_operational_accepted"] is False
    assert result["failed_steps"]
    assert result["recall_quality_per_agent"] == result["natural_recall_quality_per_agent"]


def test_business_projection_does_not_copy_private_case_data():
    receipt = historical_business_receipt()
    for agents in (receipt["agents"], receipt["business_qualification"]["agents"],
                   receipt["verification"]["agents"], receipt["verification"]["business_qualification"]["agents"]):
        agents[0]["query"] = "PRIVATE-HISTORICAL-QUERY"
        agents[0]["scope"] = {"user_id": "PRIVATE-EXACT-USER"}
    result = nightly_result_diagnostics({"controlled_production_acceptance": receipt}, [])
    assert result["recall_quality_accepted"] is True
    assert "PRIVATE-" not in json.dumps(result)


def test_existing_natural_quality_acceptance_remains_independent_when_history_is_not_qualified():
    from eimemory.evaluation.real_query_schema import (
        PRODUCTION_REAL_QUERY_REPORT_SCHEMA, PRODUCTION_REAL_QUERY_POLICY,
    )
    gate = {"ok": True, "recall_quality_evidence": {
        "evaluation_contract": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        "formal_business_evidence": True, "label_trust_accepted": True,
        "release_authority_verified": True, "authority_record_id": "natural-record"}}
    quality = {"schema": PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        "policy_schema": PRODUCTION_REAL_QUERY_POLICY, "dataset_kind": "production",
        "accepted": True, "gate_status": "accepted", "persisted": True,
        "persisted_record_id": "natural-record", "quality_gate": deepcopy(gate)}
    report = {"recall_quality_gate": gate, "recall_quality": quality,
        "controlled_production_acceptance": accepted_receipt(),
        "recall_quality_per_agent": {"agents": {"hongrui": {"ok": True, "sample_count": 3}},
                                     "combined": {"ok": True}}}
    result = nightly_result_diagnostics(report, [])
    assert result["recall_quality_accepted"] and result["natural_recall_quality_accepted"]
    assert result["recall_quality_acceptance_basis"] == "natural_production_queries"
    assert result["recall_quality_per_agent"] == result["natural_recall_quality_per_agent"]
    assert result["recall_quality_evidence"] == result["natural_recall_quality_evidence"]
