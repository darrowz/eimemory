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
