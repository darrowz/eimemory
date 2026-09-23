"""Unit tests of the exact complete() AST; no Hermes import or network access.

BridgeTrace, clocks, and provider objects are explicit test doubles. These tests
are not a production bridge or real-provider latency/stability acceptance.
"""
import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(os.environ.get('LUNA_BUDGET_TEST_SOURCE', str(
    ROOT / 'luna_review_command.py')))
MODEL = 'gpt-5.6-luna'


class Clock:
    def __init__(self):
        self.wall = 1000.0
        self.mono = 2000.0

    def time(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, duration, wall_jump=0):
        self.wall += duration + wall_jump
        self.mono += duration


class Trace:
    def __init__(self):
        self.events = []

    @contextmanager
    def stage(self, name):
        self.events.append(name)
        yield

    def begin_response_validation(self):
        self.events.append('validation')


def build(*, setup_seconds=0, wall_jump=0, response=None,
          resolved_model=MODEL, unavailable=False, api_error=None):
    tree = ast.parse(SOURCE.read_text())
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == 'complete')
    compiled = compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                       str(SOURCE), 'exec')
    clock, trace, requests, setups = Clock(), Trace(), [], []
    if response is None:
        response = SimpleNamespace(model=MODEL, choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"selected":[]}', tool_calls=None))])

    def create(**kwargs):
        requests.append(kwargs)
        if api_error is not None:
            raise api_error
        return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    def resolve(provider, *, model):
        setups.append((provider, model))
        clock.advance(setup_seconds, wall_jump)
        return (None if unavailable else client), resolved_model

    namespace = {'time': clock, 'json': json, '_luna_trace': trace,
                 'resolve_provider_client': resolve}
    exec(compiled, namespace)
    request = dict(system_prompt='UNCHANGED SYSTEM', user_prompt='UNCHANGED USER',
                   json_mode=True, deadline_unix_ms=(clock.wall + 9) * 1000,
                   provider='openai-codex', model=MODEL, reasoning_effort='low')
    return namespace['complete'], request, clock, trace, requests, setups


def test_setup_time_is_deducted():
    complete, request, _, _, calls, _ = build(setup_seconds=2.5)
    complete(request)
    assert calls[0]['timeout'] == pytest.approx(6.5)


@pytest.mark.parametrize('setup', [9.0, 12.0])
def test_no_provider_call_after_setup_consumes_budget(setup):
    complete, request, _, trace, calls, _ = build(setup_seconds=setup)
    with pytest.raises(ValueError, match='deadline_expired'):
        complete(request)
    assert not calls
    assert 'provider_response_ms' not in trace.events  # Unmeasured stays absent.


@pytest.mark.parametrize('jump', [-1000.0, 1000.0])
def test_wall_clock_jump_does_not_refresh_monotonic_budget(jump):
    complete, request, _, _, calls, _ = build(setup_seconds=1.5, wall_jump=jump)
    complete(request)
    assert calls[0]['timeout'] == pytest.approx(7.5)


def test_expired_at_entry_does_not_initialize_client():
    complete, request, clock, _, calls, setups = build()
    request['deadline_unix_ms'] = clock.wall * 1000
    with pytest.raises(ValueError, match='deadline_expired'):
        complete(request)
    assert not calls and not setups


def test_model_reasoning_messages_and_two_layer_response_unchanged():
    complete, request, _, trace, calls, setups = build(setup_seconds=0.5)
    result = complete(request)
    assert setups == [('openai-codex', MODEL)]
    assert len(calls) == 1
    assert calls[0] == dict(model=MODEL, messages=[
        {'role':'system', 'content':'UNCHANGED SYSTEM'},
        {'role':'user', 'content':'UNCHANGED USER'}], reasoning_effort='low', timeout=8.5)
    assert result == {'text':'{"selected":[]}', 'model_id':MODEL, 'provider_id':'openai-codex'}
    assert trace.events == ['bridge_client_setup_ms', 'provider_response_ms', 'validation']


def test_original_cap_not_raised():
    complete, request, clock, _, calls, _ = build(setup_seconds=1)
    request['deadline_unix_ms'] = (clock.wall + 120) * 1000
    complete(request)
    assert calls[0]['timeout'] == 90


def test_original_missing_deadline_fallback_is_not_refreshed_after_setup():
    complete, request, _, _, calls, _ = build(setup_seconds=2)
    del request['deadline_unix_ms']
    complete(request)
    assert calls[0]['timeout'] == pytest.approx(88)


@pytest.mark.parametrize('kwargs', [{'unavailable':True}, {'resolved_model':'OTHER'}])
def test_invalid_client_or_model_fails_before_api(kwargs):
    complete, request, _, _, calls, _ = build(**kwargs)
    with pytest.raises(RuntimeError, match='model_unavailable'):
        complete(request)
    assert not calls


@pytest.mark.parametrize('mode', ['model', 'tool_call', 'empty', 'json'])
def test_response_validation_is_preserved(mode):
    response = SimpleNamespace(model=MODEL, choices=[SimpleNamespace(
        message=SimpleNamespace(content='{"selected":[]}', tool_calls=None))])
    if mode == 'model':
        response.model = 'OTHER'
    elif mode == 'tool_call':
        response.choices[0].message.tool_calls = [object()]
    elif mode == 'empty':
        response.choices[0].message.content = ' '
    else:
        response.choices[0].message.content = 'not json'
    complete, request, _, _, calls, _ = build(response=response)
    with pytest.raises((ValueError, RuntimeError)):
        complete(request)
    assert len(calls) == 1


@pytest.mark.parametrize('error', [TimeoutError('fake'), OSError('fake')])
def test_provider_errors_are_not_converted_to_empty_success(error):
    complete, request, _, _, calls, _ = build(api_error=error)
    with pytest.raises(type(error)):
        complete(request)
    assert len(calls) == 1


@pytest.mark.parametrize('key,value', [('system_prompt',None), ('user_prompt',123),
                                     ('user_prompt','x' * 131073)])
def test_prompt_validation_still_precedes_setup(key, value):
    complete, request, _, _, calls, setups = build()
    request[key] = value
    with pytest.raises(ValueError):
        complete(request)
    assert not setups and not calls


def test_missing_provider_or_model_is_unavailable():
    complete, request, _, _, calls, setups = build()
    request.pop('provider')
    request.pop('model')
    with pytest.raises(RuntimeError, match='model_unavailable'):
        complete(request)
    assert not calls and not setups


def test_environment_configures_provider_model_and_reasoning(monkeypatch):
    complete, request, _, _, calls, _ = build(setup_seconds=0.5)
    for key in ('provider', 'model', 'reasoning_effort'):
        request.pop(key)
    monkeypatch.setenv('EIMEMORY_LUNA_PROVIDER', 'configured-provider')
    monkeypatch.setenv('EIMEMORY_RECALL_EXPECTED_MODEL', MODEL)
    monkeypatch.setenv('EIMEMORY_LUNA_REASONING_EFFORT', 'medium')
    result = complete(request)
    assert calls[0]['model'] == MODEL
    assert calls[0]['reasoning_effort'] == 'medium'
    assert result['provider_id'] == 'configured-provider'


def test_hermes_agent_root_prefers_explicit_env(tmp_path, monkeypatch):
    import os
    tree = ast.parse(SOURCE.read_text())
    function = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == '_hermes_agent_root')
    compiled = compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                       str(SOURCE), 'exec')
    namespace = {}
    exec(compiled, namespace)
    explicit = tmp_path / 'agent'
    explicit.mkdir()
    monkeypatch.setenv('EIMEMORY_HERMES_AGENT_ROOT', str(explicit))
    assert namespace['_hermes_agent_root']() == explicit
    monkeypatch.delenv('EIMEMORY_HERMES_AGENT_ROOT')
    home = tmp_path / 'hermes-home'
    (home / 'hermes-agent').mkdir(parents=True)
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.delenv('EIMEMORY_HERMES_HOME', raising=False)
    assert namespace['_hermes_agent_root']() == home / 'hermes-agent'
    assert 'darrow' not in str(namespace['_hermes_agent_root']()).split(os.sep)


def test_existing_api_call_and_return_ast_are_unchanged():
    def relevant(path):
        tree = ast.parse(path.read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == 'complete')
        call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == 'create')
        ret = next(n for n in ast.walk(fn) if isinstance(n, ast.Return))
        return ast.dump(call), ast.dump(ret)
    assert relevant(SOURCE) == relevant(ROOT / 'tests/fixtures/luna_budget_baseline.py')
