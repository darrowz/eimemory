from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance import evidence_contract
from eimemory.governance.evidence_contract import (
    EvidenceRequirement,
    ReleaseIdentity,
    current_release_identity,
    release_identity_payload,
    resolve_evidence,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
OTHER_SCOPE = ScopeRef(agent_id="other", workspace_id="embodied", user_id="darrow")
RELEASE = ReleaseIdentity(
    commit="a" * 40,
    version="1.9.70",
    receipt_id="receipt-current",
    session_id="release-session-current",
)
REQUIREMENT = EvidenceRequirement(
    kinds=frozenset({"l5_world_model"}),
    sources=frozenset({"eimemory.l5.world_model"}),
    statuses=frozenset({"active"}),
    evidence_classes=frozenset({"structural"}),
)


def _evidence(
    *,
    scope: ScopeRef,
    commit: str = RELEASE.commit,
    version: str = RELEASE.version,
    receipt_id: str = RELEASE.receipt_id,
    session_id: str = RELEASE.session_id,
) -> RecordEnvelope:
    payload = {
        "evidence_class": "structural",
        "release_commit": commit,
        "release_version": version,
        "deployment_receipt_id": receipt_id,
        "release_session_id": session_id,
    }
    return RecordEnvelope.create(
        kind="l5_world_model",
        title="Release-bound world model",
        scope=scope,
        source="eimemory.l5.world_model",
        status="active",
        content=payload,
        meta=payload,
    )


def test_evidence_resolver_rejects_missing_wrong_scope_and_stale_release(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    wrong_scope = runtime.store.append(_evidence(scope=OTHER_SCOPE))
    stale_release = runtime.store.append(_evidence(scope=SCOPE, commit="b" * 40))
    try:
        assert resolve_evidence(runtime, "missing", REQUIREMENT, SCOPE, RELEASE).reason == "record_not_found"
        assert resolve_evidence(runtime, wrong_scope.record_id, REQUIREMENT, SCOPE, RELEASE).reason == "scope_mismatch"
        assert resolve_evidence(runtime, stale_release.record_id, REQUIREMENT, SCOPE, RELEASE).reason == "release_mismatch"
    finally:
        runtime.close()


def test_evidence_resolver_accepts_only_exact_contract(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    evidence = runtime.store.append(_evidence(scope=SCOPE))
    try:
        result = resolve_evidence(runtime, evidence.record_id, REQUIREMENT, SCOPE, RELEASE)
    finally:
        runtime.close()

    assert result.ok is True
    assert result.reason == "ok"
    assert result.record_id == evidence.record_id
    assert result.record is not None


def test_l5_evidence_authority_ignores_version_but_not_commit_receipt_or_session(
    tmp_path,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    descriptive_version_drift = runtime.store.append(
        _evidence(scope=SCOPE, version="9.9.999")
    )
    wrong_receipt = runtime.store.append(
        _evidence(scope=SCOPE, receipt_id="receipt-other")
    )
    wrong_session = runtime.store.append(
        _evidence(scope=SCOPE, session_id="session-other")
    )
    versionless = ReleaseIdentity(
        commit=RELEASE.commit,
        version="",
        receipt_id=RELEASE.receipt_id,
        session_id=RELEASE.session_id,
    )
    try:
        accepted = resolve_evidence(
            runtime,
            descriptive_version_drift.record_id,
            REQUIREMENT,
            SCOPE,
            versionless,
        )
        rejected_receipt = resolve_evidence(
            runtime, wrong_receipt.record_id, REQUIREMENT, SCOPE, RELEASE
        )
        rejected_session = resolve_evidence(
            runtime, wrong_session.record_id, REQUIREMENT, SCOPE, RELEASE
        )
    finally:
        runtime.close()

    assert versionless.complete is True
    assert accepted.ok is True
    assert rejected_receipt.reason == "release_mismatch"
    assert rejected_session.reason == "release_mismatch"


def test_current_l5_release_selection_does_not_bind_imported_package_version(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    receipt = runtime.store.append(_deployment_receipt())
    monkeypatch.setattr(
        evidence_contract,
        "_runtime_commit",
        lambda _runtime: RELEASE.commit,
    )
    try:
        release = current_release_identity(runtime, SCOPE)
    finally:
        runtime.close()

    assert release == ReleaseIdentity(
        commit=RELEASE.commit,
        version=RELEASE.version,
        receipt_id=receipt.record_id,
        session_id=receipt.record_id,
    )


def test_current_release_identity_uses_exact_receipt_after_record_churn(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    receipt = runtime.store.append(_deployment_receipt())
    runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Newer unrelated promotion",
            scope=SCOPE,
            source="eimemory.governance.noise",
            status="active",
            content={"report_type": "unrelated"},
            meta={"report_type": "unrelated"},
        )
    )
    monkeypatch.setattr(
        evidence_contract,
        "_runtime_commit",
        lambda _runtime: RELEASE.commit,
    )
    try:
        release = current_release_identity(runtime, SCOPE, limit=1)
    finally:
        runtime.close()

    assert release == ReleaseIdentity(
        commit=RELEASE.commit,
        version=RELEASE.version,
        receipt_id=receipt.record_id,
        session_id=receipt.record_id,
    )


def test_verified_real_task_release_identity_is_server_bound_and_current(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = RELEASE.commit
    receipt = runtime.store.append(_deployment_receipt())
    release = current_release_identity(runtime, SCOPE)
    assert release == ReleaseIdentity(
        commit=RELEASE.commit,
        version=RELEASE.version,
        receipt_id=receipt.record_id,
        session_id=receipt.record_id,
    )
    event_payload = {
        "source": "openclaw.agent_end",
        "hook": "agent_end",
        "session_id": "session-current-task",
        "event_type": "repo.deploy",
        "outcome_trace_id": "current-real-task",
        "outcome_trace_task_type": "repo.deploy",
        **release_identity_payload(release),
    }
    event = runtime.store.record_event(event_payload, scope=SCOPE)
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
            "session_id": "session-current-task",
            "trace_id": "current-real-task",
            "task_type": "repo.deploy",
            "release_commit": "b" * 40,
            "outcome": {"status": "success", "success": True, "rehearsal": False},
            "verifier": {
                "passed": True,
                "method": "openclaw.agent_end",
                "evidence_refs": [event["id"]],
            },
        },
        scope=SCOPE,
    )
    assert result["ok"] is True
    try:
        report = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
        trace = runtime.store.get_by_id(result["record_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert trace is not None
    assert trace.content["payload"]["release_commit"] == RELEASE.commit
    assert report["sample_counts"]["current_deployment_verified_real_tasks"] == 1
    assert report["sample_counts"]["current_deployment_verified_real_task_types"] == 1
    assert report["metrics"]["current_deployment_verified_real_task_success_rate"] == 1.0


def _deployment_receipt() -> RecordEnvelope:
    release_path = f"/opt/eimemory/releases/{RELEASE.commit}"
    payload = {
        "report_type": "deployment_receipt",
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
                "commit": RELEASE.commit,
                "version": RELEASE.version,
                "release_path": release_path,
            },
            "commit": {"commit_sha": RELEASE.commit},
            "release": {"version": RELEASE.version, "release_path": release_path},
            "rollback_evidence": {
                "prior_commit_sha": "b" * 40,
                "rollback_command": "verified rollback",
            },
        },
    }
    return RecordEnvelope.create(
        kind="promotion_request",
        title="Current deployment receipt",
        scope=SCOPE,
        source="eimemory.deployment_receipt",
        status="deployed",
        content=payload,
        meta={
            "report_type": "deployment_receipt",
            "commit_sha": RELEASE.commit,
        },
    )
