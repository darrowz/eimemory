"""Local reproductions of the nightly wrapper and Hermes routing gaps."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.scheduler.jobs import _run_dynamic_capability_evolution
from eimemory.scheduler.result_contract import _nightly_step


@pytest.mark.parametrize('ok', [True, False, None, 'true', 1, 0, 'missing'])
def test_dynamic_wrapper_preserves_executor_verdict(monkeypatch, ok):
    monkeypatch.setenv('EIMEMORY_DYNAMIC_CAPABILITY_EVOLUTION_ENABLED', '1')
    execution = {'ok': ok, 'results': [{'status': 'evaluated' if ok is True else 'blocked'}]}
    if ok == 'missing':
        execution.pop('ok')
    runtime = SimpleNamespace(execute_dynamic_capability_evolution=lambda **_: execution)
    steps = []
    report = _nightly_step(steps, 'dynamic_capability_evolution',
                          lambda: _run_dynamic_capability_evolution(runtime, scope={}))
    assert report['execution'] == execution
    assert report['ok'] is (ok is True)
    assert steps[0]['ok'] is (ok is True)
    assert steps[0]['error'] != 'step_ok_missing'


def hook_module():
    path = Path(__file__).parents[1] / 'integrations/hermes/eimemory_hook/__init__.py'
    spec = importlib.util.spec_from_file_location('incident_hermes_hook', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_task_ownership_exists_before_socket_start_even_on_rediscovery(monkeypatch):
    module = hook_module()
    owned = set()
    servers = []
    hooks = []

    class Server:
        socket_path = '/unused/test.sock'

        def __init__(self, ctx):
            servers.append(self)

        def start(self):
            assert module.FIXED_COMPLETION_TASK in owned
            return len(servers) == 1

        def stop(self):
            pytest.fail('rediscovery must not stop the live provider')

    monkeypatch.setattr(module, 'CodeImplementationSocketServer', Server)
    ctx = SimpleNamespace(
        register_auxiliary_task=lambda key, **_: owned.add(key),
        register_hook=lambda name, callback: hooks.append((name, callback)),
    )
    assert module.register_code_implementation_task(ctx) is not None
    assert ctx.eimemory_code_implementation_server is servers[0]
    # A new discovery registry must regain ownership even with an occupied socket.
    owned.clear()
    assert module.register_code_implementation_task(ctx) is None
    assert module.FIXED_COMPLETION_TASK in owned
    assert ctx.eimemory_code_implementation_server is servers[0]
    assert len(hooks) == 1
    assert hooks[0][0] == 'shutdown'


@pytest.mark.parametrize('register_aux', [None, False])

def test_host_without_task_registration_cannot_publish_provider_socket(monkeypatch, register_aux):
    module = hook_module()

    class Server:
        def __init__(self, ctx):
            pass

        def start(self):
            pytest.fail('must not expose a provider without task ownership')

    monkeypatch.setattr(module, 'CodeImplementationSocketServer', Server)
    assert module.register_code_implementation_task(
        SimpleNamespace(register_auxiliary_task=register_aux)
    ) is None


def test_socket_start_exception_cleans_up_without_claiming_context(monkeypatch):
    module = hook_module()
    events = []
    previous = object()

    class Server:
        def __init__(self, ctx):
            pass

        def start(self):
            events.append("start")
            raise RuntimeError("thread start failed")

        def stop(self):
            events.append("stop")

    monkeypatch.setattr(module, "CodeImplementationSocketServer", Server)
    ctx = SimpleNamespace(
        register_auxiliary_task=lambda *a, **kw: events.append("register"),
        register_hook=lambda *a, **kw: events.append("hook"),
        eimemory_code_implementation_server=previous,
    )
    with pytest.raises(RuntimeError, match="thread start failed"):
        module.register_code_implementation_task(ctx)
    assert events == ["register", "start", "stop"]
    assert ctx.eimemory_code_implementation_server is previous


def test_registration_rejection_never_constructs_socket(monkeypatch):
    module = hook_module()

    def reject(*args, **kwargs):
        raise ValueError("task belongs to another plugin")

    def construct(ctx):
        pytest.fail("registration failure must not publish a socket")

    monkeypatch.setattr(module, "CodeImplementationSocketServer", construct)
    with pytest.raises(ValueError, match="another plugin"):
        module.register_code_implementation_task(SimpleNamespace(register_auxiliary_task=reject))


SENSITIVE = 'SECRET_SENTINEL_patch_command_exception'
REASON = 'code_implementation_v2_provider_context_required'


@pytest.mark.parametrize('execution, expected_reasons, count', [
    ({'ok': False, 'results': [{'status': 'blocked', 'reason': REASON,
                              'patch': SENSITIVE, 'command': SENSITIVE,
                              'detail': SENSITIVE}]}, {REASON: 1}, 1),
    ({'ok': False, 'results': []}, {'execution_results_empty': 1}, 0),
    ({'ok': False, 'results': [{'status': 'blocked', 'hypothesis_gate':
                              {'allowed': False, 'detail': SENSITIVE}}]},
     {'reason_not_reported': 1}, 1),
    ({'ok': False, 'reason': REASON, 'detail': SENSITIVE}, {REASON: 1}, 0),
    ({'ok': False, 'results': [{'status': 'blocked', 'reason': SENSITIVE}]},
     {'reason_not_allowlisted': 1}, 1),
    ({'ok': False, 'results': [{'status': 'blocked', 'reason': REASON + ':' + SENSITIVE}]},
     {'reason_not_allowlisted': 1}, 1),
    ({'ok': False, 'results': [{'status': 'blocked', 'reason': {'detail': SENSITIVE}}]},
     {'reason_not_allowlisted': 1}, 1),
    (None, {'dynamic_capability_evolution_invalid_execution': 1}, 0),
    (RuntimeError(SENSITIVE), {'dynamic_capability_evolution_execution_failed': 1}, 0),
])
def test_dynamic_failure_diagnostics_survive_cli_and_storage(
        tmp_path, monkeypatch, capsys, execution, expected_reasons, count):
    import json
    from eimemory.api.runtime import Runtime
    from eimemory.cli import main as cli
    from eimemory.governance.learning.supervisor import build_supervisor_contract

    runtime = Runtime.create(root=tmp_path / 'runtime')

    def execute(**kwargs):
        if isinstance(execution, Exception):
            raise execution
        return execution

    monkeypatch.setenv('EIMEMORY_DYNAMIC_CAPABILITY_EVOLUTION_ENABLED', '1')
    monkeypatch.setattr(runtime, 'execute_dynamic_capability_evolution', execute)
    monkeypatch.setattr(cli, 'repair_hongtu_identity', lambda *a, **kw: {'ok': True})
    try:
        assert cli._cmd_nightly(SimpleNamespace(), runtime, {}) == 1
        output = json.loads(capsys.readouterr().out)
        stored = build_supervisor_contract(runtime, scope={})['runs']['nightly']
        assert output['ok'] is stored['ok'] is False
        diagnostics = stored['nightly_diagnostics']
        assert output['supervisor_summary']['nightly_diagnostics'] == diagnostics
        assert 'dynamic_capability_evolution' in diagnostics['failed_steps']
        dynamic = diagnostics['dynamic_capability_evolution']
        assert dynamic['reason_counts'] == expected_reasons
        assert dynamic['result_count'] == count
        assert dynamic['results_truncated'] is False
        assert SENSITIVE not in json.dumps(output)
        records = runtime.store.list_records(kinds=['reflection'], scope={}, limit=500)
        persisted = [r for r in records if r.meta.get('report_type') == 'supervisor_run']
        assert persisted and persisted[0].status == 'failed'
        assert persisted[0].content['nightly_diagnostics'] == diagnostics
        assert SENSITIVE not in json.dumps(persisted[0].content)
    finally:
        runtime.close()


def test_dynamic_diagnostics_are_bounded_and_do_not_mutate_verdict():
    from copy import deepcopy
    from eimemory.scheduler.result_contract import nightly_result_diagnostics

    report = {'dynamic_capability_evolution': {'ok': False, 'execution': {
        'ok': False, 'results': [{'status': 'blocked', 'reason': REASON,
                                 'detail': SENSITIVE}] * 600}}}
    before = deepcopy(report)
    diagnostics = nightly_result_diagnostics(report, [])
    assert diagnostics['execution_ok'] is False
    assert diagnostics['dynamic_capability_evolution'] == {
        'reason_counts': {REASON: 500}, 'result_count': 500, 'results_truncated': True}
    assert report == before
    assert SENSITIVE not in str(diagnostics)


def test_dynamic_missing_report_does_not_invent_cause():
    from eimemory.scheduler.result_contract import nightly_result_diagnostics

    diagnostics = nightly_result_diagnostics({}, [{'step': 'dynamic_capability_evolution',
                                                   'ok': False, 'error': SENSITIVE}])
    assert diagnostics['execution_ok'] is False
    assert diagnostics['dynamic_capability_evolution']['reason_counts'] == {
        'execution_report_missing': 1}
    assert SENSITIVE not in str(diagnostics)


def test_dynamic_success_does_not_gain_failure_diagnostics():
    from eimemory.scheduler.result_contract import nightly_result_diagnostics

    diagnostics = nightly_result_diagnostics({'dynamic_capability_evolution': {
        'ok': True, 'execution': {'ok': True, 'results': []}}}, [])
    assert diagnostics['execution_ok'] is True
    assert diagnostics['failed_steps'] == []
    assert 'dynamic_capability_evolution' not in diagnostics
