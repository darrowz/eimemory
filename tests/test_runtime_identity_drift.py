from __future__ import annotations

from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from eimemory.ops.runtime_identity_drift import (
    PYTHON_RUNTIME_UNITS,
    detect_runtime_identity_drift,
    inspect_live_runtime_identity,
)


CURRENT = "a" * 40
STALE = "b" * 40


def test_detector_emits_exact_system_incident_for_stale_learning_runtime() -> None:
    report = detect_runtime_identity_drift(
        expected_commit=CURRENT,
        unit_environments={
            "eimemory-rpc.service": f"EIMEMORY_RUNTIME_COMMIT={CURRENT}",
            "eimemory-learn-watch.service": (
                f"PYTHONPATH=/opt/eimemory/current EIMEMORY_RUNTIME_COMMIT={STALE}"
            ),
        },
        detected_at="2026-08-27T04:00:00Z",
    )

    assert report["ok"] is False
    assert report["status"] == "drift_detected"
    assert report["origin"] == "system_detector"
    assert report["detector"] == "eimemory.runtime_identity_drift.v1"
    assert report["known_before_detection"] is False
    assert report["prior_user_reported"] is False
    assert report["manual_bootstrap"] is False
    assert report["observation_valid"] is True
    assert report["mismatches"] == [
        {
            "unit": "eimemory-learn-watch.service",
            "reason": "commit_mismatch",
            "observed_commit": STALE,
        }
    ]
    assert set(report["incident"]) == {
        "incident_id",
        "incident_digest",
        "incident_class",
        "title",
        "summary",
        "diagnostic_codes",
        "acceptance_requirements",
    }
    assert report["incident"]["incident_class"] == "deployment.runtime_commit_drift"


def test_detector_is_healthy_only_when_every_unit_has_one_current_commit() -> None:
    healthy = detect_runtime_identity_drift(
        expected_commit=CURRENT,
        unit_environments={
            "eimemory-rpc.service": f"EIMEMORY_RUNTIME_COMMIT={CURRENT}",
            "eimemory-nightly.service": f"EIMEMORY_RUNTIME_COMMIT={CURRENT}",
        },
        detected_at="2026-08-27T04:00:00Z",
    )
    missing = detect_runtime_identity_drift(
        expected_commit=CURRENT,
        unit_environments={"eimemory-nightly.service": "PYTHONPATH=/opt/eimemory/current"},
        detected_at="2026-08-27T04:00:00Z",
    )

    assert healthy["ok"] is True
    assert healthy["status"] == "current"
    assert healthy["incident"] is None
    assert missing["mismatches"][0]["reason"] == "runtime_commit_missing"


def test_live_detector_persists_one_idempotent_system_incident(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {
        "tenant_id": "default",
        "agent_id": "hongtu",
        "workspace_id": "embodied",
        "user_id": "darrow",
    }
    monkeypatch.setattr(
        "eimemory.governance.evidence_contract.current_release_identity",
        lambda *_args, **_kwargs: SimpleNamespace(commit=CURRENT),
    )

    def environment(unit: str) -> str:
        commit = STALE if unit == "eimemory-nightly.service" else CURRENT
        return f"EIMEMORY_RUNTIME_COMMIT={commit}"

    try:
        first = inspect_live_runtime_identity(
            runtime,
            scope=scope,
            detected_at="2026-08-27T04:00:00Z",
            runner=environment,
        )
        retry = inspect_live_runtime_identity(
            runtime,
            scope=scope,
            detected_at="2026-08-27T04:00:00Z",
            runner=environment,
        )
        incidents = runtime.store.list_records(kinds=["incident"], limit=10)
    finally:
        runtime.close()

    assert len(PYTHON_RUNTIME_UNITS) > 5
    assert first["incident_record_id"] == retry["incident_record_id"]
    assert len(incidents) == 1
    assert incidents[0].provenance["origin"] == "system_detector"
    assert incidents[0].meta["observation_valid"] is True
