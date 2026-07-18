from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance import live_task_acceptance
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.runtime_identity import runtime_package_tree_digest


SCOPE = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"}


def test_live_task_acceptance_records_ten_current_deployment_tasks_idempotently(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = "a" * 40
    receipt_id = _seed_deployment_receipt(runtime, commit="a" * 40)
    identity = {
        "ok": True,
        "commit": "a" * 40,
        "version": "1.9.24",
        "release_path": "/opt/eimemory/releases/" + "a" * 40,
        "promotion_request_id": receipt_id,
    }
    monkeypatch.setattr(live_task_acceptance, "_verified_deployment_identity", lambda *args, **kwargs: identity)
    monkeypatch.setattr(
        live_task_acceptance,
        "_case_definitions",
        lambda *args, **kwargs: [
            {
                "case_id": case_id,
                "task_type": live_task_acceptance.live_acceptance_task_type(case_id),
                "check": lambda index=index: {"passed": True, "sample_count": index},
            }
            for index, case_id in enumerate(live_task_acceptance.LIVE_ACCEPTANCE_CASE_IDS)
        ],
    )
    try:
        first = live_task_acceptance.run_live_task_acceptance(
            runtime,
            scope=SCOPE,
            repo_root="/dev-project/eimemory",
            current_link="/opt/eimemory/current",
            health_url="http://127.0.0.1:8091/health",
            prior_commit="b" * 40,
        )
        first_counts = {
            kind: len(runtime.store.list_records(kinds=[kind], scope=SCOPE, limit=100))
            for kind in ("learning_eval", "reflection")
        }
        second = live_task_acceptance.run_live_task_acceptance(
            runtime,
            scope=SCOPE,
            repo_root="/dev-project/eimemory",
            current_link="/opt/eimemory/current",
            health_url="http://127.0.0.1:8091/health",
            prior_commit="b" * 40,
        )
        second_counts = {
            kind: len(runtime.store.list_records(kinds=[kind], scope=SCOPE, limit=100))
            for kind in ("learning_eval", "reflection")
        }
        metrics = runtime.build_capability_dashboard_metrics(scope=SCOPE, persist=False)
        acceptance_records = runtime.store.list_records(kinds=["learning_eval"], scope=SCOPE, limit=20)
    finally:
        runtime.close()

    assert first["ok"] is True
    assert first["case_count"] == first["pass_count"] == 10
    assert first["distinct_task_types"] == 10
    assert first["reused_count"] == 0
    assert second["ok"] is True
    assert second["reused_count"] == 10
    assert first_counts == second_counts == {"learning_eval": 10, "reflection": 10}
    assert metrics["metrics"]["verified_live_task_success_rate"] == 1.0
    assert metrics["sample_counts"]["verified_live_tasks"] == 10
    assert metrics["sample_counts"]["current_deployment_acceptance"] == 10
    assert metrics["sample_counts"]["current_deployment_operational_probes"] == 10
    assert metrics["sample_counts"]["current_deployment_verified_real_tasks"] == 0
    assert metrics["metrics"]["current_deployment_verified_real_task_success_rate"] == 0.0
    assert all(record.content["evidence_class"] == "operational_probe" for record in acceptance_records)


def test_live_task_acceptance_fails_closed_before_running_cases_when_identity_is_invalid(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setattr(
        live_task_acceptance,
        "_verified_deployment_identity",
        lambda *args, **kwargs: {"ok": False, "error": "health_commit_mismatch"},
    )
    called = False

    def cases(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(live_task_acceptance, "_case_definitions", cases)
    try:
        report = live_task_acceptance.run_live_task_acceptance(
            runtime,
            scope=SCOPE,
            repo_root="/dev-project/eimemory",
            current_link="/opt/eimemory/current",
            health_url="http://127.0.0.1:8091/health",
            prior_commit="b" * 40,
        )
    finally:
        runtime.close()

    assert report == {
        "ok": False,
        "report_type": "live_task_acceptance",
        "error": "health_commit_mismatch",
        "case_count": 0,
        "pass_count": 0,
        "cases": [],
    }
    assert called is False


def test_live_task_acceptance_cli_runs_scoped_gate(tmp_path, monkeypatch, capsys) -> None:
    runtime = Runtime.create(root=tmp_path)
    receipt_id = _seed_deployment_receipt(runtime, commit="a" * 40)
    runtime.close()
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    monkeypatch.setattr(
        live_task_acceptance,
        "_verified_deployment_identity",
        lambda *args, **kwargs: {
            "ok": True,
            "commit": "a" * 40,
            "version": "1.9.24",
            "release_path": "/opt/eimemory/releases/" + "a" * 40,
            "promotion_request_id": receipt_id,
        },
    )
    monkeypatch.setattr(
        live_task_acceptance,
        "_case_definitions",
        lambda *args, **kwargs: [
            {"case_id": case_id, "task_type": live_task_acceptance.live_acceptance_task_type(case_id), "check": lambda: {"passed": True}}
            for case_id in live_task_acceptance.LIVE_ACCEPTANCE_CASE_IDS
        ],
    )

    exit_code = cli_main(
        [
            "learn",
            "live-acceptance",
            "--repo-root",
            "/dev-project/eimemory",
            "--current-link",
            "/opt/eimemory/current",
            "--health-url",
            "http://127.0.0.1:8091/health",
            "--prior-commit",
            "b" * 40,
            "--scope-agent",
            "hongtu",
            "--scope-workspace",
            "embodied",
            "--scope-user",
            "darrow",
        ]
    )
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["ok"] is True
    assert output["scope"]["agent_id"] == "hongtu"
    assert output["case_count"] == output["pass_count"] == 10


def test_live_task_acceptance_fails_when_outcome_trace_is_not_persisted(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    receipt_id = _seed_deployment_receipt(runtime, commit="a" * 40)
    monkeypatch.setattr(
        live_task_acceptance,
        "_verified_deployment_identity",
        lambda *args, **kwargs: {
            "ok": True,
            "commit": "a" * 40,
            "version": "1.9.24",
            "release_path": "/opt/eimemory/releases/" + "a" * 40,
            "promotion_request_id": receipt_id,
        },
    )
    monkeypatch.setattr(
        live_task_acceptance,
        "_case_definitions",
        lambda *args, **kwargs: [
            {"case_id": case_id, "task_type": live_task_acceptance.live_acceptance_task_type(case_id), "check": lambda: {"passed": True}}
            for case_id in live_task_acceptance.LIVE_ACCEPTANCE_CASE_IDS
        ],
    )
    monkeypatch.setattr(runtime, "record_outcome_trace", lambda *args, **kwargs: {"ok": False, "error": "write_failed"})
    try:
        report = live_task_acceptance.run_live_task_acceptance(
            runtime,
            scope=SCOPE,
            repo_root="/dev-project/eimemory",
            current_link="/opt/eimemory/current",
            health_url="http://127.0.0.1:8091/health",
            prior_commit="b" * 40,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["pass_count"] == 0
    assert all(case["trace_persisted"] is False for case in report["cases"])


def test_readiness_pure_read_ignores_concurrent_external_records(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    concurrent_runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)

    def readiness(*args, **kwargs):
        concurrent_runtime.store.append(
            RecordEnvelope.create(
                kind="reflection",
                title="Concurrent trace",
                summary="written by another runtime",
                scope=scope_ref,
                source="test.concurrent",
            )
        )
        return {"ok": True, "current_stage": "L5"}

    monkeypatch.setattr(runtime, "build_l5_readiness_report", readiness)
    try:
        definitions = live_task_acceptance._case_definitions(
            runtime,
            scope=scope_ref,
            identity={"commit": "a" * 40, "version": "1.9.52", "release_path": "/tmp/release"},
        )
        check = next(item["check"] for item in definitions if item["case_id"] == "governance.readiness_pure_read")
        observation = check()
    finally:
        concurrent_runtime.close()
        runtime.close()

    assert observation["passed"] is True


def test_readiness_pure_read_rejects_writes_from_current_runtime(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)

    def readiness(*args, **kwargs):
        runtime.store.append(
            RecordEnvelope.create(
                kind="reflection",
                title="Unexpected readiness write",
                summary="must fail the pure-read case",
                scope=scope_ref,
                source="test.current_runtime",
            )
        )
        return {"ok": True, "current_stage": "L5"}

    monkeypatch.setattr(runtime, "build_l5_readiness_report", readiness)
    try:
        definitions = live_task_acceptance._case_definitions(
            runtime,
            scope=scope_ref,
            identity={"commit": "a" * 40, "version": "1.9.52", "release_path": "/tmp/release"},
        )
        check = next(item["check"] for item in definitions if item["case_id"] == "governance.readiness_pure_read")
        observation = check()
    finally:
        runtime.close()

    assert observation["passed"] is False


def _seed_deployment_receipt(runtime: Runtime, *, commit: str) -> str:
    release_path = f"/opt/eimemory/releases/{commit}"
    version = "1.9.24"
    payload = {
        "report_type": "deployment_receipt",
        "candidate_id": f"deployment:{commit}",
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
            "commit": {"ok": True, "commit_sha": commit},
            "release": {"version": version, "release_path": release_path},
            "rollback_evidence": {"prior_commit_sha": "b" * 40, "rollback_command": "verified rollback"},
        },
    }
    record = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Deployment receipt",
            summary="verified",
            scope=ScopeRef.from_dict(SCOPE),
            source="eimemory.deployment_receipt",
            status="deployed",
            content=payload,
            meta={"report_type": "deployment_receipt", "commit_sha": commit, "version": version, "release_path": release_path, "gate_ok": True},
        )
    )
    return record.record_id
