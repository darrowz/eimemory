from types import SimpleNamespace
from dataclasses import asdict

import pytest

from eimemory.governance.evolution.system_code_maintenance import run_system_code_maintenance
from eimemory.ops.runtime_identity_drift import detect_runtime_identity_drift
from eimemory.ops.system_code_repair_failure import detect_system_code_repair_failure


SCOPE = {"tenant_id": "default", "agent_id": "hongrui", "workspace_id": "embodied", "user_id": "darrow"}
COMMIT = "a" * 40


@pytest.fixture
def owners(monkeypatch):
    calls = []
    detector = detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + COMMIT},
        detected_at="2026-10-19T00:00:00Z")
    detector["incident_record_id"] = ""
    route = {"ok": True, "status": "idle", "processed": []}
    failure = detect_system_code_repair_failure(
        route, policy={}, release_commit=COMMIT, detected_at="2026-10-19T00:00:00Z")
    failure["incident_record_id"] = ""
    recovery = {"ok": True, "status": "idle", "resumed_count": 0}
    def invoke(name, value, **kwargs):
        calls.append((name, kwargs))
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr("eimemory.ops.runtime_identity_drift.inspect_live_runtime_identity",
                        lambda _runtime, **kw: invoke("detector", detector, **kw))
    monkeypatch.setattr("eimemory.governance.evolution.system_code_repair.process_system_code_incidents",
                        lambda _runtime, **kw: invoke("router", route, **kw))
    monkeypatch.setattr("eimemory.ops.system_code_repair_failure.record_system_code_repair_failure",
                        lambda _runtime, **kw: invoke("failure", failure, **kw))
    runtime = SimpleNamespace(resume_code_evolution_transactions=lambda **kw: invoke("recovery", recovery, **kw))
    return SimpleNamespace(runtime=runtime, calls=calls, detector=detector, route=route, failure=failure, recovery=recovery)


def run(owners, **kwargs):
    return run_system_code_maintenance(owners.runtime, scope=SCOPE, owner_id="test-owner", **kwargs)


def test_healthy_current_detector_is_idle_and_preserves_owner(owners):
    report = run(owners)
    assert report["execution_ok"] is True and report["status"] == "idle"
    assert report["repair_complete"] is False
    assert [name for name, _ in owners.calls] == ["detector", "router", "failure", "recovery"]
    assert all(asdict(kwargs["scope"]) == SCOPE for _, kwargs in owners.calls)
    assert owners.calls[1][1]["max_items"] == 1
    assert owners.calls[-1][1]["owner_id"] == "test-owner"


def test_dry_run_never_calls_detectors_provider_or_effect_recovery(owners):
    report = run(owners, apply=False)
    assert report["execution_ok"] is True and report["status"] == "skipped"
    assert owners.calls == []


@pytest.mark.parametrize("healthy", [True, False])
def test_owner_bound_v2_detector_is_validated_by_the_native_contract(owners, healthy):
    owners.detector.clear()
    owners.detector.update(detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + (COMMIT if healthy else "b" * 40)},
        detected_at="2026-10-19T00:00:00Z"))
    owners.detector["incident_record_id"] = "" if healthy else "incident-native"
    assert run(owners)["execution_ok"] is True
    assert [name for name, _ in owners.calls] == ["detector", "router", "failure", "recovery"]


@pytest.mark.parametrize("bad", ["foreign_owner", "changed_hash", "wrong_status"])
def test_corrupt_v2_detector_blocks_routes_and_keeps_recovery(owners, bad):
    owners.detector.clear()
    owners.detector.update(detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + COMMIT},
        detected_at="2026-10-19T00:00:00Z"))
    if bad == "foreign_owner":
        owners.detector["scope"] = {**SCOPE, "user_id": "another-user"}
    elif bad == "changed_hash":
        owners.detector["identity_digest"] = "f" * 64
    else:
        owners.detector["status"] = "drift_detected"
    assert run(owners)["execution_ok"] is False
    assert [name for name, _ in owners.calls] == ["detector", "recovery"]


def test_unconfigured_nightly_preserves_recovery_without_system_detectors(owners):
    report = run(owners, detect_and_route=False)
    assert report["ok"] is True and report["system_detection_enabled"] is False
    assert [name for name, _ in owners.calls] == ["recovery"]


@pytest.mark.parametrize("invalid", [-1, 101, True, "1", 1.1])
def test_invalid_bounds_fail_before_any_owner(owners, invalid):
    with pytest.raises(ValueError):
        run(owners, limit=invalid)
    assert owners.calls == []


@pytest.mark.parametrize("bad", [
    {"ok": False}, {"ok": False, "status": "unavailable"},
    {"ok": True, "status": "current", "schema": "runtime_identity_drift.v1"},
    {"ok": False, "status": "current", "schema": "runtime_identity_drift.v1"},
    {"ok": True, "status": "current", "schema": "runtime_identity_drift.v1", "error": "read_failed"},
])
def test_unverified_detector_blocks_new_routes_but_always_recovers_existing_leases(owners, bad):
    owners.detector.clear(); owners.detector.update(bad)
    report = run(owners)
    assert report["ok"] is False and report["status"] == "blocked"
    assert [name for name, _ in owners.calls] == ["detector", "recovery"]


@pytest.mark.parametrize("bad", [
    {"ok": False}, {"ok": False, "status": "unavailable"},
    {"ok": False, "status": "non_actionable", "schema": "system_code_repair_failure.v1"},
    {"ok": True, "status": "non_actionable", "schema": "system_code_repair_failure.v1", "error": "read_failed"},
])
def test_failure_detector_cannot_default_malformed_or_failed_results_to_success(owners, bad):
    owners.failure.clear(); owners.failure.update(bad)
    report = run(owners)
    assert report["execution_ok"] is False
    assert owners.calls[-1][0] == "recovery"


def test_valid_system_drift_is_an_observation_and_not_a_false_detector_exception(owners):
    owners.detector.clear()
    owners.detector.update(detect_runtime_identity_drift(
        expected_commit=COMMIT, scope=SCOPE,
        unit_environments={"eimemory-rpc.service": "EIMEMORY_RUNTIME_COMMIT=" + "b" * 40},
        detected_at="2026-10-19T00:00:00Z"))
    owners.detector["incident_record_id"] = "incident-native"
    owners.route.update(status="processed", processed=[{"transaction_id": "transaction-native", "status": "PROPOSED"}])
    report = run(owners)
    assert report["execution_ok"] is True and report["status"] == "processed"
    assert report["repair_complete"] is False
    assert report["runtime_identity"]["known_before_detection"] is False
    assert report["code_evolution"] == owners.recovery


def test_blocked_route_and_failed_recovery_remain_visible(owners):
    owners.route.update(ok=False, status="blocked", reason="automation_policy_identity_unavailable")
    owners.recovery.update(ok=False, status="blocked", reason="observation_failed")
    report = run(owners)
    assert report["execution_ok"] is False and report["repair_complete"] is False
    assert report["system_code_repair"]["reason"] == "automation_policy_identity_unavailable"
    assert report["code_evolution"]["reason"] == "observation_failed"
    assert owners.calls[-1][0] == "recovery"


@pytest.mark.parametrize("stage", ["detector", "route", "failure", "recovery"])
def test_explicit_error_list_is_never_overridden_by_a_success_flag(owners, stage):
    getattr(owners, stage)["errors"] = ["explicit_failure"]
    report = run(owners)
    assert report["ok"] is False and report["execution_ok"] is False
    assert owners.calls[-1][0] == "recovery"


@pytest.mark.parametrize("apply,maintenance_ok", [(False, True), (True, True), (True, False)])
def test_watch_uses_shared_owner_and_propagates_nested_failure(monkeypatch, capsys, apply, maintenance_ok):
    from eimemory.cli import main as cli
    calls = []
    monkeypatch.setattr("eimemory.governance.world_watchers.collect_world_signals", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr("eimemory.governance.world_watchers.default_watches", lambda: [])
    def maintenance(_runtime, **kwargs):
        calls.append(kwargs)
        return {"execution_ok": maintenance_ok, "runtime_identity": {}, "system_code_repair": {},
                "system_code_repair_failure": {}, "code_evolution": {}}
    monkeypatch.setattr("eimemory.governance.evolution.system_code_maintenance.run_system_code_maintenance", maintenance)
    args = cli._build_parser().parse_args(["learn", "watch"] + (["--apply"] if apply else []))
    assert cli._cmd_learn(args, runtime=object(), scope=SCOPE) == (0 if maintenance_ok else 1)
    assert calls[0]["apply"] is apply and calls[0]["owner_id"] == "learn-watch:code-evolution"
    assert '"system_code_maintenance"' in capsys.readouterr().out
