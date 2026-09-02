from __future__ import annotations

from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from eimemory.ops.system_code_repair_failure import (
    INCIDENT_CLASS,
    detect_system_code_repair_failure,
    record_system_code_repair_failure,
)


SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}
CURRENT = "a" * 40
OLD = "b" * 40


def _policy(*, base_commit: str = OLD) -> dict:
    return {
        "ok": True,
        "status": "enabled",
        "policy_digest": "c" * 64,
        "repository": {"base_commit": base_commit},
    }


def _repair_report() -> dict:
    return {
        "ok": False,
        "status": "blocked",
        "reason": "automation_policy_already_consumed",
        "policy_transaction_id": "old-transaction",
        "processed": [],
    }


def test_consumed_policy_for_older_base_is_system_detected() -> None:
    report = detect_system_code_repair_failure(
        _repair_report(),
        policy=_policy(),
        release_commit=CURRENT,
        detected_at="2026-09-02T15:00:00Z",
    )

    assert report["status"] == "failure_detected"
    assert report["origin"] == "system_detector"
    assert report["known_before_detection"] is False
    assert report["prior_user_reported"] is False
    assert report["incident"]["incident_class"] == INCIDENT_CLASS
    assert "without reusing or resetting" in report["incident"]["summary"]


def test_current_or_unobserved_policy_state_is_not_incident() -> None:
    current = detect_system_code_repair_failure(
        _repair_report(),
        policy=_policy(base_commit=CURRENT),
        release_commit=CURRENT,
        detected_at="2026-09-02T15:00:00Z",
    )
    idle = detect_system_code_repair_failure(
        {"ok": True, "status": "idle", "processed": []},
        policy=_policy(),
        release_commit=CURRENT,
        detected_at="2026-09-02T15:00:00Z",
    )

    assert current["status"] == "non_actionable"
    assert idle["status"] == "non_actionable"


def test_recording_is_idempotent_and_preserves_detector_provenance(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    monkeypatch.setattr(
        "eimemory.ops.system_code_repair_failure.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=CURRENT),
    )
    monkeypatch.setattr(
        "eimemory.ops.system_code_repair_failure.load_code_automation_policy",
        lambda **_kwargs: _policy(),
    )
    try:
        first = record_system_code_repair_failure(
            runtime,
            scope=SCOPE,
            repair_report=_repair_report(),
            detected_at="2026-09-02T15:00:00Z",
        )
        second = record_system_code_repair_failure(
            runtime,
            scope=SCOPE,
            repair_report=_repair_report(),
            detected_at="2026-09-02T15:15:00Z",
        )
        records = runtime.store.list_records_by_meta_value(
            kinds=["incident"],
            scope=SCOPE,
            meta_key="incident_class",
            meta_value=INCIDENT_CLASS,
            limit=10,
        )
    finally:
        runtime.close()

    assert first["incident_record_id"] == second["incident_record_id"]
    assert len(records) == 1
    assert records[0].source == "eimemory.system_code_repair_failure"
    assert records[0].provenance == {
        "origin": "system_detector",
        "detector": "eimemory.system_code_repair_failure.v1",
        "known_before_detection": False,
        "prior_user_reported": False,
    }
