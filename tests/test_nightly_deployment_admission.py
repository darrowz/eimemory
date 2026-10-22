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
