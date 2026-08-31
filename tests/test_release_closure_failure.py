from __future__ import annotations

import pytest

from eimemory.api.runtime import Runtime
from eimemory.ops.release_closure_failure import (
    detect_release_closure_failure,
    record_release_closure_failure,
)


SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}


def _failed_report() -> dict:
    return {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "blocked_stage": "release_lineage",
        "blocked_reason": "release_lineage_not_compatible",
        "deployment": {"commit": "a" * 40, "version": "1.11.40"},
        "release_lineage": {
            "ok": True,
            "compatible": False,
            "unknown_production_paths": [],
        },
    }


def test_release_closure_failure_becomes_exact_system_incident() -> None:
    report = detect_release_closure_failure(
        _failed_report(),
        detected_at="2026-08-28T12:00:00Z",
    )

    assert report["ok"] is False
    assert report["status"] == "failure_detected"
    assert report["origin"] == "system_detector"
    assert report["known_before_detection"] is False
    assert report["prior_user_reported"] is False
    assert report["manual_bootstrap"] is False
    assert report["observation_valid"] is True
    assert set(report["incident"]) == {
        "incident_id",
        "incident_digest",
        "incident_class",
        "title",
        "summary",
        "diagnostic_codes",
        "acceptance_requirements",
    }
    assert report["incident"]["incident_class"] == "release.closure_internal_failure"


def test_expected_data_accumulation_is_not_a_code_incident() -> None:
    report = detect_release_closure_failure(
        {
            **_failed_report(),
            "data_accumulating": True,
            "blocked_stage": "production_recall_gate",
            "blocked_reason": "production_dataset_not_ready",
        },
        detected_at="2026-08-28T12:00:00Z",
    )

    assert report["ok"] is True
    assert report["status"] == "non_actionable"
    assert report["incident"] is None


def test_code_evolution_evidence_failure_declares_exact_receipt_requirement() -> None:
    report = detect_release_closure_failure(
        {
            **_failed_report(),
            "blocked_reason": "code_evolution_gate_evidence_missing",
        },
        detected_at="2026-08-28T12:00:00Z",
    )

    assert (
        "code_evolution_gate_uses_exact_current_deployment_receipt"
        in report["incident"]["acceptance_requirements"]
    )
    assert {
        "deployment_receipt_is_authoritative_input",
        "storage_acceptance_records_are_not_deployment_receipts",
        "deployment_receipt_fallback_is_forbidden",
        "missing_deployment_receipt_fails_closed",
    } <= set(report["incident"]["acceptance_requirements"])
    assert "never infer or replace it from live record IDs" in report["incident"]["summary"]


@pytest.mark.parametrize(
    "reason",
    [
        "strict_code_evolution_receipt_required",
        "observation_not_valid",
        "waiting_for_observation",
    ],
)
def test_expected_pre_observation_states_are_not_code_incidents(reason: str) -> None:
    report = detect_release_closure_failure(
        {**_failed_report(), "blocked_reason": reason},
        detected_at="2026-08-28T12:00:00Z",
    )

    assert report["ok"] is True
    assert report["status"] == "non_actionable"
    assert report["incident"] is None


def test_release_closure_failure_persistence_is_idempotent(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        first = record_release_closure_failure(
            runtime,
            scope=SCOPE,
            closure_report=_failed_report(),
            detected_at="2026-08-28T12:00:00Z",
        )
        retry = record_release_closure_failure(
            runtime,
            scope=SCOPE,
            closure_report=_failed_report(),
            detected_at="2026-08-28T12:00:00Z",
        )
        incidents = runtime.store.list_records(kinds=["incident"], scope=SCOPE, limit=10)
    finally:
        runtime.close()

    assert first["incident_record_id"] == retry["incident_record_id"]
    assert len(incidents) == 1
    assert incidents[0].source == "eimemory.release_closure_failure"
    assert incidents[0].provenance["origin"] == "system_detector"
