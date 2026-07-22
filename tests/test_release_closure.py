from __future__ import annotations

from copy import deepcopy

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.governance import closure_rehearsal as closure_rehearsal_module
from eimemory.governance import release_closure as release_closure_module
from eimemory.governance.release_closure import (
    _recall_result_allows_bootstrap_pending,
    run_release_closure,
)


SCOPE = {
    "agent_id": "release-closure",
    "workspace_id": "production",
    "user_id": "darrow",
    "tenant_id": "default",
}
REPO_ROOT = "/dev-project/eimemory"
CURRENT_LINK = "/opt/eimemory/current"
HEALTH_URL = "http://127.0.0.1:8091/health"
PRIOR_COMMIT = "a" * 40
CURRENT_COMMIT = "b" * 40


def test_runtime_exposes_release_closure(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: list[tuple[object, dict]] = []

    def fake_run(runtime_arg, **kwargs):
        calls.append((runtime_arg, kwargs))
        return {"ok": True, "closure_complete": True}

    monkeypatch.setattr(release_closure_module, "run_release_closure", fake_run)
    try:
        report = runtime.run_release_closure(**_identity_kwargs())
    finally:
        runtime.close()

    assert report["ok"] is True
    assert calls == [(runtime, _identity_kwargs())]


def test_runtime_exposes_weak_capability_replay_gate(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: list[tuple[object, dict]] = []

    def fake_run(runtime_arg, **kwargs):
        calls.append((runtime_arg, kwargs))
        return _successful_replay_bootstrap()

    monkeypatch.setattr(closure_rehearsal_module, "run_weak_capability_replay_gate", fake_run)
    kwargs = {"scope": SCOPE, "persist": True, "loop_id": "release_closure_bootstrap"}
    try:
        report = runtime.run_weak_capability_replay_gate(**kwargs)
    finally:
        runtime.close()

    assert report["ok"] is True
    assert calls == [(runtime, kwargs)]


@pytest.mark.parametrize(("ok", "expected_exit"), [(True, 0), (False, 1)])
def test_release_closure_cli_dispatches_scoped_gate(
    tmp_path,
    monkeypatch,
    capsys,
    ok: bool,
    expected_exit: int,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    calls: list[dict] = []

    def fake_run(_runtime, **kwargs):
        calls.append(kwargs)
        return {
            "ok": ok,
            "closure_complete": ok,
            "blocked_stage": "" if ok else "readiness",
            "blocked_reason": "" if ok else "readiness_not_l5",
        }

    monkeypatch.setattr(release_closure_module, "run_release_closure", fake_run)

    exit_code = cli_main(
        [
            "learn",
            "release-closure",
            "--repo-root",
            REPO_ROOT,
            "--current-link",
            CURRENT_LINK,
            "--health-url",
            HEALTH_URL,
            "--prior-commit",
            PRIOR_COMMIT,
            "--scope-agent",
            SCOPE["agent_id"],
            "--scope-workspace",
            SCOPE["workspace_id"],
            "--scope-user",
            SCOPE["user_id"],
        ]
    )
    output = __import__("json").loads(capsys.readouterr().out)

    assert exit_code == expected_exit
    assert output["ok"] is ok
    assert calls == [_identity_kwargs()]


class FakeRuntime:
    def __init__(
        self,
        *,
        receipt: dict | None = None,
        replay_bootstrap: dict | None = None,
        live_acceptance: dict | None = None,
        rehearsal: dict | None = None,
        readiness: dict | None = None,
        expect_bootstrap_pending: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.receipt = receipt or _successful_receipt()
        self.replay_bootstrap = replay_bootstrap or _successful_replay_bootstrap()
        self.live_acceptance = live_acceptance or _successful_live_acceptance()
        self.rehearsal = rehearsal or _successful_rehearsal()
        self.readiness = readiness or _successful_readiness()
        self.expect_bootstrap_pending = expect_bootstrap_pending
        self.store = type(
            "FakeStore",
            (),
            {"sqlite": type("FakeSQLite", (), {"pending_storage_migrations": lambda _self: []})()},
        )()

    @classmethod
    def successful(cls) -> "FakeRuntime":
        return cls()

    def verify_and_record_deployment(self, **kwargs) -> dict:
        self.calls.append("deployment_receipt")
        assert kwargs == _identity_kwargs()
        return deepcopy(self.receipt)

    def run_live_task_acceptance(self, **kwargs) -> dict:
        self.calls.append("live_acceptance")
        assert kwargs == _identity_kwargs()
        return deepcopy(self.live_acceptance)

    def run_configured_production_recall_gate(self, **kwargs) -> dict:
        self.calls.append("production_recall_run")
        assert kwargs == {"scope": SCOPE}
        return {"ok": True, "accepted": True, "gate_status": "accepted", "blocked_reason": ""}

    def verify_production_recall_gate(self, **kwargs) -> dict:
        self.calls.append("production_recall_verify")
        identity = kwargs.pop("release_identity")
        assert kwargs == {"scope": SCOPE, "limit": 500}
        assert identity.commit == CURRENT_COMMIT
        assert identity.version == "1.9.51"
        assert identity.receipt_id == "receipt-1"
        return {"ok": True, "status": "accepted", "record_id": "prg-current", "report_id": "prg-current"}

    def activate_production_recall_strict_state(self, **kwargs) -> dict:
        self.calls.append("production_recall_activate")
        identity = kwargs.pop("release_identity")
        assert kwargs == {"scope": SCOPE, "gate_record_id": "prg-current"}
        assert identity.commit == CURRENT_COMMIT
        return {
            "ok": True,
            "status": "strict_activated",
            "record_id": "prbs-strict-current",
            "candidate_commit": identity.commit,
            "gate_record_id": "prg-current",
        }

    def run_weak_capability_replay_gate(self, **kwargs) -> dict:
        self.calls.append("replay_bootstrap")
        assert kwargs == {
            "scope": SCOPE,
            "persist": True,
            "loop_id": "release_closure_bootstrap",
        }
        return deepcopy(self.replay_bootstrap)

    def run_l5_closure_rehearsal(self, **kwargs) -> dict:
        self.calls.append("closure_rehearsal")
        expected = {
            "scope": SCOPE,
            "persist": True,
            "replay_bootstrap": self.replay_bootstrap,
        }
        if self.expect_bootstrap_pending:
            assert "bootstrap_pending" in kwargs
            assert "release_identity" in kwargs
        if "bootstrap_pending" in kwargs or "release_identity" in kwargs:
            pending = kwargs.pop("bootstrap_pending")
            identity = kwargs.pop("release_identity")
            assert pending["status"] == "bootstrap_data_pending"
            assert identity.commit == CURRENT_COMMIT
            assert identity.version == "1.9.51"
            assert identity.receipt_id == "receipt-1"
        assert kwargs == expected
        return deepcopy(self.rehearsal)

    def build_l5_readiness_report(self, **kwargs) -> dict:
        self.calls.append("readiness")
        assert kwargs == {
            "scope": SCOPE,
            "persist": True,
            "limit": 1000,
            "loop_id": "release_closure",
        }
        return deepcopy(self.readiness)


def test_release_closure_runs_all_stages_in_order() -> None:
    runtime = FakeRuntime.successful()

    report = _run(runtime)

    assert runtime.calls == [
        "deployment_receipt",
        "production_recall_run",
        "production_recall_verify",
        "production_recall_activate",
        "replay_bootstrap",
        "live_acceptance",
        "closure_rehearsal",
        "readiness",
    ]
    assert report["ok"] is True
    assert report["closure_complete"] is True
    assert report["blocked_stage"] == ""
    assert report["blocked_reason"] == ""
    assert report["deployment"] == {
        "commit": CURRENT_COMMIT,
        "version": "1.9.51",
        "release_path": f"/opt/eimemory/releases/{CURRENT_COMMIT}",
        "promotion_request_id": "receipt-1",
    }
    assert report["record_ids"] == {
        "deployment_receipt": "receipt-1",
        "production_recall_gate": "prg-current",
        "production_recall_strict_state": "prbs-strict-current",
        "readiness": "readiness-1",
    }


def test_release_closure_blocks_before_recall_while_storage_migrations_are_pending() -> None:
    runtime = FakeRuntime.successful()
    runtime.store.sqlite.pending_storage_migrations = lambda: ["records.payload_archive.v1"]

    report = _run(runtime)

    assert report["ok"] is False
    assert report["blocked_stage"] == "storage_migrations"
    assert report["storage_migrations"]["pending"] == ["records.payload_archive.v1"]
    assert runtime.calls == ["deployment_receipt"]


class ProductionGateRuntime(FakeRuntime):
    def __init__(self, *, accepted: bool = True) -> None:
        super().__init__()
        self.accepted = accepted

    def run_configured_production_recall_gate(self, **kwargs) -> dict:
        self.calls.append("production_recall_run")
        assert kwargs == {"scope": SCOPE}
        return {
            "ok": self.accepted,
            "accepted": self.accepted,
            "gate_status": "accepted" if self.accepted else "not_run",
            "blocked_reason": "" if self.accepted else "eligible_dataset_missing",
        }

    def verify_production_recall_gate(self, **kwargs) -> dict:
        self.calls.append("production_recall_verify")
        identity = kwargs.pop("release_identity")
        assert kwargs == {"scope": SCOPE, "limit": 500}
        assert identity.commit == CURRENT_COMMIT
        assert identity.version == "1.9.51"
        assert identity.receipt_id == "receipt-1"
        assert identity.session_id == "receipt-1"
        return {
            "ok": True,
            "status": "accepted",
            "record_id": "prg-current",
            "report_id": "prg-current",
        }


def test_release_closure_runs_production_recall_after_receipt_before_replay() -> None:
    runtime = ProductionGateRuntime()

    report = _run(runtime)

    assert runtime.calls == [
        "deployment_receipt",
        "production_recall_run",
        "production_recall_verify",
        "production_recall_activate",
        "replay_bootstrap",
        "live_acceptance",
        "closure_rehearsal",
        "readiness",
    ]
    assert report["production_recall_gate"]["ok"] is True
    assert report["record_ids"]["production_recall_gate"] == "prg-current"
    assert report["production_recall_strict_state"]["status"] == "strict_activated"
    assert report["record_ids"]["production_recall_strict_state"] == "prbs-strict-current"


def test_release_closure_fails_closed_before_replay_when_strict_activation_fails() -> None:
    runtime = ProductionGateRuntime()
    runtime.activate_production_recall_strict_state = lambda **_kwargs: {
        "ok": False,
        "status": "blocked",
        "reason": "strict_gate_record_mismatch",
        "record_id": "",
    }

    report = _run(runtime)

    assert runtime.calls == [
        "deployment_receipt",
        "production_recall_run",
        "production_recall_verify",
    ]
    assert report["ok"] is False
    assert report["blocked_stage"] == "production_recall_strict_state"
    assert report["blocked_reason"] == "strict_gate_record_mismatch"
    assert report["replay_bootstrap"]["status"] == "not_run"


def test_release_closure_fails_closed_before_replay_when_production_dataset_not_run() -> None:
    runtime = ProductionGateRuntime(accepted=False)

    report = _run(runtime)

    assert runtime.calls == ["deployment_receipt", "production_recall_run"]
    assert report["ok"] is False
    assert report["blocked_stage"] == "production_recall_gate"
    assert report["blocked_reason"] == "eligible_dataset_missing"


def test_release_closure_never_masks_cross_channel_leakage_with_bootstrap_pending(monkeypatch) -> None:
    runtime = ProductionGateRuntime(accepted=False)
    runtime.run_configured_production_recall_gate = lambda **_kwargs: {
        "ok": False,
        "accepted": False,
        "gate_status": "blocked",
        "blocked_reason": "production_recall_gate_failed",
        "cross_channel_leakage_count": 1,
        "threshold_gate": {
            "ok": False,
            "blocked_reason": "production_recall_gate_failed",
            "blocking_metrics": {
                "cross_channel_leakage_count": {"actual": 1, "threshold": 0, "operator": "=="}
            },
        },
    }
    monkeypatch.setattr(
        "eimemory.evaluation.real_query_gate.verify_current_bootstrap_data_pending",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("security failures must not enter the pending verifier")
        ),
    )

    report = _run(runtime)

    assert report["ok"] is False
    assert report["blocked_stage"] == "production_recall_gate"
    assert report["blocked_reason"] == "production_recall_gate_failed"
    assert report["replay_bootstrap"]["status"] == "not_run"


def test_release_closure_allows_real_passing_diagnostic_only_as_bootstrap_input(tmp_path) -> None:
    report = _passing_diagnostic_recall_report(tmp_path)

    assert report["ok"] is True
    assert report["accepted"] is False
    assert report["gate_status"] == "diagnostic"
    assert report["dataset_kind"] == "diagnostic"
    assert report["quality_gate"]["ok"] is True
    assert _recall_result_allows_bootstrap_pending(report) is True


def test_release_closure_routes_real_passing_diagnostic_to_release_bound_pending_verifier(
    tmp_path,
    monkeypatch,
) -> None:
    diagnostic = _passing_diagnostic_recall_report(tmp_path)
    runtime = ProductionGateRuntime(accepted=False)
    runtime.run_configured_production_recall_gate = lambda **_kwargs: diagnostic
    calls: list[bool] = []

    def pending_verifier(*_args, **_kwargs):
        calls.append(True)
        return {
            "ok": False,
            "status": "blocked",
            "reason": "diagnostic_reached_release_bound_pending_verifier",
            "record_id": "",
        }

    monkeypatch.setattr(
        "eimemory.evaluation.real_query_gate.verify_current_bootstrap_data_pending",
        pending_verifier,
    )

    report = _run(runtime)

    assert report["ok"] is False
    assert report["blocked_stage"] == "production_recall_gate"
    assert report["blocked_reason"] == "production_recall_gate_failed"
    assert calls == [True]


def test_release_closure_rejects_every_incomplete_or_failed_diagnostic_contract(tmp_path) -> None:
    report = _passing_diagnostic_recall_report(tmp_path)
    mutations = [
        (("quality_gate", "ok"), False),
        (("quality_gate", "blocking_metrics"), {"hit_at_1": {"actual": 0.0}}),
        (("errors",), [{"error": "seed_failed"}]),
        (("seed_error_count",), 1),
        (("false_recall_rate",), 0.01),
        (("forbidden_hit_rate",), 0.01),
        (("gate_ok",), False),
        (("passed_threshold",), False),
        (("blocked_reason",), "recall_quality_gate_failed"),
    ]
    for path, value in mutations:
        changed = deepcopy(report)
        target = changed
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        assert _recall_result_allows_bootstrap_pending(changed) is False, path
    for required in (
        "quality_gate",
        "errors",
        "seed_error_count",
        "false_recall_rate",
        "forbidden_hit_rate",
        "gate_ok",
        "passed_threshold",
        "dataset_kind",
    ):
        changed = deepcopy(report)
        changed.pop(required)
        assert _recall_result_allows_bootstrap_pending(changed) is False, required


def test_release_closure_allows_only_verified_bootstrap_data_pending_and_keeps_l5_downgraded(monkeypatch) -> None:
    runtime = ProductionGateRuntime(accepted=False)
    runtime.expect_bootstrap_pending = True
    runtime.rehearsal = {
        **runtime.rehearsal,
        "closure_complete": False,
        "data_accumulating": True,
    }
    runtime.readiness = {
        **runtime.readiness,
        "schema_version": "l5_readiness.v2",
        "release_identity": {
            "release_commit": CURRENT_COMMIT,
            "release_version": "1.9.51",
            "deployment_receipt_id": "receipt-1",
            "release_session_id": "receipt-1",
        },
        "current_stage": "L4.5",
        "readiness_score": 0.8,
        "production_recall_gate": {
            "ok": False,
            "status": "not_run",
            "reason": "current_release_production_recall_report_missing",
            "record_id": "",
        },
        "production_recall_strict_state": {
            "ok": False,
            "status": "not_run",
            "reason": "strict_state_missing",
            "record_id": "bootstrap-pending-current",
        },
        "verified_replay": {
            **runtime.readiness["verified_replay"],
            "pass_rate": 1.0,
        },
        "verified_core_replay": {
            **runtime.readiness["verified_core_replay"],
            "pass_count": 15,
            "fail_count": 0,
            "pass_rate": 1.0,
        },
    }
    monkeypatch.setattr(
        "eimemory.evaluation.real_query_gate.verify_current_bootstrap_data_pending",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": "bootstrap_data_pending",
            "reason": "production_dataset_not_ready",
            "record_id": "bootstrap-pending-current",
            "progress": {"case_count": 2, "required_case_count": 15},
            "release_identity": {
                "release_commit": CURRENT_COMMIT,
                "release_version": "1.9.51",
                "deployment_receipt_id": "receipt-1",
                "release_session_id": "receipt-1",
            },
        },
    )

    report = _run(runtime)

    assert report["ok"] is True
    assert report["closure_complete"] is False
    assert report["data_accumulating"] is True
    assert report["production_recall_gate"]["status"] == "data_accumulating"
    assert report["record_ids"]["production_recall_bootstrap"] == "bootstrap-pending-current"
    assert "production_recall_verify" not in runtime.calls
    assert runtime.calls[-1] == "readiness"


def test_release_closure_fails_closed_when_production_gate_runner_is_unavailable() -> None:
    class ReceiptOnly:
        store = type(
            "FakeStore",
            (),
            {"sqlite": type("FakeSQLite", (), {"pending_storage_migrations": lambda _self: []})()},
        )()

        def verify_and_record_deployment(self, **_kwargs) -> dict:
            return _successful_receipt()

    report = _run(ReceiptOnly())

    assert report["ok"] is False
    assert report["blocked_stage"] == "production_recall_gate"
    assert report["blocked_reason"] == "production_recall_gate_runner_unavailable"


@pytest.mark.parametrize(
    ("stage", "runtime_kwargs", "expected_calls", "reason"),
    [
        (
            "deployment_receipt",
            {"receipt": {"ok": False, "error": "health_commit_mismatch"}},
            ["deployment_receipt"],
            "health_commit_mismatch",
        ),
        (
            "replay_bootstrap",
            {"replay_bootstrap": {"ok": False, "blocked_reasons": ["weak_capability_replay_failed"]}},
            ["deployment_receipt", "production_recall_run", "production_recall_verify", "production_recall_activate", "replay_bootstrap"],
            "weak_capability_replay_failed",
        ),
        (
            "live_acceptance",
            {"live_acceptance": {"ok": False, "error": "acceptance_case_failed"}},
            ["deployment_receipt", "production_recall_run", "production_recall_verify", "production_recall_activate", "replay_bootstrap", "live_acceptance"],
            "acceptance_case_failed",
        ),
        (
            "closure_rehearsal",
            {"rehearsal": {"ok": False, "closure_complete": False, "blocked_reasons": ["replay_failed"]}},
            ["deployment_receipt", "production_recall_run", "production_recall_verify", "production_recall_activate", "replay_bootstrap", "live_acceptance", "closure_rehearsal"],
            "replay_failed",
        ),
        (
            "readiness",
            {"readiness_score": 0.9},
            ["deployment_receipt", "production_recall_run", "production_recall_verify", "production_recall_activate", "replay_bootstrap", "live_acceptance", "closure_rehearsal", "readiness"],
            "readiness_not_l5",
        ),
    ],
)
def test_release_closure_stops_at_first_failed_stage(
    stage: str,
    runtime_kwargs: dict,
    expected_calls: list[str],
    reason: str,
) -> None:
    if stage == "readiness":
        runtime_kwargs = {"readiness": {**_successful_readiness(), **runtime_kwargs}}
    runtime = FakeRuntime(**runtime_kwargs)
    report = _run(runtime)

    assert runtime.calls == expected_calls
    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["blocked_stage"] == stage
    assert report["blocked_reason"] == reason


@pytest.mark.parametrize(
    "readiness_patch",
    [
        {"latest_l5_assessment": {"complete": False}},
        {"live_task_gate": {"ok": False, "current_deployment_verified_real_tasks": 10}},
        {"live_task_gate": {"ok": True, "current_deployment_verified_real_tasks": 9}},
        {"verified_replay": {"weak_capabilities_missing": ["device.control"]}},
    ],
)
def test_release_closure_requires_every_final_readiness_gate(readiness_patch: dict) -> None:
    readiness = {**_successful_readiness(), **readiness_patch}
    report = _run(FakeRuntime(readiness=readiness))

    assert report["ok"] is False
    assert report["blocked_stage"] == "readiness"
    assert report["blocked_reason"] == "readiness_not_l5"


def test_release_closure_rejects_live_deficit_without_release_bound_bootstrap() -> None:
    readiness = {
        **_successful_readiness(),
        "current_stage": "L4.5",
        "readiness_score": 0.8,
        "live_task_gate": {
            "ok": False,
            "sample_deficit": 10,
            "task_type_deficit": 5,
            "current_deployment_verified_real_tasks": 0,
            "current_deployment_operational_probes": 10,
        },
    }

    report = _run(FakeRuntime(readiness=readiness))

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["data_accumulating"] is False
    assert report["blocked_stage"] == "readiness"
    assert report["blocked_reason"] == "readiness_not_l5"


def test_release_closure_rejects_unbound_accumulating_rehearsal_before_final_readiness() -> None:
    readiness = {
        **_successful_readiness(),
        "current_stage": "L4.5",
        "readiness_score": 0.8,
        "live_task_gate": {
            "ok": False,
            "sample_deficit": 10,
            "task_type_deficit": 5,
            "current_deployment_verified_real_tasks": 0,
            "current_deployment_operational_probes": 10,
        },
    }
    rehearsal = {
        **_successful_rehearsal(),
        "closure_complete": False,
        "data_accumulating": True,
    }

    report = _run(FakeRuntime(rehearsal=rehearsal, readiness=readiness))

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["data_accumulating"] is False
    assert report["blocked_stage"] == "readiness"
    assert report["blocked_reason"] == "readiness_not_l5"


def test_release_closure_rejects_task_type_only_deficit_without_bootstrap() -> None:
    readiness = {
        **_successful_readiness(),
        "current_stage": "L4.5",
        "readiness_score": 0.8,
        "live_task_gate": {
            "ok": False,
            "sample_deficit": 0,
            "task_type_deficit": 2,
            "current_deployment_verified_real_tasks": 10,
            "current_deployment_operational_probes": 10,
        },
    }

    report = _run(FakeRuntime(readiness=readiness))

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["data_accumulating"] is False
    assert report["blocked_stage"] == "readiness"
    assert report["blocked_reason"] == "readiness_not_l5"


def _run(runtime: FakeRuntime) -> dict:
    return run_release_closure(
        runtime,
        scope=SCOPE,
        repo_root=REPO_ROOT,
        current_link=CURRENT_LINK,
        health_url=HEALTH_URL,
        prior_commit=PRIOR_COMMIT,
    )


def _passing_diagnostic_recall_report(tmp_path) -> dict:
    runtime = Runtime.create(root=tmp_path / "diagnostic-runtime")
    dataset = {
        "name": "release-bootstrap-diagnostic",
        "scope": SCOPE,
        "seed": [
            {
                "id": "deployment-receipt-memory",
                "kind": "memory",
                "title": "Deployment receipt rollback evidence",
                "text": "Deployment receipt rollback evidence keeps the immutable release safe.",
                "memory_type": "fact",
            }
        ],
        "cases": [
            {
                "case_id": "deployment-receipt-recall",
                "query": "deployment receipt rollback evidence",
                "expected_record_ids": ["deployment-receipt-memory"],
                "expected_titles": ["Deployment receipt rollback evidence"],
                "topk": 5,
                "scope": SCOPE,
            }
        ],
    }
    try:
        return run_production_recall_eval(runtime, dataset)
    finally:
        runtime.close()


def _identity_kwargs() -> dict:
    return {
        "scope": SCOPE,
        "repo_root": REPO_ROOT,
        "current_link": CURRENT_LINK,
        "health_url": HEALTH_URL,
        "prior_commit": PRIOR_COMMIT,
    }


def _successful_receipt() -> dict:
    return {
        "ok": True,
        "report_type": "deployment_receipt",
        "commit": CURRENT_COMMIT,
        "version": "1.9.51",
        "release_path": f"/opt/eimemory/releases/{CURRENT_COMMIT}",
        "promotion_request_id": "receipt-1",
        "release_session_id": "receipt-1",
    }


def _successful_live_acceptance() -> dict:
    return {
        "ok": True,
        "case_count": 10,
        "pass_count": 10,
        "fail_count": 0,
        "distinct_task_types": 10,
        "reused_count": 0,
        "deployment": {
            "commit": CURRENT_COMMIT,
            "version": "1.9.51",
            "release_path": f"/opt/eimemory/releases/{CURRENT_COMMIT}",
            "promotion_request_id": "receipt-1",
        },
    }


def _successful_replay_bootstrap() -> dict:
    return {
        "ok": True,
        "capability_acceptance": {"ok": True, "execution_id": "acceptance-1"},
        "weak_capability_replay": {
            "ok": True,
            "manifest_record_id": "manifest-1",
        },
        "replay_gate": {"ok": True, "blocked_reasons": []},
        "blocked_reasons": [],
    }


def _successful_rehearsal() -> dict:
    return {
        "ok": True,
        "closure_complete": True,
        "blocked_reasons": [],
        "weak_capability_replay": {"manifest_record_id": "manifest-1"},
    }


def _successful_readiness() -> dict:
    return {
        "ok": True,
        "schema_version": "l5_readiness.v2",
        "release_identity": {"release_commit": CURRENT_COMMIT},
        "production_recall_gate": {"ok": True, "status": "accepted"},
        "production_recall_strict_state": {
            "ok": True,
            "status": "strict_activated",
            "candidate_commit": CURRENT_COMMIT,
            "record_id": "prbs-strict-current",
        },
        "storage_migrations": {"ok": True, "status": "ready", "pending": []},
        "capability_gaps": [],
        "current_stage": "L5",
        "readiness_score": 1.0,
        "latest_l5_assessment": {
            "trusted": True,
            "complete": True,
            "level": "L5",
            "record_id": "assessment-1",
        },
        "live_task_gate": {
            "ok": True,
            "current_deployment_verified_real_tasks": 10,
            "current_deployment_operational_probes": 10,
        },
        "verified_replay": {
            "weak_capabilities_missing": [],
            "manifest_rejection_reasons": {},
            "executed_count": 12,
            "pass_count": 12,
            "fail_count": 0,
        },
        "verified_core_replay": {
            "executed_count": 15,
            "core_capabilities_missing": [],
            "manifest_rejection_reasons": {},
        },
        "persisted_record_id": "readiness-1",
    }
