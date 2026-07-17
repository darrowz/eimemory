from __future__ import annotations

from pathlib import Path

from eimemory.api.runtime import Runtime
from eimemory.governance import capability_dashboard
from eimemory.governance.live_task_acceptance import LIVE_ACCEPTANCE_CASE_IDS, live_acceptance_task_type
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


SCOPE = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}


def test_verified_live_task_metric_requires_trusted_referenced_non_rehearsal_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        _append_deployment_receipt(runtime, scope_ref, commit="a" * 40, created_at="2026-07-13T00:00:00+00:00")
        for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
            _append_acceptance(runtime, scope_ref, index=index, case_id=case_id, passed=index < 9)
        runtime.record_outcome_trace(
            {
                "source": "eimemory.live_task_acceptance",
                "trace_id": "forged-missing-evidence",
                "task_type": "live.acceptance.forged",
                "outcome": {"status": "success", "success": True, "rehearsal": False},
                "verifier": {
                    "passed": True,
                    "method": "eimemory.live_task_acceptance",
                    "evidence_refs": ["missing-evidence"],
                },
                "deployment_commit": "a" * 40,
                "acceptance_case_id": "forged",
            },
            scope=SCOPE,
        )
        rehearsal_evidence = runtime.store.append(
            RecordEnvelope.create(
                kind="learning_eval",
                title="rehearsal evidence",
                summary="must not count",
                scope=scope_ref,
                source="eimemory.live_task_acceptance",
                content={"report_type": "live_task_acceptance_case", "schema_version": "live_task_acceptance.v1", "case_id": "rehearsal", "task_type": "live.acceptance.rehearsal", "trace_id": "rehearsal", "passed": True, "deployment_commit": "a" * 40},
                meta={"report_type": "live_task_acceptance_case", "schema_version": "live_task_acceptance.v1", "case_id": "rehearsal", "task_type": "live.acceptance.rehearsal", "trace_id": "rehearsal", "passed": True, "deployment_commit": "a" * 40},
            )
        )
        runtime.record_outcome_trace(
            {
                "source": "eimemory.live_task_acceptance",
                "trace_id": "rehearsal",
                "task_type": "live.acceptance.rehearsal",
                "outcome": {"status": "success", "success": True, "rehearsal": True},
                "verifier": {"passed": True, "method": "eimemory.live_task_acceptance", "evidence_refs": [rehearsal_evidence.record_id]},
                "deployment_commit": "a" * 40,
                "acceptance_case_id": "rehearsal",
            },
            scope=SCOPE,
        )

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["metrics"]["verified_live_task_success_rate"] == 0.9
    assert metrics["metric_quality"]["verified_live_task_success_rate"] == {
        "sample_count": 10,
        "minimum": 10,
        "sufficient": True,
    }
    assert metrics["sample_counts"]["verified_live_tasks"] == 10
    assert metrics["sample_counts"]["verified_live_task_types"] == 10
    assert metrics["sample_counts"]["current_deployment_acceptance"] == 10
    assert metrics["metrics"]["current_deployment_live_task_success_rate"] == 0.9


def test_previous_deployment_acceptance_does_not_satisfy_current_deployment_gate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        _append_deployment_receipt(runtime, scope_ref, commit="a" * 40, created_at="2026-07-13T00:00:00+00:00")
        for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
            _append_acceptance(runtime, scope_ref, index=index, case_id=case_id, passed=True)
        _append_deployment_receipt(runtime, scope_ref, commit="c" * 40, created_at="2099-07-13T01:00:00+00:00")
        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["metrics"]["verified_live_task_success_rate"] == 1.0
    assert metrics["sample_counts"]["verified_live_tasks"] == 10
    assert metrics["sample_counts"]["current_deployment_acceptance"] == 0


def test_current_deployment_failures_cannot_be_masked_by_previous_successes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "c" * 40
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        _append_deployment_receipt(runtime, scope_ref, commit="a" * 40, created_at="2026-07-13T00:00:00+00:00")
        for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
            _append_acceptance(runtime, scope_ref, index=index, case_id=case_id, passed=True, commit="a" * 40)
        _append_deployment_receipt(runtime, scope_ref, commit="c" * 40, created_at="2099-07-13T01:00:00+00:00")
        for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
            _append_acceptance(runtime, scope_ref, index=index, case_id=case_id, passed=False, commit="c" * 40)
        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["metrics"]["verified_live_task_success_rate"] == 0.5
    assert metrics["metrics"]["current_deployment_live_task_success_rate"] == 0.0
    assert metrics["sample_counts"]["current_deployment_acceptance"] == 10


def test_older_receipt_updated_later_does_not_hide_current_runtime_receipt(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "c" * 40
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        older_receipt_id = _append_deployment_receipt(
            runtime,
            scope_ref,
            commit="a" * 40,
            created_at="2026-07-13T00:00:00+00:00",
        )
        _append_deployment_receipt(
            runtime,
            scope_ref,
            commit="c" * 40,
            created_at="2026-07-17T00:00:00+00:00",
        )
        for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
            _append_acceptance(runtime, scope_ref, index=index, case_id=case_id, passed=True, commit="c" * 40)
        runtime.store.sqlite.conn.execute(
            "UPDATE records SET updated_at = ? WHERE record_id = ?",
            ("2099-07-17T00:00:00+00:00", older_receipt_id),
        )
        runtime.store.sqlite.conn.commit()

        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["metrics"]["current_deployment_live_task_success_rate"] == 1.0
    assert metrics["sample_counts"]["current_deployment_acceptance"] == 10


def test_runtime_commit_must_match_latest_deployment_receipt(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    monkeypatch.setenv("EIMEMORY_RUNTIME_COMMIT", "c" * 40)
    try:
        _append_deployment_receipt(runtime, scope_ref, commit="a" * 40)
        for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
            _append_acceptance(runtime, scope_ref, index=index, case_id=case_id, passed=True)
        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["sample_counts"]["verified_live_tasks"] == 10
    assert metrics["sample_counts"]["current_deployment_acceptance"] == 0
    assert metrics["metrics"]["current_deployment_live_task_success_rate"] == 0.0


def test_development_checkout_with_matching_env_and_receipt_cannot_grant_current_l5(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    monkeypatch.setenv("EIMEMORY_RUNTIME_COMMIT", "a" * 40)
    try:
        _append_deployment_receipt(runtime, scope_ref, commit="a" * 40)
        for index, case_id in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
            _append_acceptance(runtime, scope_ref, index=index, case_id=case_id, passed=True)
        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
    finally:
        runtime.close()

    assert metrics["sample_counts"]["verified_live_tasks"] == 10
    assert metrics["sample_counts"]["current_deployment_acceptance"] == 0


def test_runtime_commit_is_derived_from_immutable_release_venv_import(monkeypatch) -> None:
    commit = "d" * 40
    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    monkeypatch.setattr(
        capability_dashboard,
        "package_import_root",
        lambda: Path(f"/opt/eimemory/releases/{commit}/.venv/lib/python3.14/site-packages/eimemory"),
    )

    assert capability_dashboard._actual_runtime_commit() == (commit, True)


def test_runtime_commit_fails_closed_when_environment_and_import_release_disagree(monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_RUNTIME_COMMIT", "a" * 40)
    monkeypatch.setattr(
        capability_dashboard,
        "package_import_root",
        lambda: Path(f"/opt/eimemory/releases/{'b' * 40}/eimemory"),
    )

    assert capability_dashboard._actual_runtime_commit() == ("", True)


def test_noncanonical_releases_directory_cannot_define_production_runtime(monkeypatch) -> None:
    commit = "a" * 40
    monkeypatch.delenv("EIMEMORY_RUNTIME_COMMIT", raising=False)
    monkeypatch.setattr(
        capability_dashboard,
        "package_import_root",
        lambda: Path(f"/tmp/releases/{commit}/eimemory"),
    )

    assert capability_dashboard._actual_runtime_commit() == ("", False)


def _append_acceptance(
    runtime: Runtime,
    scope: ScopeRef,
    *,
    index: int,
    case_id: str,
    passed: bool,
    commit: str = "a" * 40,
) -> None:
    task_type = live_acceptance_task_type(case_id)
    observation_digest = f"{index:064x}"
    trace_id = f"live-acceptance:{commit}:{case_id}:{observation_digest[:12]}"
    receipts = [
        record
        for record in runtime.store.list_records(kinds=["promotion_request"], scope=scope, limit=20)
        if record.source == "eimemory.deployment_receipt" and str(record.meta.get("commit_sha") or "") == commit
    ]
    assert receipts
    evidence = runtime.store.append(
        RecordEnvelope.create(
            kind="learning_eval",
            title=f"Live acceptance {case_id}",
            summary="passed" if passed else "failed",
            scope=scope,
            source="eimemory.live_task_acceptance",
            status="active",
            content={
                "report_type": "live_task_acceptance_case",
                "schema_version": "live_task_acceptance.v1",
                "case_id": case_id,
                "task_type": task_type,
                "trace_id": trace_id,
                "passed": passed,
                "deployment_commit": commit,
                "deployment_version": "1.9.24",
                "release_path": "/opt/eimemory/releases/" + commit,
                "promotion_request_id": receipts[0].record_id,
                "observation_digest": observation_digest,
            },
            meta={
                "report_type": "live_task_acceptance_case",
                "schema_version": "live_task_acceptance.v1",
                "case_id": case_id,
                "task_type": task_type,
                "trace_id": trace_id,
                "passed": passed,
                "deployment_commit": commit,
                "promotion_request_id": receipts[0].record_id,
                "observation_digest": observation_digest,
            },
        )
    )
    result = runtime.record_outcome_trace(
        {
            "source": "eimemory.live_task_acceptance",
            "trace_id": trace_id,
            "task_type": task_type,
            "outcome": {"status": "success" if passed else "failed", "success": passed, "rehearsal": False},
            "verifier": {
                "passed": passed,
                "method": "eimemory.live_task_acceptance",
                "evidence_refs": [evidence.record_id],
            },
            "deployment_commit": commit,
            "deployment_version": "1.9.24",
            "release_path": "/opt/eimemory/releases/" + commit,
            "acceptance_case_id": case_id,
        },
        scope=SCOPE,
    )
    assert result["ok"] is True


def _append_deployment_receipt(runtime: Runtime, scope: ScopeRef, *, commit: str, created_at: str | None = None) -> str:
    release_path = f"/opt/eimemory/releases/{commit}"
    version = "1.9.24"
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
                "commit": commit,
                "version": version,
                "release_path": release_path,
                "import_root": f"{release_path}/eimemory",
                "package_tree_digest": runtime_package_tree_digest(),
                "checks": {"ready": True},
            },
            "commit": {"commit_sha": commit},
            "release": {"version": version, "release_path": release_path},
            "rollback_evidence": {"prior_commit_sha": "b" * 40, "rollback_command": "verified rollback"},
        },
    }
    record = RecordEnvelope.create(
        kind="promotion_request",
        title="Deployment receipt",
        summary="verified",
        scope=scope,
        source="eimemory.deployment_receipt",
        status="deployed",
        content=payload,
        meta={"report_type": "deployment_receipt", "commit_sha": commit, "version": version, "release_path": release_path, "gate_ok": True},
    )
    if created_at:
        record.time.created_at = created_at
        record.time.updated_at = created_at
    record = runtime.store.append(record)
    return record.record_id
