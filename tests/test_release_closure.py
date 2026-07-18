from __future__ import annotations

from copy import deepcopy

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.governance import closure_rehearsal as closure_rehearsal_module
from eimemory.governance import release_closure as release_closure_module
from eimemory.governance.release_closure import run_release_closure


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
    ) -> None:
        self.calls: list[str] = []
        self.receipt = receipt or _successful_receipt()
        self.replay_bootstrap = replay_bootstrap or _successful_replay_bootstrap()
        self.live_acceptance = live_acceptance or _successful_live_acceptance()
        self.rehearsal = rehearsal or _successful_rehearsal()
        self.readiness = readiness or _successful_readiness()

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
        assert kwargs == {
            "scope": SCOPE,
            "persist": True,
            "replay_bootstrap": self.replay_bootstrap,
        }
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
        "readiness": "readiness-1",
    }


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
            ["deployment_receipt", "replay_bootstrap"],
            "weak_capability_replay_failed",
        ),
        (
            "live_acceptance",
            {"live_acceptance": {"ok": False, "error": "acceptance_case_failed"}},
            ["deployment_receipt", "replay_bootstrap", "live_acceptance"],
            "acceptance_case_failed",
        ),
        (
            "closure_rehearsal",
            {"rehearsal": {"ok": False, "closure_complete": False, "blocked_reasons": ["replay_failed"]}},
            ["deployment_receipt", "replay_bootstrap", "live_acceptance", "closure_rehearsal"],
            "replay_failed",
        ),
        (
            "readiness",
            {"readiness_score": 0.9},
            ["deployment_receipt", "replay_bootstrap", "live_acceptance", "closure_rehearsal", "readiness"],
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


def test_release_closure_reports_data_accumulation_without_claiming_l5_complete() -> None:
    readiness = {
        **_successful_readiness(),
        "current_stage": "data_accumulating",
        "readiness_score": 0.9,
        "live_task_gate": {
            "ok": False,
            "sample_deficit": 10,
            "current_deployment_verified_real_tasks": 0,
        },
    }

    report = _run(FakeRuntime(readiness=readiness))

    assert report["ok"] is True
    assert report["closure_complete"] is False
    assert report["data_accumulating"] is True
    assert report["blocked_stage"] == ""


def _run(runtime: FakeRuntime) -> dict:
    return run_release_closure(
        runtime,
        scope=SCOPE,
        repo_root=REPO_ROOT,
        current_link=CURRENT_LINK,
        health_url=HEALTH_URL,
        prior_commit=PRIOR_COMMIT,
    )


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
        "current_stage": "L5",
        "readiness_score": 1.0,
        "latest_l5_assessment": {"complete": True, "record_id": "assessment-1"},
        "live_task_gate": {"ok": True, "current_deployment_verified_real_tasks": 10},
        "verified_replay": {"weak_capabilities_missing": [], "executed_count": 12, "pass_count": 12, "fail_count": 0},
        "persisted_record_id": "readiness-1",
    }
