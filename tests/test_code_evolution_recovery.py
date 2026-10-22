from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from eimemory.governance.code_evolution_observation import DEFAULT_OBSERVATION_SECONDS, OBSERVATION_OFFSETS

from eimemory.governance.code_evolution_transaction import (
    CodeEvolutionTransactionManager,
    reconcile_commit,
    reconcile_deployment,
    reconcile_push,
    reconcile_rollback,
    recover_transaction,
)
from eimemory.governance.promotion_watch import (
    _record_code_evolution_observation_sample as observe_code_evolution_transaction,
)
from eimemory.governance.promotion_watch import (
    observe_code_evolution_transaction as observe_live_code_evolution_transaction,
)
from eimemory.governance.promotion_watch import resume_code_evolution_transactions
from eimemory.storage.runtime_store import RuntimeStore
from eimemory.storage.code_evolution_store import CodeEvolutionConflict
from eimemory.storage.code_evolution_store import CodeEvolutionStore


@pytest.fixture(autouse=True)
def isolated_native_observation_clock(monkeypatch):
    """These recovery fixtures simulate elapsed time; never use a production clock."""
    from eimemory.governance.promotion import promotion_watch as native_watch
    original = _sample
    clock = {'schema': 'code_observation_clock.v1', 'wall_utc': '2026-01-01T00:00:00+00:00',
             'boot_id': '11111111-1111-1111-1111-111111111111',
             'monotonic_ns': int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()) * 10**9}
    def fixture_sample(*args, **kwargs):
        instant = datetime.fromisoformat(kwargs['observed_at'])
        clock.update(schema='code_observation_clock.v1', wall_utc=instant.isoformat(),
                     boot_id='11111111-1111-1111-1111-111111111111',
                     monotonic_ns=int(instant.timestamp()) * 10**9)
        return original(*args, **kwargs)
    monkeypatch.setattr(__name__ + '._sample', fixture_sample)
    monkeypatch.setattr(native_watch, 'native_observation_clock', lambda: dict(clock))


def _observing_manager(runtime_store: RuntimeStore, transaction_id: str, *, state="OBSERVING") -> CodeEvolutionTransactionManager:
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="watch-test")
    manager.create_detected({
        "transaction_id": transaction_id,
        "idempotency_key": f"idem-{transaction_id}",
        "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        "incident": {"incident_id": f"incident-{transaction_id}", "incident_class": "code"},
        "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
    })
    for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT", "COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"):
        manager.transition(transaction_id, target)
        if target == state:
            break
    manager.update_metadata(
        transaction_id,
        updates={"observation_started_at": "2026-01-01T00:00:00+00:00",
                 "observation_deadline": (datetime(2026, 1, 1, tzinfo=timezone.utc)
                                          + timedelta(seconds=DEFAULT_OBSERVATION_SECONDS)).isoformat()},
        payload_updates={
            "candidate_pushed_and_deployed": True,
            "deployment_receipt_digest": "c" * 64,
        },
    )
    return manager


def _sample(key: str, *, observed_at: str, health_ok: bool, measure: int = 1) -> dict:
    return {
        "sample_key": key,
        "observed_at": observed_at,
        "commit": "a" * 40,
        "release_identity": "release-a",
        "provider_advertisement_digest": "b" * 64,
        "deployment_receipt_digest": "c" * 64,
        "incident_measure": {"value": measure},
        "health_ok": health_ok,
    }


@pytest.fixture
def failed_managed_observation(tmp_path, monkeypatch, request):
    """Real SQL ledger plus isolated authenticated-manager transport fixtures."""
    from copy import deepcopy
    from eimemory.core import runtime_manager as native
    from eimemory.governance.evolution import code_deployment_job as jobs

    store = RuntimeStore(tmp_path / "runtime")
    requested_state = getattr(request, "param", "OBSERVING")
    manager = _observing_manager(store, "native-failed-observation", state=requested_state)
    transaction_id = "native-failed-observation"
    key, invocation = "1" * 64, "2" * 32
    candidate = tmp_path / "releases" / ("b" * 40)
    candidate.mkdir(parents=True)
    owner = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}
    manager.update_metadata(transaction_id, updates={"candidate_commit": "b" * 40,
        "deployed_commit": "b" * 40 if requested_state == "OBSERVING" else ""},
        payload_updates={"managed_deployment_jobs": {"deploy": key},
            **({"post_resume_business_job_key": key, "post_resume_business_invocation": invocation}
               if requested_state == "OBSERVING" else {}),
            "authorized_policy": {"deployment": {"current_link": str(candidate)}}})
    body = {"transaction_id": transaction_id, "operation": "deploy", "scope": owner,
        "base_commit": "a" * 40, "candidate_commit": "b" * 40, "target_commit": "b" * 40,
        "controller_commit": "c" * 40}
    registry = {"generation": "3" * 64, "runtime_commit": "b" * 40, "controller_commit": "c" * 40}
    terminal = {"LoadState": "loaded", "ActiveState": "failed", "SubState": "failed", "Result": "signal",
        "InvocationID": invocation, "ExecMainStatus": "9", "MainPID": "0", "ControlGroup": "",
        "ExecMainExitTimestampMonotonic": "1234567"}
    recovery = {"schema": "eimemory.native_failed_job_restore.v1", "job_key": key,
        "original_invocation": invocation, "original_phase": "serving_awaiting_business", "outcome": "failed",
        "failure_reason": "worker_abnormal_native_exit", "failure_kind": "native_exit", "native_exit": terminal,
        "boot_change": None, "registry_generation": registry["generation"], "current_commit": "b" * 40,
        "controller_commit": "c" * 40, "restore_plan_sha256": "4" * 64, "phase": "failure_verified",
        "autonomous_repair_credit": False}
    status = {"job_key": key, "unit": jobs._validation_unit(key, "receipt").replace("code-validation-receipt", "code-deployment-job"),
        "manager": jobs._manager_name(), "original_invocation": invocation, "state": deepcopy(terminal),
        "terminal_state": deepcopy(terminal), "phase": "serving_awaiting_business", "complete": False,
        "resumed": False, "background_held": True, "terminal_recovery": deepcopy(recovery)}
    artifact = tmp_path / "jobs" / key
    artifact.mkdir(parents=True, mode=0o700)
    artifact.chmod(0o700)
    (artifact / "started.json").write_bytes(jobs.canonical_bytes({"request_key": key, "invocation": invocation}))
    (artifact / "started.json").chmod(0o600)
    original_result = b'{"original_failed_worker_result":"preserved"}'
    (artifact / "result.json").write_bytes(original_result)
    (artifact / "result.json").chmod(0o600)
    monkeypatch.setattr(jobs, "JOBS_ROOT", artifact.parent)
    monkeypatch.setattr(jobs, "_bound_request", lambda tx, operation: (key, deepcopy(body)))
    monkeypatch.setattr(native, "deployment_job_status", lambda requested: deepcopy(status))
    monkeypatch.setattr(native, "read_registry", lambda: deepcopy(registry))
    monkeypatch.setattr("eimemory.governance.code_evolution_effects.sample_code_evolution_observation",
        lambda *_args, **_kwargs: {"ok": False, "reason": "isolated_observation_unavailable"})
    try:
        yield {"store": store, "manager": manager, "owner": owner, "transaction_id": transaction_id,
            "status": status, "registry": registry, "body": body, "key": key, "invocation": invocation,
            "candidate": candidate, "artifact": artifact, "original_result": original_result}
    finally:
        store.close()


@pytest.mark.parametrize("failure", ["signal", "timeout", "exit-code"])
@pytest.mark.parametrize("phase", ["failure_verified", "restoring", "restored"])
def test_failed_native_worker_rolls_observing_sql_into_intent_without_worker_replay(failed_managed_observation, failure, phase):
    f = failed_managed_observation
    native = f["status"]
    native["terminal_state"]["Result"] = native["state"]["Result"] = failure
    native["terminal_recovery"]["native_exit"]["Result"] = failure
    native["terminal_recovery"]["phase"] = phase
    before_result = (f["artifact"] / "result.json").read_bytes()
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    assert result["reports"][0]["status"] == "rollback_required"
    current = f["manager"].store.get_transaction(f["transaction_id"])
    assert current["current_state"] == "ROLLBACK_INTENT" and not current["terminal"]
    events = f["manager"].store.list_step_events(f["transaction_id"])
    assert [(row["step"], row["phase"]) for row in events] == [("rollback", "intent")]
    assert f["manager"].store.get_terminal_receipt(f["transaction_id"]) is None
    assert (f["artifact"] / "result.json").read_bytes() == before_result == f["original_result"]


def test_failed_worker_after_completed_native_transition_still_rolls_back_sql(failed_managed_observation):
    f = failed_managed_observation
    f["status"].update(phase="completed", complete=True, finalized=True, resumed=True,
        held_background=[], background_held=False, terminal_recovery=None)
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    assert result["reports"][0]["status"] == "rollback_required"
    assert f["manager"].store.get_transaction(f["transaction_id"])["current_state"] == "ROLLBACK_INTENT"
    assert f["status"]["phase"] == "completed" and f["status"]["complete"] is True
    assert (f["artifact"] / "result.json").read_bytes() == f["original_result"]


@pytest.mark.parametrize("fault", ["transport", "missing_exit", "successful_exit", "foreign_invocation",
    "foreign_marker", "foreign_scope", "foreign_target", "foreign_generation", "unknown_boot",
    "unbound_marker_extra", "changed_started", "foreign_current"])
def test_unknown_or_foreign_worker_failure_cannot_change_observing_sql(failed_managed_observation, monkeypatch, fault):
    from eimemory.core import runtime_manager as native
    f = failed_managed_observation
    status = f["status"]
    marker = status["terminal_recovery"]
    if fault == "transport":
        monkeypatch.setattr(native, "deployment_job_status", lambda *_: (_ for _ in ()).throw(native.RuntimeManagerError("manager_transport_unavailable")))
    elif fault == "missing_exit":
        status["terminal_state"] = marker["native_exit"] = None
    elif fault == "successful_exit":
        for value in (status["terminal_state"], status["state"], marker["native_exit"]):
            value.update(Result="success", ExecMainStatus="0", ActiveState="inactive")
    elif fault == "foreign_invocation":
        status["original_invocation"] = "f" * 32
    elif fault == "foreign_marker":
        marker["job_key"] = "f" * 64
    elif fault == "foreign_scope":
        f["body"]["scope"] = {**f["owner"], "user_id": "foreign"}
    elif fault == "foreign_target":
        marker["current_commit"] = "f" * 40
    elif fault == "foreign_generation":
        marker["registry_generation"] = "f" * 64
    elif fault == "unknown_boot":
        marker.update(failure_kind="boot_change", failure_reason="worker_interrupted_by_boot_change", native_exit=None)
    elif fault == "unbound_marker_extra":
        marker["caller_says_failed"] = True
    elif fault == "changed_started":
        (f["artifact"] / "started.json").write_text('{}')
    elif fault == "foreign_current":
        f["candidate"].rename(f["candidate"].with_name("f" * 40))
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    assert result["ok"] is False
    assert f["manager"].store.get_transaction(f["transaction_id"])["current_state"] == "OBSERVING"
    assert f["manager"].store.list_step_events(f["transaction_id"]) == []
    assert f["manager"].store.get_terminal_receipt(f["transaction_id"]) is None


def test_native_failed_proof_is_reread_after_sql_lease(failed_managed_observation, monkeypatch):
    from copy import deepcopy
    from eimemory.core import runtime_manager as native
    f = failed_managed_observation
    calls = []
    def status(_key):
        calls.append(_key)
        value = deepcopy(f["status"])
        if len(calls) > 1:
            value["terminal_recovery"]["restore_plan_sha256"] = "f" * 64
        return value
    monkeypatch.setattr(native, "deployment_job_status", status)
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    assert result["ok"] is False and len(calls) == 2
    assert f["manager"].store.get_transaction(f["transaction_id"])["current_state"] == "OBSERVING"
    assert f["manager"].store.list_step_events(f["transaction_id"]) == []


def test_failed_worker_recovery_respects_another_sql_owner(failed_managed_observation):
    f = failed_managed_observation
    other = CodeEvolutionTransactionManager(f["store"], owner_id="other-live-owner")
    other.acquire_lease(f["transaction_id"])
    try:
        report = resume_code_evolution_transactions(f["store"], scope=f["owner"])
        assert report["ok"] is False
        assert f["manager"].store.get_transaction(f["transaction_id"])["current_state"] == "OBSERVING"
        assert f["manager"].store.list_step_events(f["transaction_id"]) == []
    finally:
        other.release_lease(f["transaction_id"])


def test_replayed_failed_worker_proof_does_not_append_a_second_rollback_intent(failed_managed_observation):
    from eimemory.governance.promotion.promotion_watch import _recover_failed_managed_observation
    f = failed_managed_observation
    stale = f["manager"].store.get_transaction(f["transaction_id"])
    owner = CodeEvolutionTransactionManager(f["store"], owner_id="native-recovery-fixture")
    first = _recover_failed_managed_observation(f["store"], manager=owner, transaction=stale)
    second = _recover_failed_managed_observation(f["store"], manager=owner, transaction=stale)
    assert first["status"] == "rollback_required" and second["status"] == "managed_failed_job_state_changed"
    events = f["manager"].store.list_step_events(f["transaction_id"])
    assert [(row["step"], row["phase"]) for row in events] == [("rollback", "intent")]
    assert (f["artifact"] / "result.json").read_bytes() == f["original_result"]


@pytest.mark.parametrize("failed_managed_observation", ["DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY"], indirect=True)
@pytest.mark.parametrize("fault", [None, "unknown", "foreign_started", "foreign_scope"])
def test_pre_observation_worker_failure_preempts_forward_effects(failed_managed_observation, monkeypatch, fault):
    from eimemory.governance.evolution import code_evolution_effects as effects
    f = failed_managed_observation
    before = f["manager"].store.get_transaction(f["transaction_id"])
    assert not before.get("deployed_commit")
    assert "post_resume_business_invocation" not in before["payload"]
    monkeypatch.setattr(effects, "execute_code_evolution_effects", lambda *_a, **_k: pytest.fail("forward worker replay"))
    monkeypatch.setattr(effects, "read_code_evolution_external_state", lambda *_a, **_k: pytest.fail("physical receipt promoted before failure recovery"))
    if fault == "unknown": f["status"].update(terminal_recovery=None, terminal_state=None)
    elif fault == "foreign_started": (f["artifact"] / "started.json").write_text('{}')
    elif fault == "foreign_scope": f["body"]["scope"] = {**f["owner"], "agent_id": "foreign"}
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    after = f["manager"].store.get_transaction(f["transaction_id"])
    assert after["current_state"] == ("ROLLBACK_INTENT" if fault is None else before["current_state"])
    assert result["reports"][0]["status"] == ("rollback_required" if fault is None else "managed_failed_job_recovery_unverified")
    assert not after.get("deployed_commit") and f["manager"].store.get_terminal_receipt(f["transaction_id"]) is None
    assert (f["artifact"] / "result.json").read_bytes() == f["original_result"]


@pytest.mark.parametrize("failed_managed_observation", ["DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY"], indirect=True)
def test_only_unlanded_deploy_intent_may_recover_failure_on_its_original_base(failed_managed_observation):
    f = failed_managed_observation
    before = f["manager"].store.get_transaction(f["transaction_id"])
    base = f["candidate"].with_name(f["body"]["base_commit"])
    base.mkdir()
    f["manager"].update_metadata(f["transaction_id"], payload_updates={"authorized_policy": {
        "deployment": {"current_link": str(base)}}})
    f["registry"]["runtime_commit"] = base.name
    f["status"]["terminal_recovery"]["current_commit"] = base.name
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    after = f["manager"].store.get_transaction(f["transaction_id"])
    accepted = before["current_state"] == "DEPLOY_INTENT"
    assert after["current_state"] == ("ROLLBACK_INTENT" if accepted else before["current_state"])
    assert result["reports"][0]["status"] == ("rollback_required" if accepted else "managed_failed_job_recovery_unverified")


@pytest.mark.parametrize("failed_managed_observation", ["DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"], indirect=True)
@pytest.mark.parametrize("kind", ["native_exit", "boot_change"])
def test_sql_failure_recovery_does_not_wait_for_controller_health_or_restore_marker(failed_managed_observation, monkeypatch, kind):
    from copy import deepcopy
    from eimemory.contracts import runtime_manager as contract
    from eimemory.core import runtime_manager as native
    from eimemory.governance.evolution import code_evolution_effects as effects
    f = failed_managed_observation
    registry_reads = []
    def read_registry():
        registry_reads.append(1)
        return deepcopy(f["registry"])
    monkeypatch.setattr(native, "read_registry", read_registry)
    f["status"]["terminal_recovery"] = None
    assert not f["status"]["resumed"] and f["status"]["background_held"]
    monkeypatch.setattr(effects, "execute_code_evolution_effects", lambda *_a, **_k: pytest.fail("forward replay"))
    monkeypatch.setattr(effects, "read_code_evolution_external_state", lambda *_a, **_k: pytest.fail("health is not a failure prerequisite"))
    if kind == "boot_change":
        unit = contract.job_unit(f["key"])
        group = "/system.slice/" + unit
        if contract.USER_MODE:
            uid = str(contract._user_coordinates["uid"])
            group = "/user.slice/user-" + uid + ".slice/user@" + uid + ".service/app.slice/" + unit
        plan = {"active_units": ["eimemory-rpc.service"], "enabled_units": {}}
        hold = {"schema": "eimemory.native_deployment_hold.v1", "job_key": f["key"],
            "original_invocation": f["invocation"], "boot_id": "22222222-2222-2222-2222-222222222222",
            "registry_generation": "6" * 64, "restore_plan_sha256": contract.digest(plan),
            "native_owner": {"ActiveState": "activating", "InvocationID": f["invocation"], "ControlGroup": group},
            "peer_pid": 123}
        f["status"].update(terminal_state=None, native_hold=hold, restore_plan=plan, launch_registry_generation="6" * 64)
        f["status"]["state"].update(LoadState="not-found", ActiveState="inactive", SubState="dead",
            InvocationID="", ControlPID="0", Result="success", ExecMainStatus="0")
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    current = f["manager"].store.get_transaction(f["transaction_id"])
    assert current["current_state"] == "ROLLBACK_INTENT" and result["reports"][0]["status"] == "rollback_required"
    assert len(registry_reads) == 2
    assert f["status"]["terminal_recovery"] is None and not f["status"]["resumed"]
    assert f["manager"].store.get_terminal_receipt(f["transaction_id"]) is None
    assert (f["artifact"] / "result.json").read_bytes() == f["original_result"]
    if kind == "boot_change":
        assert f["status"]["terminal_state"] is None


@pytest.mark.parametrize("fault", [None, "same_boot", "foreign_boot", "missing_hold", "active_worker",
    "different_invocation", "invented_exit", "changed_native_state", "garbage_collected", "gc_foreign_invocation",
    "held_garbage_collected", "second_boot", "independent_missing_hold", "independent_same_boot",
    "independent_foreign_hold", "independent_running"])
def test_bound_boot_interruption_is_separate_from_failed_exit(failed_managed_observation, monkeypatch, fault):
    from copy import deepcopy
    from eimemory.contracts import runtime_manager as contract
    f = failed_managed_observation
    old_boot = "22222222-2222-2222-2222-222222222222"
    new_boot = "11111111-1111-1111-1111-111111111111"
    state = {**f["status"]["state"], "LoadState": "not-found", "ActiveState": "inactive",
        "InvocationID": "", "ControlPID": "0", "Result": "success", "ExecMainStatus": "0"}
    marker = f["status"]["terminal_recovery"]
    unit = contract.job_unit(f["key"])
    group = "/system.slice/" + unit
    if contract.USER_MODE:
        uid = str(contract._user_coordinates["uid"])
        group = "/user.slice/user-" + uid + ".slice/user@" + uid + ".service/app.slice/" + unit
    plan = {"active_units": ["eimemory-rpc.service"], "enabled_units": {}}
    hold = {"schema": "eimemory.native_deployment_hold.v1", "job_key": f["key"],
        "original_invocation": f["invocation"], "boot_id": old_boot, "registry_generation": "6" * 64,
        "restore_plan_sha256": contract.digest(plan), "native_owner": {"ActiveState": "activating",
            "InvocationID": f["invocation"], "ControlGroup": group}, "peer_pid": 123}
    f["status"].update(native_hold=hold, restore_plan=plan, launch_registry_generation="6" * 64)
    marker.update(failure_kind="boot_change", failure_reason="worker_interrupted_by_boot_change", native_exit=None,
        restore_plan_sha256=contract.digest(plan),
        boot_change={"schema": "eimemory.native_job_boot_interruption.v1", "hold_sha256": contract.digest(hold),
            "previous_boot_id": old_boot, "current_boot_id": new_boot, "native_state": deepcopy(state)})
    f["status"].update(state=state, terminal_state=None)
    if fault == "same_boot":
        marker["boot_change"]["previous_boot_id"] = new_boot
    elif fault == "foreign_boot":
        marker["boot_change"]["current_boot_id"] = old_boot
    elif fault == "missing_hold":
        marker["boot_change"]["hold_sha256"] = ""
    elif fault == "active_worker":
        state["ActiveState"] = "active"
        marker["boot_change"]["native_state"] = deepcopy(state)
    elif fault == "different_invocation":
        state["InvocationID"] = "f" * 32
        marker["boot_change"]["native_state"] = deepcopy(state)
    elif fault == "invented_exit":
        f["status"]["terminal_state"] = {**state, "Result": "timeout"}
    elif fault == "changed_native_state":
        marker["boot_change"]["native_state"]["MainPID"] = "123"
    elif fault == "second_boot":
        monkeypatch.setattr("eimemory.governance.promotion.promotion_watch.native_observation_clock",
            lambda: {"boot_id": "33333333-3333-3333-3333-333333333333"})
    elif isinstance(fault, str) and fault.startswith("independent_"):
        f["status"]["terminal_recovery"] = None
        if fault == "independent_missing_hold": f["status"]["native_hold"] = None
        elif fault == "independent_same_boot": hold["boot_id"] = new_boot
        elif fault == "independent_foreign_hold": hold["original_invocation"] = "f" * 32
        elif fault == "independent_running": state["MainPID"] = "123"
    elif fault in {"garbage_collected", "gc_foreign_invocation", "held_garbage_collected"}:
        if fault != "held_garbage_collected":
            marker["phase"] = "restored"
            f["status"]["resumed"] = True
        marker["boot_change"]["native_state"].update(LoadState="loaded", ActiveState="failed", InvocationID=f["invocation"])
        if fault == "gc_foreign_invocation": state["InvocationID"] = "f" * 32
    result = resume_code_evolution_transactions(f["store"], scope=f["owner"])
    current = f["manager"].store.get_transaction(f["transaction_id"])
    valid = fault in {None, "garbage_collected", "held_garbage_collected", "second_boot"}
    assert current["current_state"] == ("ROLLBACK_INTENT" if valid else "OBSERVING")
    assert f["manager"].store.get_terminal_receipt(f["transaction_id"]) is None
    assert f["status"]["terminal_state"] is None or fault == "invented_exit"
    if fault == "second_boot":
        assert marker["boot_change"]["current_boot_id"] == new_boot
    assert result["reports"][0]["status"] == ("rollback_required" if valid else "managed_failed_job_recovery_unverified")


def test_recovery_accepts_only_exact_commit_and_remote_cas_states() -> None:
    assert reconcile_commit({"candidate_commit": "c", "base_commit": "b", "parent": "b", "tree_matches": True, "transaction_trailer_matches": True}).status == "committed"
    assert reconcile_commit({"candidate_commit": "c", "base_commit": "b", "parent": "x", "tree_matches": True, "transaction_trailer_matches": True}).quarantine_required
    assert reconcile_push({"remote_sha": "c", "candidate_commit": "c", "base_commit": "b"}).status == "pushed"
    assert reconcile_push({"remote_sha": "b", "candidate_commit": "c", "base_commit": "b"}).retry_allowed
    assert reconcile_push({"remote_sha": "x", "candidate_commit": "c", "base_commit": "b"}).quarantine_required


def test_deploy_and_rollback_reconcile_without_blind_replay() -> None:
    deployed = reconcile_deployment({"current_commit": "c", "candidate_commit": "c", "prior_commit": "b", "deployment_receipt_valid": True, "storage_release_marker": "committed", "health_ok": True})
    assert deployed.status == "deployed_verified"
    assert reconcile_deployment({"current_commit": "c", "candidate_commit": "c", "deployment_receipt_valid": False}).rollback_required
    assert reconcile_rollback({"current_commit": "b", "prior_commit": "b", "receipt_valid": True, "health_ok": True, "storage_clean": True}).status == "rolled_back_healthy"


def test_manager_recovers_pending_intent_to_quarantine_on_unknown_state(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="recovery-test")
    try:
        manager.create_detected({
            "transaction_id": "tx-recovery",
            "idempotency_key": "idem-recovery",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-recovery", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        manager.transition("tx-recovery", "DIAGNOSED")
        manager.transition("tx-recovery", "PROVIDER_RESOLVED")
        manager.transition("tx-recovery", "PATCH_PROPOSED")
        manager.transition("tx-recovery", "PATCH_VALIDATED")
        manager.transition("tx-recovery", "CANDIDATE_MATERIALIZED")
        manager.transition("tx-recovery", "FOCUSED_VERIFIED")
        manager.transition("tx-recovery", "REGRESSION_VERIFIED")
        manager.transition("tx-recovery", "FULL_SUITE_VERIFIED")
        manager.transition("tx-recovery", "POLICY_AUTHORIZED")
        manager.begin_intent("tx-recovery", step="commit", intent_state="COMMIT_INTENT")
        result = recover_transaction(manager, "tx-recovery", external_state={"candidate_commit": "c", "base_commit": "b", "parent": "x"})
        assert result["current_state"] == "RECOVERY_QUARANTINED"
    finally:
        runtime_store.close()


def test_resume_reconciles_pending_intent_from_protected_external_reader(tmp_path, monkeypatch) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="recovery-test")
    manager.create_detected({
        "transaction_id": "tx-auto-reconcile",
        "idempotency_key": "idem-auto-reconcile",
        "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        "incident": {"incident_id": "incident-auto-reconcile", "incident_class": "code"},
        "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        "candidate_tree_digest": "b" * 64,
        "candidate_commit": "c" * 40,
    })
    for state in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT"):
        manager.transition("tx-auto-reconcile", state)
    monkeypatch.setattr(
        "eimemory.governance.code_evolution_effects.read_code_evolution_external_state",
        lambda *_args, **_kwargs: {
            "candidate_commit": "c" * 40,
            "base_commit": "a" * 40,
            "parent": "a" * 40,
            "tree_matches": True,
            "transaction_trailer_matches": True,
        },
    )
    try:
        report = resume_code_evolution_transactions(
            runtime_store,
            scope={"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        )
    finally:
        runtime_store.close()

    assert report["reports"][0]["current_state"] == "COMMITTED"
    assert report["reports"][0]["candidate_commit"] == "c" * 40
    assert report["reports"][0]["prior_commit"] == "a" * 40


def test_resume_rejects_caller_supplied_external_recovery_evidence(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    try:
        with pytest.raises(TypeError, match="external_state_by_transaction"):
            resume_code_evolution_transactions(
                runtime_store,
                external_state_by_transaction={"forged": {"candidate_commit": "f" * 40}},
            )
    finally:
        runtime_store.close()


def test_public_observation_rejects_caller_supplied_sample_and_timestamp(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    try:
        with pytest.raises(TypeError, match="sample"):
            observe_live_code_evolution_transaction(
                runtime_store,
                transaction_id="forged-observation",
                sample={"health_ok": True},
                observed_at="2099-01-01T00:00:00+00:00",
            )
    finally:
        runtime_store.close()


def test_recovery_requires_exclusive_transaction_lease(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="recovery-owner")
    holder = CodeEvolutionTransactionManager(runtime_store, owner_id="other-owner")
    try:
        manager.create_detected({
            "transaction_id": "tx-recovery-lease",
            "idempotency_key": "idem-recovery-lease",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-recovery-lease", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT"):
            manager.transition("tx-recovery-lease", target)
        holder.acquire_lease("tx-recovery-lease")

        with pytest.raises(CodeEvolutionConflict, match="lease"):
            recover_transaction(
                manager,
                "tx-recovery-lease",
                external_state={"candidate_commit": "c", "base_commit": "b", "parent": "x"},
            )
        assert manager.store.get_transaction("tx-recovery-lease")["current_state"] == "COMMIT_INTENT"
    finally:
        runtime_store.close()


def test_observation_samples_are_lease_owned_and_idempotent(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="watch-test")
    try:
        manager.create_detected({
            "transaction_id": "tx-observe",
            "idempotency_key": "idem-observe",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-observe", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT", "COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"):
            manager.transition("tx-observe", target)
        sample = {
            "sample_key": "t0",
            "observed_at": "2026-01-01T00:00:00+00:00",
            "commit": "a" * 40,
            "release_identity": "release-a",
            "provider_advertisement_digest": "b" * 64,
            "live_provider_advertisement_digest": "d" * 64,
            "provider_authority_error": "",
            "deployment_receipt_digest": "c" * 64,
            "incident_measure": {"ok": True},
            "health_ok": True,
        }
        manager.update_metadata(
            "tx-observe",
            payload_updates={
                "candidate_pushed_and_deployed": True,
                "deployment_receipt_digest": "c" * 64,
            },
        )
        first = observe_code_evolution_transaction(runtime_store, transaction_id="tx-observe", sample=sample, owner_id="watch-test")
        second = observe_code_evolution_transaction(runtime_store, transaction_id="tx-observe", sample=sample, owner_id="watch-test")
        assert first["status"] == "observing"
        assert second["status"] == "duplicate"
        saved = manager.store.get_transaction("tx-observe")["payload"]["observation_samples"][0]
        assert saved["provider_advertisement_digest"] == "b" * 64
        assert saved["live_provider_advertisement_digest"] == "d" * 64
        events = manager.store.list_step_events("tx-observe")
        assert len([event for event in events if event["step"] == "observation"]) == 1
    finally:
        runtime_store.close()


def test_resume_samples_observing_transaction_from_trusted_runtime_state(tmp_path, monkeypatch) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-auto-observe")
    transaction = manager.store.get_transaction("tx-auto-observe")
    assert transaction is not None
    monkeypatch.setattr(
        "eimemory.governance.code_evolution_effects.sample_code_evolution_observation",
        lambda *_args, **_kwargs: _sample(
            "phase-0",
            observed_at="2026-01-01T00:00:00+00:00",
            health_ok=True,
        ),
    )
    try:
        report = resume_code_evolution_transactions(
            runtime_store,
            scope={"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
        )
    finally:
        runtime_store.close()

    assert report["reports"][0]["status"] == "observing"
    assert report["reports"][0]["sample_key"] == "phase-0"


def test_observation_cannot_sediment_without_deployment_receipt_and_candidate(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = CodeEvolutionTransactionManager(runtime_store, owner_id="watch-test")
    try:
        manager.create_detected({
            "transaction_id": "tx-unproven-observe",
            "idempotency_key": "idem-unproven-observe",
            "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            "incident": {"incident_id": "incident-unproven-observe", "incident_class": "code"},
            "repository": {"root": "/repo", "remote": "origin", "ref": "master", "base_commit": "a" * 40},
        })
        for target in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED", "CANDIDATE_MATERIALIZED", "FOCUSED_VERIFIED", "REGRESSION_VERIFIED", "FULL_SUITE_VERIFIED", "POLICY_AUTHORIZED", "COMMIT_INTENT", "COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"):
            manager.transition("tx-unproven-observe", target)
        result = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-unproven-observe",
            owner_id="watch-test",
            sample={
                "sample_key": "unproven",
                "observed_at": "2026-01-01T00:00:00+00:00",
                "commit": "a" * 40,
                "release_identity": "release-a",
                "provider_advertisement_digest": "b" * 64,
                "deployment_receipt_digest": "c" * 64,
                "incident_measure": {"ok": True},
                "health_ok": True,
            },
        )
        assert result["ok"] is False
        assert result["status"] == "observation_evidence_unproven"
        assert manager.store.get_terminal_receipt("tx-unproven-observe") is None
    finally:
        runtime_store.close()


def test_noncritical_health_degradation_requires_two_consecutive_samples(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-degraded")
    try:
        first = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-degraded",
            owner_id="watch-test",
            sample=_sample("degraded-1", observed_at="2026-01-01T00:00:00+00:00", health_ok=False),
        )
        assert first["status"] == "observing"
        assert manager.store.get_transaction("tx-degraded")["current_state"] == "OBSERVING"

        second = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-degraded",
            owner_id="watch-test",
            sample=_sample("degraded-2", observed_at="2026-01-01T00:15:00+00:00", health_ok=False),
        )
        assert second["status"] == "rollback_required"
        assert second["consecutive_degraded"] is True
        assert manager.store.get_transaction("tx-degraded")["current_state"] == "ROLLBACK_INTENT"
    finally:
        runtime_store.close()


def test_observation_sample_key_conflict_fails_closed(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    _observing_manager(runtime_store, "tx-sample-conflict")
    try:
        first = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-sample-conflict",
            owner_id="watch-test",
            sample=_sample("same-key", observed_at="2026-01-01T00:00:00+00:00", health_ok=True),
        )
        conflict = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-sample-conflict",
            owner_id="watch-test",
            sample=_sample("same-key", observed_at="2026-01-01T00:00:00+00:00", health_ok=True, measure=2),
        )
        assert first["status"] == "observing"
        assert conflict == {
            "ok": False,
            "status": "observation_sample_identity_conflict",
            "transaction_id": "tx-sample-conflict",
            "sample_key": "same-key",
        }
    finally:
        runtime_store.close()


def test_complete_observation_window_appends_and_reconciles_real_outcome_once(tmp_path) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-sedimentation")
    try:
        timestamps = tuple(
            (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)).isoformat()
            for offset in OBSERVATION_OFFSETS
        )
        reports = [
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-sedimentation",
                owner_id="watch-test",
                sample=_sample(f"phase-{index}", observed_at=timestamp, health_ok=True),
            )
            for index, timestamp in enumerate(timestamps)
        ]

        assert reports[-1]["status"] == "succeeded_sedimented"
        assert reports[-1]["sedimentation_record_id"]
        transaction = manager.store.get_transaction("tx-sedimentation")
        assert transaction is not None
        assert transaction["current_state"] == "SUCCEEDED_SEDIMENTED"
        assert bool(transaction["terminal"]) is True
        receipt = manager.store.get_terminal_receipt("tx-sedimentation")
        assert receipt is not None
        assert receipt["outcome"] == "succeeded_sedimented"
        assert receipt["payload"]["sedimentation_record_id"] == reports[-1]["sedimentation_record_id"]
        assert receipt["payload"]["sedimentation_digest"] == reports[-1]["sedimentation_digest"]
        outcomes = runtime_store.list_records(
            kinds=["reflection"],
            scope={"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
            limit=10,
        )
        matching = [record for record in outcomes if record.meta.get("code_evolution_transaction_id") == "tx-sedimentation"]
        assert len(matching) == 1
        assert matching[0].meta["semantic_digest"] == reports[-1]["sedimentation_digest"]
        sedimentation = [
            event
            for event in manager.store.list_step_events("tx-sedimentation")
            if event["step"] == "sedimentation"
        ]
        assert len(sedimentation) == 2
        assert sedimentation[0]["phase"] == "intent"
        assert sedimentation[1]["phase"] == "reconcile"
    finally:
        runtime_store.close()


def test_quarter_hour_watch_retains_all_phases_through_configured_window(tmp_path):
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-quarter-hour")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    try:
        for tick in range(DEFAULT_OBSERVATION_SECONDS // 900 + 1):
            timestamp = (start + timedelta(minutes=15 * tick)).isoformat(timespec="seconds")
            result = observe_code_evolution_transaction(
                runtime_store, transaction_id="tx-quarter-hour", owner_id="watch-test",
                sample=_sample(f"tick-{tick}", observed_at=timestamp, health_ok=True),
            )
            if tick < DEFAULT_OBSERVATION_SECONDS // 900:
                assert result["status"] == "observing"
        assert result["status"] == "succeeded_sedimented"
        payload = manager.store.get_transaction("tx-quarter-hour")["payload"]
        assert len(payload["observation_samples"]) <= 16
        assert payload["observation_samples"][0]["observed_at"] == start.isoformat(timespec="seconds")
        assert len([event for event in manager.store.list_step_events("tx-quarter-hour", limit=2000)
                    if event["step"] == "observation" and event["phase"] == "result"]) == DEFAULT_OBSERVATION_SECONDS // 900 + 1
    finally:
        runtime_store.close()


def test_delayed_first_watch_anchors_phase_zero_without_backdating(tmp_path):
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-delayed-watch")
    try:
        manager.update_metadata(
            "tx-delayed-watch",
            updates={"observation_started_at": "2026-01-01T00:00:00+00:00", "observation_deadline": "2026-01-03T00:00:00+00:00"},
            payload_updates={"observation_anchor_pending": True},
        )
        result = observe_code_evolution_transaction(
            runtime_store, transaction_id="tx-delayed-watch", owner_id="watch-test",
            sample=_sample("first-live", observed_at="2026-01-01T00:20:00+00:00", health_ok=True),
        )
        assert result["status"] == "observing"
        tx = manager.store.get_transaction("tx-delayed-watch")
        assert tx["observation_started_at"] == "2026-01-01T00:20:00+00:00"
        assert tx["observation_deadline"] == "2026-01-03T00:00:00+00:00"
        assert tx["payload"]["observation_effective_deadline"] == tx["observation_deadline"]
        assert tx["payload"]["observation_anchor_pending"] is False
        assert result["required_phases"] == [0]
    finally:
        runtime_store.close()


def test_observation_failure_and_rollback_intent_survive_crash_after_atomic_commit(tmp_path, monkeypatch) -> None:
    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-atomic-rollback")
    original = CodeEvolutionStore.commit_observation_result

    def commit_then_crash(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("simulated_observation_crash")

    monkeypatch.setattr(CodeEvolutionStore, "commit_observation_result", commit_then_crash)
    try:
        with pytest.raises(RuntimeError, match="simulated_observation_crash"):
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-atomic-rollback",
                owner_id="watch-test",
                sample={
                    **_sample(
                        "hard-failure",
                        observed_at="2026-01-01T00:00:00+00:00",
                        health_ok=False,
                    ),
                    "hard_failure": True,
                },
            )
        transaction = manager.store.get_transaction("tx-atomic-rollback")
        assert transaction["current_state"] == "ROLLBACK_INTENT"
        assert transaction["payload"]["observation_failure"] is True
        events = manager.store.list_step_events("tx-atomic-rollback")
        assert [(event["step"], event["phase"]) for event in events[-2:]] == [
            ("observation", "result"),
            ("rollback", "intent"),
        ]
    finally:
        runtime_store.close()


def test_sedimentation_resumes_after_crash_with_atomic_intent(tmp_path, monkeypatch) -> None:
    import eimemory.governance.promotion_watch as promotion_watch_module

    runtime_store = RuntimeStore(tmp_path / "runtime")
    manager = _observing_manager(runtime_store, "tx-atomic-sedimentation")
    timestamps = tuple(
        (datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset)).isoformat()
        for offset in OBSERVATION_OFFSETS
    )
    try:
        for index, timestamp in enumerate(timestamps[:-1]):
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-atomic-sedimentation",
                owner_id="watch-test",
                sample=_sample(f"phase-{index}", observed_at=timestamp, health_ok=True),
            )
        execute_sedimentation = promotion_watch_module._execute_code_evolution_sedimentation
        monkeypatch.setattr(
            promotion_watch_module,
            "_execute_code_evolution_sedimentation",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("simulated_sedimentation_crash")),
        )
        final_sample = _sample(
            "phase-7",
            observed_at=timestamps[-1],
            health_ok=True,
        )
        with pytest.raises(RuntimeError, match="simulated_sedimentation_crash"):
            observe_code_evolution_transaction(
                runtime_store,
                transaction_id="tx-atomic-sedimentation",
                owner_id="watch-test",
                sample=final_sample,
            )
        interrupted = manager.store.get_transaction("tx-atomic-sedimentation")
        assert interrupted["current_state"] == "OBSERVING"
        assert interrupted["payload"]["observation_valid"] is True
        assert any(
            event["step"] == "sedimentation" and event["phase"] == "intent"
            for event in manager.store.list_step_events("tx-atomic-sedimentation")
        )

        monkeypatch.setattr(
            promotion_watch_module,
            "_execute_code_evolution_sedimentation",
            execute_sedimentation,
        )
        retry_sample = {
            **final_sample,
            "observed_at": "2026-01-03T00:15:00+00:00",
        }
        resumed = observe_code_evolution_transaction(
            runtime_store,
            transaction_id="tx-atomic-sedimentation",
            owner_id="watch-test",
            sample=retry_sample,
        )
        assert resumed["status"] == "succeeded_sedimented"
        assert manager.store.get_transaction("tx-atomic-sedimentation")["current_state"] == "SUCCEEDED_SEDIMENTED"
    finally:
        runtime_store.close()
