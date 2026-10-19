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
    assert report["known_before_detection"] is None
    assert report["prior_user_reported"] is None
    assert report["incident"]["incident_class"] == INCIDENT_CLASS
    assert "without reusing or resetting" in report["incident"]["summary"]


def test_current_policy_state_is_not_incident() -> None:
    current = detect_system_code_repair_failure(
        _repair_report(),
        policy=_policy(base_commit=CURRENT),
        release_commit=CURRENT,
        detected_at="2026-09-02T15:00:00Z",
    )
    assert current["status"] == "non_actionable"


def test_idle_router_does_not_turn_an_unused_old_policy_into_a_system_incident() -> None:
    report = detect_system_code_repair_failure(
        {"ok": True, "status": "idle", "processed": []},
        policy=_policy(),
        release_commit=CURRENT,
        detected_at="2026-09-02T15:00:00Z",
    )

    assert report["ok"] is True and report["status"] == "non_actionable"
    assert report["incident"] is None and report["blocked_reason"] == ""
    assert report["policy_transaction_id"] == ""


def test_healthy_repeated_native_recording_preserves_existing_incidents_without_appending(tmp_path, monkeypatch) -> None:
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.models.records import RecordEnvelope, ScopeRef
    store = RuntimeStore(tmp_path / "runtime")
    scope = ScopeRef.from_dict(SCOPE)
    old = store.append(RecordEnvelope.create(kind="incident", title="original observation", scope=scope))
    monkeypatch.setattr("eimemory.ops.system_code_repair_failure.current_release_identity",
                        lambda *_args, **_kwargs: SimpleNamespace(commit=CURRENT))
    monkeypatch.setattr("eimemory.ops.system_code_repair_failure.load_code_automation_policy", lambda **_kwargs: _policy())
    runtime = SimpleNamespace(store=store)
    try:
        for observed in ("2026-10-19T00:00:00Z", "2026-10-19T00:05:00Z"):
            result = record_system_code_repair_failure(runtime, scope=SCOPE,
                repair_report={"ok": True, "status": "idle", "processed": []}, detected_at=observed)
            assert result["status"] == "non_actionable" and result["incident_record_id"] == ""
        rows = store.list_records(kinds=["incident"], scope=scope, limit=10)
        assert len(rows) == 1 and rows[0].to_dict() == old.to_dict()
    finally:
        store.close()


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
        "known_before_detection": None,
        "prior_user_reported": None,
        'manual_bootstrap': None,
    }
