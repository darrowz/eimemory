from __future__ import annotations

from datetime import datetime, timedelta, timezone

from eimemory.api.runtime import Runtime
from eimemory.governance.l5_maturity import (
    STAGE_ORDER,
    apply_monotonic_maturity,
    resolve_maturity_checkpoint,
)
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


SCOPE = ScopeRef(
    agent_id="maturity-agent",
    workspace_id="maturity-workspace",
    user_id="maturity-user",
)


def _append_historical_readiness(runtime: Runtime, *, stage: str, created_at: datetime) -> str:
    record = RecordEnvelope.create(
        kind="reflection",
        title="Historical readiness",
        summary=stage,
        scope=SCOPE,
        source="eimemory.l5_readiness",
        content={
            "report_type": "l5_readiness_report",
            "schema_version": "l5_readiness.v2",
            "current_stage": stage,
            "readiness_score": 1.0 if stage == "L5" else 0.8,
        },
        meta={
            "report_type": "l5_readiness_report",
            "schema_version": "l5_readiness.v2",
            "stage": stage,
        },
    )
    timestamp = created_at.isoformat()
    record.time = TimeRef(created_at=timestamp, updated_at=timestamp, occurred_at=timestamp)
    return runtime.store.append(record).record_id


def _append_fatal_incident(
    runtime: Runtime,
    *,
    created_at: datetime,
    target_stage: str = "L4.5",
    scope: ScopeRef = SCOPE,
    severity: str = "critical",
    fatal: bool = True,
    status: str = "confirmed",
    evidence_record_ids: list[str] | None = None,
    confirmed_by: str = "release-owner",
) -> str:
    payload = {
        "incident_type": "l5_fatal_regression",
        "severity": severity,
        "fatal": fatal,
        "status": status,
        "target_stage": target_stage,
        "evidence_record_ids": ["evidence-1"] if evidence_record_ids is None else evidence_record_ids,
        "confirmed_by": confirmed_by,
    }
    record = RecordEnvelope.create(
        kind="incident",
        title="Confirmed fatal L5 regression",
        summary="redacted",
        scope=scope,
        source="eimemory.l5_maturity",
        content=payload,
        meta=payload,
    )
    timestamp = created_at.isoformat()
    record.time = TimeRef(created_at=timestamp, updated_at=timestamp, occurred_at=timestamp)
    return runtime.store.append(record).record_id


def test_stage_order_and_checkpoint_identity_are_version_neutral(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        result = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L5",
            observed_score=1.0,
            persist=True,
            loop_id="maturity-test",
        )
        checkpoint = runtime.store.get_by_id(result["maturity_checkpoint_record_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert list(STAGE_ORDER) == ["L3.5", "L4", "L4.5", "L5"]
    assert result["current_stage"] == "L5"
    assert checkpoint is not None
    serialized_identity = str(checkpoint.meta.get("semantic_key") or checkpoint.content.get("semantic_key") or "")
    assert "version" not in serialized_identity
    assert "commit" not in serialized_identity
    assert "receipt" not in serialized_identity
    assert "session" not in serialized_identity


def test_lower_release_observation_holds_accumulated_l5(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        first = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L5",
            observed_score=1.0,
            persist=True,
            loop_id="release-a",
        )
        later = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L3.5",
            observed_score=0.2,
            persist=True,
            loop_id="release-b",
        )
    finally:
        runtime.close()

    assert first["current_stage"] == "L5"
    assert later["observed_stage"] == "L3.5"
    assert later["current_stage"] == "L5"
    assert later["maturity_transition"] == "held"
    assert later["maturity_checkpoint_record_id"] == first["maturity_checkpoint_record_id"]
    assert later["regression_warning"]


def test_historical_readiness_bootstraps_highest_stage(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    now = datetime.now(timezone.utc)
    try:
        _append_historical_readiness(runtime, stage="L4", created_at=now)
        historical_l5 = _append_historical_readiness(
            runtime,
            stage="L5",
            created_at=now + timedelta(seconds=1),
        )
        result = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L3.5",
            observed_score=0.2,
            persist=False,
            loop_id="bootstrap",
        )
    finally:
        runtime.close()

    assert result["current_stage"] == "L5"
    assert result["maturity_checkpoint_record_id"] == historical_l5
    assert result["maturity_transition"] == "held"


def test_only_newer_confirmed_fatal_incident_can_downgrade_and_downgrade_persists(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    now = datetime.now(timezone.utc)
    try:
        first = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L5",
            observed_score=1.0,
            persist=True,
            loop_id="before-fatal",
        )
        incident_id = _append_fatal_incident(
            runtime,
            created_at=now + timedelta(minutes=1),
        )
        downgraded = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L3.5",
            observed_score=0.2,
            persist=True,
            loop_id="fatal",
        )
        later_release = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L5",
            observed_score=1.0,
            persist=True,
            loop_id="after-fatal",
        )
    finally:
        runtime.close()

    assert first["current_stage"] == "L5"
    assert downgraded["current_stage"] == "L4.5"
    assert downgraded["maturity_transition"] == "fatal_downgrade"
    assert downgraded["downgrade_incident_id"] == incident_id
    assert later_release["current_stage"] == "L4.5"
    assert later_release["downgrade_incident_id"] == incident_id


def test_checkpoint_survives_more_than_one_thousand_unrelated_reflections(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        baseline = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L5",
            observed_score=1.0,
            persist=True,
            loop_id="high-water",
        )
        for index in range(1005):
            runtime.store.append(
                RecordEnvelope.create(
                    kind="reflection",
                    title=f"Unrelated {index}",
                    summary="noise",
                    scope=SCOPE,
                    source="test.unrelated",
                    content={"index": index},
                )
            )
        result = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L3.5",
            observed_score=0.2,
            persist=False,
            loop_id="after-noise",
        )
    finally:
        runtime.close()

    assert result["current_stage"] == "L5"
    assert result["maturity_checkpoint_record_id"] == baseline["maturity_checkpoint_record_id"]


def test_malformed_or_cross_scope_incidents_cannot_downgrade(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    now = datetime.now(timezone.utc)
    other_scope = ScopeRef(
        agent_id=SCOPE.agent_id,
        workspace_id="other",
        user_id=SCOPE.user_id,
    )
    try:
        baseline = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L5",
            observed_score=1.0,
            persist=True,
            loop_id="baseline",
        )
        invalid_ids = [
            _append_fatal_incident(
                runtime,
                created_at=now + timedelta(seconds=1),
                severity="high",
            ),
            _append_fatal_incident(
                runtime,
                created_at=now + timedelta(seconds=2),
                status="unconfirmed",
            ),
            _append_fatal_incident(
                runtime,
                created_at=now + timedelta(seconds=3),
                evidence_record_ids=[],
            ),
            _append_fatal_incident(
                runtime,
                created_at=now + timedelta(seconds=4),
                scope=other_scope,
            ),
        ]
        result = apply_monotonic_maturity(
            runtime,
            scope=SCOPE,
            observed_stage="L3.5",
            observed_score=0.2,
            persist=True,
            loop_id="invalid-fatal",
        )
        checkpoint = resolve_maturity_checkpoint(runtime, scope=SCOPE)
    finally:
        runtime.close()

    assert invalid_ids
    assert result["current_stage"] == "L5"
    assert result["maturity_transition"] == "held"
    assert result["downgrade_incident_id"] == ""
    assert checkpoint["record_id"] == baseline["maturity_checkpoint_record_id"]
