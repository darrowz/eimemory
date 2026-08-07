from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.governance import closure_rehearsal as closure_rehearsal_module
from eimemory.governance import release_closure as release_closure_module
from eimemory.governance import release_closure_pending as pending_module
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.governance.release_closure import (
    _recall_result_allows_bootstrap_pending,
    run_release_closure,
)
from eimemory.governance.release_closure_pending import (
    reconcile_release_closure_pending,
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


def test_runtime_exposes_release_closure_reconcile(tmp_path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    calls: list[object] = []

    def fake_reconcile(runtime_arg):
        calls.append(runtime_arg)
        return {"ok": True, "status": "no_pending"}

    monkeypatch.setattr(
        pending_module,
        "reconcile_release_closure_pending",
        fake_reconcile,
    )
    try:
        report = runtime.reconcile_release_closure()
    finally:
        runtime.close()

    assert report == {"ok": True, "status": "no_pending"}
    assert calls == [runtime]


def test_release_closure_reconcile_cli_dispatches_runtime(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    calls: list[object] = []

    def fake_reconcile(runtime_arg):
        calls.append(runtime_arg)
        return {"ok": True, "status": "waiting_for_channel_acceptance"}

    monkeypatch.setattr(
        pending_module,
        "reconcile_release_closure_pending",
        fake_reconcile,
    )

    exit_code = cli_main(["learn", "release-closure-reconcile"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {"ok": True, "status": "waiting_for_channel_acceptance"}
    assert len(calls) == 1


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
        channel_acceptance: dict | None = None,
        rehearsal: dict | None = None,
        readiness: dict | None = None,
        expect_bootstrap_pending: bool = False,
    ) -> None:
        self.calls: list[str] = []
        self.receipt = receipt or _successful_receipt()
        self.replay_bootstrap = replay_bootstrap or _successful_replay_bootstrap()
        self.live_acceptance = live_acceptance or _successful_live_acceptance()
        self.channel_acceptance = channel_acceptance or {
            "ok": True,
            "record_id": "channel-current",
        }
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

    def record_openclaw_channel_acceptance(self, **kwargs) -> dict:
        self.calls.append("channel_acceptance")
        assert kwargs == {
            "scope": SCOPE,
            "current_release": self.current_release_identity(
                scope=SCOPE,
                limit=500,
            ),
        }
        return deepcopy(self.channel_acceptance)

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
            "repo_root": REPO_ROOT,
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

    def current_release_identity(self, **kwargs) -> ReleaseIdentity:
        assert kwargs == {"scope": SCOPE, "limit": 500}
        return ReleaseIdentity(CURRENT_COMMIT, "1.9.51", "receipt-1", "receipt-1")

    def current_release_lineage(self, **kwargs) -> dict:
        assert kwargs["scope"] == SCOPE
        assert kwargs["current_release"] == self.current_release_identity(
            scope=SCOPE,
            limit=500,
        )
        return deepcopy(self.readiness["release_lineage"])

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
        "channel_acceptance",
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
        "channel_acceptance": "channel-current",
        "readiness": "readiness-1",
    }


def test_release_closure_finalizes_exact_lineage_inside_single_rehearsal(
    monkeypatch,
) -> None:
    runtime = FakeRuntime.successful()
    runtime.live_acceptance["cases"] = [
        {"case_id": case_id, "record_id": f"live-case-{index}"}
        for index, case_id in enumerate(release_closure_module.LIVE_ACCEPTANCE_CASE_IDS)
    ]
    captured: dict = {}

    def record_release_lineage(**kwargs) -> dict:
        runtime.calls.append("record_release_lineage")
        captured.update(kwargs)
        return {
            "ok": True,
            "validated": True,
            "compatible": True,
            "record_id": "lineage-final",
        }

    def current_release_lineage(**kwargs) -> dict:
        runtime.calls.append("current_release_lineage")
        assert kwargs["repo_root"] == REPO_ROOT
        return {
            "ok": True,
            "validated": True,
            "compatible": True,
            "record_id": "lineage-final",
        }

    def run_rehearsal(**kwargs) -> dict:
        runtime.calls.append("closure_rehearsal")
        finalizer = kwargs.pop("release_lineage_finalizer")
        assert kwargs == {
            "scope": SCOPE,
            "persist": True,
            "replay_bootstrap": runtime.replay_bootstrap,
            "repo_root": REPO_ROOT,
        }
        lineage = finalizer({"ok": True, "manifest_record_id": "core-manifest"})
        return {
            **runtime.rehearsal,
            "release_lineage": lineage,
            "l5_readiness": runtime.readiness,
        }

    runtime.record_release_lineage = record_release_lineage
    runtime.current_release_lineage = current_release_lineage
    runtime.run_l5_closure_rehearsal = run_rehearsal
    monkeypatch.setattr(
        release_closure_module,
        "readiness_gate_status",
        lambda _report, **kwargs: (
            "L5"
            if kwargs
            == {
                "runtime": runtime,
                "scope": SCOPE,
                "repo_root": REPO_ROOT,
            }
            else ""
        ),
    )

    report = _run(runtime)

    assert report["ok"] is True
    assert runtime.calls == [
        "deployment_receipt",
        "production_recall_run",
        "production_recall_verify",
        "production_recall_activate",
        "replay_bootstrap",
        "live_acceptance",
        "channel_acceptance",
        "closure_rehearsal",
        "record_release_lineage",
        "current_release_lineage",
    ]
    assert captured["gate_evidence"] == {
        "memory.recall": ["prg-current", "prbs-strict-current"],
        "memory.governance": ["manifest-1", "core-manifest"],
        "channel.openclaw": ["channel-current"],
        "storage.integrity": [f"live-case-{index}" for index in range(10)],
        "deployment.runtime": ["receipt-1"],
    }
    assert report["readiness"] == runtime.readiness
    assert report["record_ids"]["release_lineage"] == "lineage-final"


def test_release_closure_finalizes_pending_recall_lineage_with_bootstrap_and_core_manifest() -> None:
    captured: dict = {}

    class PendingLineageRuntime:
        def record_release_lineage(self, **kwargs) -> dict:
            captured.update(kwargs)
            return {
                "ok": True,
                "validated": True,
                "compatible": True,
                "record_id": "lineage-pending",
            }

        def current_release_lineage(self, **_kwargs) -> dict:
            return {
                "ok": True,
                "validated": True,
                "compatible": True,
                "record_id": "lineage-pending",
            }

    release = ReleaseIdentity(
        commit=CURRENT_COMMIT,
        version="1.9.51",
        receipt_id="receipt-1",
        session_id="receipt-1",
    )
    live_acceptance = {
        "cases": [
            {"case_id": case_id, "record_id": f"live-case-{index}"}
            for index, case_id in enumerate(release_closure_module.LIVE_ACCEPTANCE_CASE_IDS)
        ]
    }

    report = release_closure_module._finalize_release_lineage(
        PendingLineageRuntime(),
        scope=SCOPE,
        repo_root=REPO_ROOT,
        current_release=release,
        receipt_record_id="receipt-1",
        recall_gate_record_id="",
        strict_state_record_id="",
        bootstrap_pending_record_id="bootstrap-pending-current",
        channel_acceptance_record_id="channel-current",
        weak_replay={"weak_capability_replay": {"manifest_record_id": "weak-manifest"}},
        core_replay={"manifest_record_id": "core-manifest"},
        live_acceptance=live_acceptance,
    )

    assert report["ok"] is True
    assert captured["gate_evidence"]["memory.recall"] == [
        "bootstrap-pending-current",
        "core-manifest",
    ]


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
        "channel_acceptance",
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
    assert report["cross_channel_leakage_count"] == 0
    assert report["source_filter_leakage_count"] == 0
    assert _recall_result_allows_bootstrap_pending(report) is True


def test_release_closure_allows_bounded_latency_only_diagnostic_as_bootstrap_input(
    tmp_path,
) -> None:
    report = _latency_only_diagnostic_recall_report(
        tmp_path,
        actual=1520.126,
        threshold=1500.0,
    )

    assert report["ok"] is False
    assert report["quality_gate"]["blocking_metrics"] == {
        "latency_ms_p95": {
            "actual": 1520.126,
            "threshold": 1500.0,
            "operator": "<=",
        }
    }
    assert _recall_result_allows_bootstrap_pending(report) is True


def test_release_closure_allows_observed_bootstrap_smoke_latency_only_diagnostic(
    tmp_path,
) -> None:
    report = _latency_only_diagnostic_recall_report(
        tmp_path,
        actual=1606.266,
        threshold=1500.0,
    )
    report["sample_count"] = 5

    assert report["quality_gate"]["blocking_metrics"] == {
        "latency_ms_p95": {
            "actual": 1606.266,
            "threshold": 1500.0,
            "operator": "<=",
        }
    }
    assert _recall_result_allows_bootstrap_pending(report) is True


def test_release_closure_allows_low_signal_real_query_data_as_bootstrap_pending() -> None:
    report = {
        "ok": False,
        "accepted": False,
        "gate_status": "not_run",
        "blocked_reason": "query_features_low_signal",
        "threshold_gate": {
            "ok": False,
            "blocking_metrics": {},
        },
        "cross_channel_leakage_count": 0,
        "source_filter_leakage_count": 0,
    }

    assert _recall_result_allows_bootstrap_pending(report) is True


@pytest.mark.parametrize(
    ("mutation_path", "value"),
    [
        (("latency_ms_p95",), 1800.001),
        (("sample_count",), 11),
        (
            ("quality_gate", "blocking_metrics", "hit_at_1"),
            {"actual": 0.0, "threshold": 0.7, "operator": ">="},
        ),
        (("quality_gate", "blocking_metrics", "latency_ms_p95", "operator"), ">="),
        (("cross_channel_leakage_count",), 1),
        (("source_filter_leakage_count",), 1),
        (("errors",), [{"error": "seed_failed"}]),
    ],
)
def test_release_closure_rejects_unsafe_or_unbounded_latency_diagnostic(
    tmp_path,
    mutation_path: tuple[str, ...],
    value,
) -> None:
    report = _latency_only_diagnostic_recall_report(
        tmp_path,
        actual=1520.126,
        threshold=1500.0,
    )
    target = report
    for key in mutation_path[:-1]:
        target = target[key]
    target[mutation_path[-1]] = value

    assert _recall_result_allows_bootstrap_pending(report) is False


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
        (("cross_channel_leakage_count",), 1),
        (("source_filter_leakage_count",), 1),
        (("cross_channel_leakage_count",), "0"),
        (("source_filter_leakage_count",), False),
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
        "cross_channel_leakage_count",
        "source_filter_leakage_count",
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
            "channel_acceptance",
            {
                "channel_acceptance": {
                    "ok": False,
                    "error": "current_release_channel_receipt_not_found",
                }
            },
            [
                "deployment_receipt",
                "production_recall_run",
                "production_recall_verify",
                "production_recall_activate",
                "replay_bootstrap",
                "live_acceptance",
                "channel_acceptance",
                "channel_acceptance",
            ],
            "current_release_channel_receipt_not_found",
        ),
        (
            "closure_rehearsal",
            {"rehearsal": {"ok": False, "closure_complete": False, "blocked_reasons": ["replay_failed"]}},
            ["deployment_receipt", "production_recall_run", "production_recall_verify", "production_recall_activate", "replay_bootstrap", "live_acceptance", "channel_acceptance", "closure_rehearsal"],
            "replay_failed",
        ),
        (
            "readiness",
            {"readiness_score": 0.9},
            ["deployment_receipt", "production_recall_run", "production_recall_verify", "production_recall_activate", "replay_bootstrap", "live_acceptance", "channel_acceptance", "closure_rehearsal", "readiness"],
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


def test_release_closure_missing_channel_persists_resumable_checkpoint(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    runtime = FakeRuntime(
        channel_acceptance={
            "ok": False,
            "error": "current_release_channel_receipt_not_found",
        }
    )

    report = run_release_closure(
        runtime,
        **_identity_kwargs(),
        pending_path=pending_path,
    )

    assert report["blocked_stage"] == "channel_acceptance"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    assert pending["schema_version"] == "release_closure_pending.v1"
    assert pending["status"] == "waiting_for_channel_acceptance"
    assert pending["current_commit"] == CURRENT_COMMIT
    assert pending["prior_commit"] == PRIOR_COMMIT
    assert pending["deployment_receipt_id"] == "receipt-1"
    assert "version" not in pending
    assert "version" not in pending["passed_gate_reports"]["live_acceptance"].get(
        "deployment", {}
    )
    assert pending["passed_gate_reports"]["replay_bootstrap"]["ok"] is True
    assert pending["passed_gate_reports"]["live_acceptance"]["case_count"] == 10


def test_release_closure_rechecks_channel_after_arming_checkpoint(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"

    class ReceiptRaceRuntime(FakeRuntime):
        def record_openclaw_channel_acceptance(self, **kwargs) -> dict:
            super().record_openclaw_channel_acceptance(**kwargs)
            if self.calls.count("channel_acceptance") == 1:
                return {
                    "ok": False,
                    "error": "current_release_channel_receipt_not_found",
                }
            return {"ok": True, "record_id": "channel-race-winner"}

    runtime = ReceiptRaceRuntime()

    report = run_release_closure(
        runtime,
        **_identity_kwargs(),
        pending_path=pending_path,
    )

    assert report["ok"] is True
    assert report["closure_complete"] is True
    assert runtime.calls.count("channel_acceptance") == 2
    assert not pending_path.exists()


def test_release_closure_resume_uses_checkpoint_without_rerunning_pre_channel_gates(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    runtime = FakeRuntime(
        channel_acceptance={
            "ok": False,
            "error": "current_release_channel_receipt_not_found",
        }
    )
    blocked = run_release_closure(
        runtime,
        **_identity_kwargs(),
        pending_path=pending_path,
    )
    assert blocked["ok"] is False
    runtime.calls.clear()
    runtime.channel_acceptance = {"ok": True, "record_id": "channel-resumed"}

    resumed = reconcile_release_closure_pending(
        runtime,
        pending_path=pending_path,
    )

    assert resumed["ok"] is True
    assert resumed["closure_complete"] is True
    assert runtime.calls == [
        "channel_acceptance",
        "closure_rehearsal",
        "readiness",
    ]
    assert not pending_path.exists()


def test_release_closure_does_not_arm_checkpoint_for_unrelated_failure(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    runtime = FakeRuntime(
        live_acceptance={"ok": False, "error": "acceptance_case_failed"}
    )

    report = run_release_closure(
        runtime,
        **_identity_kwargs(),
        pending_path=pending_path,
    )

    assert report["blocked_stage"] == "live_acceptance"
    assert not pending_path.exists()


def test_new_release_discards_superseded_checkpoint_before_early_gate_failure(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    waiting_runtime = FakeRuntime(
        channel_acceptance={
            "ok": False,
            "error": "current_release_channel_receipt_not_found",
        }
    )
    run_release_closure(
        waiting_runtime,
        **_identity_kwargs(),
        pending_path=pending_path,
    )
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["current_commit"] = "c" * 40
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    next_runtime = FakeRuntime(
        live_acceptance={"ok": False, "error": "acceptance_case_failed"}
    )

    report = run_release_closure(
        next_runtime,
        **_identity_kwargs(),
        pending_path=pending_path,
    )

    assert report["blocked_stage"] == "live_acceptance"
    assert report["pending_checkpoint"] == {"ok": True, "status": "superseded"}
    assert not pending_path.exists()


def test_release_closure_reconcile_supersedes_stale_commit(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    runtime = FakeRuntime(
        channel_acceptance={
            "ok": False,
            "error": "current_release_channel_receipt_not_found",
        }
    )
    run_release_closure(runtime, **_identity_kwargs(), pending_path=pending_path)
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["current_commit"] = "c" * 40
    pending_path.write_text(json.dumps(pending), encoding="utf-8")
    runtime.calls.clear()

    report = reconcile_release_closure_pending(
        runtime,
        pending_path=pending_path,
    )

    assert report == {"ok": True, "status": "superseded"}
    assert runtime.calls == []
    assert not pending_path.exists()


def test_release_closure_reconcile_keeps_checkpoint_when_identity_is_missing(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    runtime = FakeRuntime(
        channel_acceptance={
            "ok": False,
            "error": "current_release_channel_receipt_not_found",
        }
    )
    run_release_closure(
        runtime,
        **_identity_kwargs(),
        pending_path=pending_path,
    )
    runtime.current_release_identity = lambda **_kwargs: None  # type: ignore[method-assign]

    report = reconcile_release_closure_pending(
        runtime,
        pending_path=pending_path,
    )

    assert report == {
        "ok": False,
        "status": "blocked",
        "error": "current_release_identity_missing",
    }
    assert pending_path.exists()


def test_release_closure_reconcile_rejects_malformed_checkpoint(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    pending_path.parent.mkdir(parents=True)
    pending_path.write_text('{"schema_version":"release_closure_pending.v1"}\n')
    runtime = FakeRuntime()

    report = reconcile_release_closure_pending(
        runtime,
        pending_path=pending_path,
    )

    assert report["ok"] is False
    assert report["status"] == "invalid"
    assert report["error"] == "release_closure_pending_invalid"
    assert runtime.calls == []
    assert pending_path.exists()


def test_release_closure_reconcile_skips_when_lock_is_held(
    tmp_path: Path,
) -> None:
    pending_path = tmp_path / "state" / "release-closure-pending.json"
    runtime = FakeRuntime(
        channel_acceptance={
            "ok": False,
            "error": "current_release_channel_receipt_not_found",
        }
    )
    run_release_closure(runtime, **_identity_kwargs(), pending_path=pending_path)
    runtime.calls.clear()

    with pending_module._release_closure_reconcile_lock(pending_path):
        report = reconcile_release_closure_pending(
            runtime,
            pending_path=pending_path,
        )

    assert report == {
        "ok": False,
        "status": "busy",
        "error": "release_closure_reconcile_busy",
    }
    assert runtime.calls == []
    assert pending_path.exists()


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


def test_release_closure_test_helper_ignores_inherited_pending_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inherited_path = tmp_path / "production" / "release-closure-pending.json"
    checkpoint = pending_module.build_release_closure_pending(
        scope=SCOPE,
        repo_root=REPO_ROOT,
        current_link=CURRENT_LINK,
        health_url=HEALTH_URL,
        prior_commit=PRIOR_COMMIT,
        current_release=ReleaseIdentity(
            CURRENT_COMMIT,
            "1.9.51",
            "receipt-1",
            "receipt-1",
        ),
        release_path=f"/opt/eimemory/releases/{CURRENT_COMMIT}",
        record_ids={"deployment_receipt": "receipt-1"},
        replay_bootstrap=_successful_replay_bootstrap(),
        live_acceptance=_successful_live_acceptance(),
        bootstrap_pending=None,
    )
    pending_module.write_release_closure_pending(checkpoint, path=inherited_path)
    before = inherited_path.read_text(encoding="utf-8")
    monkeypatch.setenv(
        "EIMEMORY_RELEASE_CLOSURE_PENDING_PATH",
        str(inherited_path),
    )
    runtime = FakeRuntime(
        channel_acceptance={
            "ok": False,
            "error": "current_release_channel_receipt_not_found",
        }
    )

    _run(runtime)

    assert inherited_path.read_text(encoding="utf-8") == before


def _run(runtime: FakeRuntime) -> dict:
    with TemporaryDirectory(prefix="eimemory-release-closure-test-") as root:
        return run_release_closure(
            runtime,
            scope=SCOPE,
            repo_root=REPO_ROOT,
            current_link=CURRENT_LINK,
            health_url=HEALTH_URL,
            prior_commit=PRIOR_COMMIT,
            pending_path=Path(root) / "release-closure-pending.json",
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


def _latency_only_diagnostic_recall_report(
    tmp_path,
    *,
    actual: float,
    threshold: float,
) -> dict:
    report = _passing_diagnostic_recall_report(tmp_path)
    report.update(
        {
            "ok": False,
            "gate_ok": False,
            "passed_threshold": False,
            "blocked_reason": "recall_quality_gate_failed",
            "latency_ms_p95": actual,
        }
    )
    report["quality_gate"].update(
        {
            "ok": False,
            "blocked_reason": "recall_quality_gate_failed",
            "blocking_metrics": {
                "latency_ms_p95": {
                    "actual": actual,
                    "threshold": threshold,
                    "operator": "<=",
                }
            },
        }
    )
    report["quality_gate"]["thresholds"]["latency_ms_p95"] = threshold
    return report


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
    lineage = {
        "ok": True,
        "validated": True,
        "compatible": True,
        "record_id": "lineage-current",
        "schema_version": "release_lineage.v1",
        "current_release": {
            "commit": CURRENT_COMMIT,
            "version": "1.9.51",
            "receipt_id": "receipt-1",
            "session_id": "receipt-1",
        },
    }
    return {
        "ok": True,
        "schema_version": "l5_readiness.v2",
        "release_identity": {
            "release_commit": CURRENT_COMMIT,
            "release_version": "1.9.51",
            "deployment_receipt_id": "receipt-1",
            "release_session_id": "receipt-1",
        },
        "release_lineage": lineage,
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
        "real_business_gate": {
            "ok": True,
            "accepted_path": "live_tasks",
            "live_tasks": {
                "ok": True,
                "current_deployment_verified_real_tasks": 10,
            },
            "real_replay": {"ok": False},
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


def test_release_closure_accepts_verified_real_replay_without_new_live_task_accumulation() -> None:
    readiness = _successful_readiness()
    readiness["live_task_gate"] = {
        "ok": False,
        "current_deployment_verified_real_tasks": 0,
        "sample_deficit": 10,
        "task_type_deficit": 5,
    }
    readiness["real_business_gate"] = {
        "ok": True,
        "accepted_path": "real_replay",
        "live_tasks": readiness["live_task_gate"],
        "real_replay": {
            "ok": True,
            "sample_count": 10,
            "distinct_task_types": 5,
            "pass_rate": 0.8,
            "minimum_samples": 10,
            "minimum_task_types": 5,
            "minimum_pass_rate": 0.8,
            "provenance_contract": "verified_real_replay.v1",
        },
    }

    report = _run(FakeRuntime(readiness=readiness))

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["blocked_stage"] == "readiness"


def test_release_closure_respects_fatal_maturity_checkpoint_below_observed_l5() -> None:
    readiness = _successful_readiness()
    readiness["observed_stage"] = "L5"
    readiness["observed_score"] = 1.0
    readiness["current_stage"] = "L4.5"
    readiness["readiness_score"] = 0.8
    readiness["maturity_transition"] = "fatal_downgrade"
    readiness["downgrade_incident_id"] = "incident-fatal"

    report = _run(FakeRuntime(readiness=readiness))

    assert report["ok"] is False
    assert report["closure_complete"] is False
    assert report["blocked_stage"] == "readiness"
