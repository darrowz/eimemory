from copy import deepcopy
import json
from unittest.mock import patch

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


def test_native_run_requires_successful_second_verification():
    from eimemory.scheduler.jobs import _run_controlled_production_acceptance
    module = "eimemory.evaluation.controlled_production_acceptance"
    report = accepted_receipt()
    report.pop("runtime_authority_verified")
    report.pop("verification")
    with patch(module + ".load_controlled_policy", return_value={}), \
         patch(module + ".run_controlled_production_acceptance", return_value=report), \
         patch(module + ".verify_controlled_production_acceptance",
               return_value={"ok": False, "accepted": False}):
        result = _run_controlled_production_acceptance(object(), scope={}, stable_before="")
    assert result["ok"] is False and result["accepted"] is False
    assert result["runtime_authority_verified"] is False
    assert result["error"] == "controlled_production_receipt_invalid"
