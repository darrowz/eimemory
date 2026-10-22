from copy import deepcopy
import pytest

from eimemory.core import nightly_deployment_admission as admission


KEY = "a" * 64
INVOCATION = "b" * 32


@pytest.fixture
def native(monkeypatch, tmp_path):
    transition = {"complete": True, "job_key": KEY}
    state = dict(LoadState="loaded", ActiveState="active", SubState="exited", Result="success",
        InvocationID=INVOCATION, ExecMainStatus="0", MainPID="0", ControlGroup="",
        ExecMainExitTimestampMonotonic="123")
    result = dict(job_key=KEY, manager="user", unit=admission.contract.job_unit(KEY),
        original_invocation=INVOCATION, phase="completed", complete=True,
        terminal_state=state, state=dict(state))
    calls = []
    monkeypatch.setattr(admission.user, "user_mode", lambda: True)
    monkeypatch.setattr(admission.user, "coordinates", lambda: {"state": tmp_path})
    monkeypatch.setattr(admission.manager, "read_registry", lambda: {})
    monkeypatch.setattr(admission.os.path, "lexists", lambda _: True)
    monkeypatch.setattr(admission.user, "read_file", lambda _: admission.contract.canonical(transition))
    monkeypatch.setattr(admission.manager, "deployment_job_status", lambda _: deepcopy(result))
    def request(operation, **kwargs):
        calls.append((operation, kwargs))
        return {"job_key": KEY, "release_requested": True}
    monkeypatch.setattr(admission.manager, "request", request)
    return transition, result, calls


def test_waits_for_actual_worker_exit_then_recovers_observers(native, monkeypatch):
    transition, result, calls = native
    terminal = result.pop("terminal_state")
    result.update(complete=False, phase="resumed_awaiting_business")
    transition["complete"] = False
    sleeps = []
    def advance(seconds):
        assert not calls
        sleeps.append(seconds)
        result.update(terminal_state=terminal, complete=True, phase="completed")
        transition["complete"] = True
    monkeypatch.setattr(admission.time, "sleep", advance)
    observed = admission.wait_for_deployment_terminal()
    assert len(sleeps) == 1 and observed["worker_succeeded"] is True
    assert calls == [("release_job", {"job_key": KEY})]


def test_failed_worker_can_be_diagnosed_without_observer_restart_or_acceptance(native):
    transition, result, calls = native
    transition["complete"] = False
    result.update(complete=False, phase="resumed_awaiting_business")
    for field in ("state", "terminal_state"):
        result[field].update(ActiveState="failed", SubState="failed", Result="exit-code", ExecMainStatus="1")
    observed = admission.wait_for_deployment_terminal()
    assert observed["worker_succeeded"] is False and observed["release_complete"] is False
    assert calls == []


@pytest.mark.parametrize("fault", ["owner", "job", "unit", "invocation", "clock", "live",
    "completion", "phase", "transition", "transition_flag"])
def test_rejects_unbound_or_incomplete_release_evidence(native, fault):
    transition, result, calls = native
    if fault == "owner": result["manager"] = "system"
    elif fault == "job": result["job_key"] = "c" * 64
    elif fault == "unit": result["unit"] = "eimemory-nightly.service"
    elif fault == "invocation": result["original_invocation"] = "c" * 32
    elif fault == "clock": result["terminal_state"]["ExecMainExitTimestampMonotonic"] = "0"
    elif fault == "live": result["state"]["MainPID"] = "123"
    elif fault == "completion": result["complete"] = False
    elif fault == "phase": result["phase"] = "resumed_awaiting_business"
    elif fault == "transition": transition["complete"] = False
    elif fault == "transition_flag": transition["complete"] = 1
    with pytest.raises(ValueError):
        admission.wait_for_deployment_terminal()
    assert not calls


def test_pending_deployment_timeout_never_opens_admission(native):
    _, result, calls = native
    result["terminal_state"] = None
    with pytest.raises(ValueError, match="terminal_wait_expired"):
        admission.wait_for_deployment_terminal(timeout_seconds=0)
    assert not calls


@pytest.mark.parametrize("when", ["before_release", "after_release"])
def test_transition_replacement_cannot_be_admitted(native, monkeypatch, when):
    transition, _, calls = native
    raw = admission.contract.canonical(transition)
    reads = iter([raw, b"changed"] if when == "before_release" else [raw, raw, b"changed"])
    monkeypatch.setattr(admission.user, "read_file", lambda _: next(reads))
    with pytest.raises(ValueError, match="transition_changed"):
        admission.wait_for_deployment_terminal()
    assert len(calls) == (1 if when == "after_release" else 0)


def test_unmanaged_local_nightly_does_not_contact_native_controller(monkeypatch):
    monkeypatch.setattr(admission.user, "user_mode", lambda: False)
    monkeypatch.setattr(admission.manager, "read_registry", lambda: pytest.fail("local controller lookup"))
    assert admission.wait_for_deployment_terminal()["status"] == "not_user_managed"


def test_registered_node_without_a_deployment_can_start(native, monkeypatch):
    monkeypatch.setattr(admission.os.path, "lexists", lambda _: False)
    assert admission.wait_for_deployment_terminal()["status"] == "no_deployment_transition"


def test_cli_blocks_before_settings_and_runtime_construction(monkeypatch, capsys):
    import importlib
    cli = importlib.import_module("eimemory.cli.main")
    def reject():
        raise ValueError("nightly_deployment_terminal_wait_expired")
    monkeypatch.setattr(admission, "wait_for_deployment_terminal", reject)
    monkeypatch.setattr(cli, "load_settings", lambda: pytest.fail("settings loaded before admission"))
    monkeypatch.setattr(cli.Runtime, "create", lambda **_: pytest.fail("database opened before admission"))
    assert cli.main(["nightly"]) == 2
    assert "nightly_deployment_not_terminal" in capsys.readouterr().out


def test_cli_passes_failed_terminal_to_scheduler(monkeypatch):
    import importlib
    from types import SimpleNamespace
    cli = importlib.import_module("eimemory.cli.main")
    failed = dict(status="terminal", job_key=KEY, worker_succeeded=False, release_complete=False)
    class Captured(BaseException):
        pass
    def run(runtime, *, scope, deployment_admission):
        assert deployment_admission == failed
        raise Captured
    monkeypatch.setattr(cli, "run_nightly_jobs", run)
    with pytest.raises(Captured):
        cli._cmd_nightly(SimpleNamespace(_nightly_deployment_admission=failed), object(), {})


@pytest.mark.parametrize("success", [False, True])
def test_scheduler_records_worker_outcome_before_business_steps(monkeypatch, success):
    from eimemory.scheduler import jobs
    original = jobs._nightly_step
    observed = []
    class Captured(BaseException):
        pass
    def step(steps, name, call):
        if name == "roi":
            observed.extend(steps)
            raise Captured
        return original(steps, name, call)
    monkeypatch.setattr(jobs, "_nightly_step", step)
    with pytest.raises(Captured):
        jobs.run_nightly_jobs(object(), scope={}, deployment_admission=dict(
            status="terminal", job_key=KEY, worker_succeeded=success, release_complete=success))
    assert observed[0]["step"] == "deployment_admission"
    assert observed[0]["ok"] is success
    assert jobs._aggregate_nightly_ok({}, observed) is success


@pytest.fixture
def boot_interrupted(native, monkeypatch, tmp_path):
    transition, result, calls = native
    contract = admission.contract
    old_boot = "11111111-1111-1111-1111-111111111111"
    new_boot = "22222222-2222-2222-2222-222222222222"
    commit, controller = "c" * 40, "d" * 40
    target = tmp_path / "releases" / commit
    target.mkdir(parents=True)
    registry = dict(generation="e" * 64, runtime_commit=commit, controller_commit=controller)
    plan = {"active_units": ["eimemory-rpc.service"], "enabled_units": {}}
    unit = contract.job_unit(KEY)
    group = "/system.slice/" + unit
    if contract.USER_MODE:
        uid = str(contract._user_coordinates["uid"])
        group = "/user.slice/user-" + uid + ".slice/user@" + uid + ".service/app.slice/" + unit
    hold = dict(schema="eimemory.native_deployment_hold.v1", job_key=KEY, original_invocation=INVOCATION,
        boot_id=old_boot, registry_generation="f" * 64, restore_plan_sha256=contract.digest(plan),
        native_owner=dict(ActiveState="activating", InvocationID=INVOCATION, ControlGroup=group), peer_pid=123)
    state = {**result["state"], "LoadState": "not-found", "ActiveState": "inactive", "SubState": "dead",
        "InvocationID": "", "ControlPID": "0"}
    recovery = dict(schema="eimemory.native_failed_job_restore.v1", job_key=KEY,
        original_invocation=INVOCATION, original_phase="serving_awaiting_business", outcome="failed",
        failure_reason="worker_interrupted_by_boot_change", failure_kind="boot_change", native_exit=None,
        boot_change=dict(schema="eimemory.native_job_boot_interruption.v1", hold_sha256=contract.digest(hold),
            previous_boot_id=old_boot, current_boot_id=new_boot, native_state=deepcopy(state)),
        registry_generation=registry["generation"], current_commit=commit, controller_commit=controller,
        restore_plan_sha256=contract.digest(plan), phase="restored", autonomous_repair_credit=False)
    result.update(phase="resumed_awaiting_business", complete=False, resumed=True, background_held=False,
        held_background=[], terminal_state=None, state=state, native_hold=hold, restore_plan=plan,
        launch_registry_generation="f" * 64, terminal_recovery=recovery,
        request=dict(operation="deploy", base_commit="a" * 40, candidate_commit=commit))
    transition["complete"] = False
    monkeypatch.setattr(admission.manager, "read_registry", lambda: deepcopy(registry))
    monkeypatch.setattr(admission, "CURRENT_LINK", target)
    monkeypatch.setattr(admission, "RELEASES", target.parent)
    monkeypatch.setattr(admission, "_current_boot_id", lambda: new_boot)
    return dict(transition=transition, result=result, calls=calls, registry=registry,
        target=target, old_boot=old_boot, new_boot=new_boot)


def test_restored_real_boot_interruption_admits_only_failed_diagnostics(boot_interrupted):
    f = boot_interrupted
    observed = admission.wait_for_deployment_terminal(timeout_seconds=0)
    assert observed == dict(status="terminal", job_key=KEY, worker_succeeded=False, release_complete=False,
        worker_terminal_kind="boot_interruption", native_exit_observed=False, autonomous_repair_credit=False)
    assert f["result"]["terminal_state"] is None
    assert f["calls"] == []


def test_diagnostic_admission_retains_original_proof_after_a_second_reboot(boot_interrupted, monkeypatch):
    f = boot_interrupted
    original = admission.contract.canonical(f["result"]["terminal_recovery"])
    monkeypatch.setattr(admission, "_current_boot_id", lambda: "33333333-3333-3333-3333-333333333333")
    observed = admission.wait_for_deployment_terminal(timeout_seconds=0)
    assert observed["worker_succeeded"] is False and observed["native_exit_observed"] is False
    assert admission.contract.canonical(f["result"]["terminal_recovery"]) == original
    assert f["result"]["terminal_state"] is None and not f["calls"]


@pytest.mark.parametrize("at_base", [True, False])
def test_rollback_boot_diagnostics_require_the_verified_base_runtime(boot_interrupted, at_base):
    f = boot_interrupted
    f["result"]["request"]["operation"] = "rollback"
    if at_base:
        f["result"]["request"]["base_commit"] = f["target"].name
    if at_base:
        observed = admission.wait_for_deployment_terminal(timeout_seconds=0)
        assert observed["worker_succeeded"] is False and observed["release_complete"] is False
        assert observed["native_exit_observed"] is False
    else:
        with pytest.raises(ValueError, match="boot_failure_unverified"):
            admission.wait_for_deployment_terminal(timeout_seconds=0)
    assert f["result"]["terminal_state"] is None and not f["calls"]


@pytest.mark.parametrize("fault", [None, "new_invocation", "running_pid", "control_pid", "cgroup"])
def test_sealed_boot_proof_survives_only_native_unit_gc(boot_interrupted, fault):
    f = boot_interrupted
    proof = f["result"]["terminal_recovery"]["boot_change"]
    proof["native_state"].update(LoadState="loaded", ActiveState="failed", InvocationID=INVOCATION)
    if fault == "new_invocation": f["result"]["state"]["InvocationID"] = "0" * 32
    elif fault == "running_pid": f["result"]["state"]["MainPID"] = "123"
    elif fault == "control_pid": f["result"]["state"]["ControlPID"] = "123"
    elif fault == "cgroup": f["result"]["state"]["ControlGroup"] = "/foreign.slice/foreign.service"
    if fault is None:
        observed = admission.wait_for_deployment_terminal(timeout_seconds=0)
        assert observed["worker_succeeded"] is False and observed["native_exit_observed"] is False
    else:
        with pytest.raises(ValueError):
            admission.wait_for_deployment_terminal(timeout_seconds=0)
    assert f["result"]["terminal_state"] is None and not f["calls"]


@pytest.mark.parametrize("fault", ["same_boot", "foreign_boot", "missing_hold", "changed_hold",
    "launch_generation", "changed_plan", "current_commit", "foreign_request", "registry", "controller",
    "live_worker", "control_pid", "foreign_invocation", "extra_field", "credit", "not_resumed", "still_held",
    "complete", "transition", "not_restored"])
def test_boot_diagnostic_admission_requires_the_original_native_hold_and_restoration(boot_interrupted, monkeypatch, fault):
    f = boot_interrupted
    result = f["result"]
    marker = result["terminal_recovery"]
    if fault == "same_boot": marker["boot_change"]["previous_boot_id"] = f["new_boot"]
    elif fault == "foreign_boot": monkeypatch.setattr(admission, "_current_boot_id", lambda: f["old_boot"])
    elif fault == "missing_hold": result["native_hold"] = None
    elif fault == "changed_hold": result["native_hold"]["peer_pid"] = 999
    elif fault == "launch_generation": result["launch_registry_generation"] = "0" * 64
    elif fault == "changed_plan": result["restore_plan"]["active_units"].append("foreign.service")
    elif fault == "current_commit": marker["current_commit"] = "0" * 40
    elif fault == "foreign_request": result["request"]["candidate_commit"] = "0" * 40
    elif fault == "registry": marker["registry_generation"] = "0" * 64
    elif fault == "controller": marker["controller_commit"] = "0" * 40
    elif fault == "live_worker": result["state"]["MainPID"] = "123"
    elif fault == "control_pid": result["state"]["ControlPID"] = "123"
    elif fault == "foreign_invocation": result["original_invocation"] = "0" * 32
    elif fault == "extra_field": marker["caller_says_terminal"] = True
    elif fault == "credit": marker["autonomous_repair_credit"] = True
    elif fault == "not_resumed": result["resumed"] = False
    elif fault == "still_held": result["held_background"] = ["eimemory-nightly.timer"]
    elif fault == "complete": result["complete"] = True
    elif fault == "transition": f["transition"]["complete"] = True
    elif fault == "not_restored": marker["phase"] = "restoring"
    with pytest.raises(ValueError):
        admission.wait_for_deployment_terminal(timeout_seconds=0)
    assert f["result"]["terminal_state"] is None
    assert not f["calls"]


@pytest.mark.parametrize("changed", ["pointer", "registry", "boot"])
def test_boot_diagnostic_context_is_rechecked_before_admission(boot_interrupted, monkeypatch, changed):
    f = boot_interrupted
    if changed == "pointer":
        raw = admission.contract.canonical(f["transition"])
        values = iter([raw, b'{}'])
        monkeypatch.setattr(admission.user, "read_file", lambda _: next(values))
    elif changed == "registry":
        count = []
        def registry():
            count.append(1)
            return f["registry"] if len(count) < 3 else {**f["registry"], "generation": "0" * 64}
        monkeypatch.setattr(admission.manager, "read_registry", registry)
    elif changed == "boot":
        values = iter([f["new_boot"], f["old_boot"]])
        monkeypatch.setattr(admission, "_current_boot_id", lambda: next(values))
    with pytest.raises(ValueError, match="changed"):
        admission.wait_for_deployment_terminal(timeout_seconds=0)
    assert not f["calls"]
