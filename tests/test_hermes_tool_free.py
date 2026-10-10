import json

import pytest

from eimemory.llm import hermes_tool_free as bridge
from eimemory.llm import research_review  # Import before patching its dependency: avoid retaining a fake at module load.


def launcher_argv():
    return ['/installed/python', '-I', '-c',
            "import runpy; runpy.run_module('runpy', run_name='__main__', alter_sys=True)"]


@pytest.fixture
def installed_launcher(tmp_path, monkeypatch):
    binary = tmp_path / 'hermes'
    binary.write_text('#!/bin/sh\nexit 1\n')
    binary.chmod(0o700)
    monkeypatch.setenv('EIMEMORY_HERMES_BIN', str(binary))


class FakeAgent:
    tools = []
    called = False
    closed = False
    response = dict(completed=True, failed=False,
                    messages=[{'role': 'assistant', 'content': 'answer'}], final_response='answer')

    def __init__(self, **kwargs):
        assert kwargs == dict(model='configured-model', enabled_toolsets=[], disabled_toolsets=['*'],
                              quiet_mode=True, skip_memory=True, skip_context_files=True,
                              save_trajectories=False, max_iterations=1, skip_background_review=True)

    def run_conversation(self, user, *, system_message):
        assert self.tools == []
        assert self._persist_disabled is True
        assert (user, system_message) == ('private query', 'system')
        type(self).called = True
        return self.response

    def close(self):
        type(self).closed = True


def definitions(**kwargs):
    assert kwargs == dict(enabled_toolsets=[], disabled_toolsets=['*'], quiet_mode=True)
    return []


def test_agent_configured_default_no_tools():
    assert bridge.run_agent(FakeAgent, definitions, 'system', 'private query', runtime={'model': 'configured-model'}) == 'answer'
    assert FakeAgent.closed


@pytest.mark.parametrize('response', [None, {}, {'final_response': 'answer'},
    {**FakeAgent.response, 'completed': False},
    {**FakeAgent.response, 'messages': []},
    {**FakeAgent.response, 'tool_calls': [{'name': 'terminal'}]},
    {**FakeAgent.response, 'messages': [{'role': 'assistant', 'tool_calls': [{'name': 'terminal'}]}]},
    {**FakeAgent.response, 'messages': [{'role': 'tool', 'content': 'ran'}]},
    {**FakeAgent.response, 'messages': [{'content': [{'type': 'tool_use'}]}]},
])
def test_malformed_or_tool_results_rejected(response):
    class Agent(FakeAgent):
        pass
    Agent.response = response
    with pytest.raises(RuntimeError, match='proof_failed'):
        bridge.run_agent(Agent, definitions, 'system', 'private query', runtime={'model': 'configured-model'})
    assert Agent.closed


def test_tool_execution_blocked_before_dispatch():
    class Agent(FakeAgent):
        executed = False
        def _execute_tool_calls(self, *args):
            self.executed = True
        def run_conversation(self, *args, **kwargs):
            try:
                self._execute_tool_calls('terminal')
            except RuntimeError:
                pass
            assert not self.executed
            return self.response
    with pytest.raises(RuntimeError, match='proof_failed'):
        bridge.run_agent(Agent, definitions, 'system', 'private query', runtime={'model': 'configured-model'})


@pytest.mark.parametrize('assembly', ['definitions', 'agent'])
def test_nonempty_tools_fail_before_provider(assembly):
    class Agent(FakeAgent):
        called = False
        tools = ['tool'] if assembly == 'agent' else []
    with pytest.raises(RuntimeError, match='proof_failed'):
        bridge.run_agent(Agent, (lambda **_: ['tool']) if assembly == 'definitions' else definitions,
                         'system', 'private query', runtime={'model': 'configured-model'})
    assert not Agent.called


@pytest.mark.parametrize('child', [(1, b'', b'private failure'), (0, b'garbage', b''),
    (0, b'{"text":"answer"}', b''), (0, b'{"text":"answer","tools":["terminal"]}', b'')])
def test_command_rejects_unavailable_or_missing_proof(monkeypatch, child, installed_launcher):
    from eimemory.llm import command_client
    calls = []
    def run(argv, request, *, timeout_seconds, environment):
        calls.append(argv)
        assert 'private query' not in repr(argv)
        assert 0 < timeout_seconds <= 90
        return (0, json.dumps(launcher_argv()).encode(), b'') if len(calls) == 1 else child
    monkeypatch.setattr(command_client, 'run_bounded_command', run)
    monkeypatch.setattr('eimemory.llm.research_review.run_bounded_command', run)
    with pytest.raises((RuntimeError, ValueError)):
        bridge.complete('system', 'private query')
    assert len(calls) == 2


def test_command_stdin_and_proof(monkeypatch, installed_launcher):
    from eimemory.llm import command_client
    monkeypatch.setenv('EIMEMORY_HERMES_HOME', 'profile with spaces')
    def run(argv, request, *, timeout_seconds, environment):
        assert environment['HERMES_HOME'] == 'profile with spaces'
        if '--print-runtime-command' in argv:
            return 0, json.dumps(launcher_argv()).encode(), b''
        assert json.loads(request) == {'system': 'system', 'user': 'private query'}
        return 0, b'{"text":"answer","tools":[]}', b''
    monkeypatch.setattr(command_client, 'run_bounded_command', run)
    monkeypatch.setattr('eimemory.llm.research_review.run_bounded_command', run)
    assert bridge.complete('system', 'private query') == 'answer'


@pytest.mark.parametrize('probe, fail', [(False, False), (True, False), (False, True), (True, True)])
def test_isolated_child_discards_output_blocks_writes_and_uses_real_protocol(tmp_path, monkeypatch, probe, fail):
    import sys
    from pathlib import Path
    from eimemory.llm.command_client import run_bounded_command
    (tmp_path / 'hermes_cli').mkdir()
    (tmp_path / 'hermes_cli' / '__init__.py').write_text('')
    for name in ('plugins', 'lifecycle'):
        (tmp_path / 'hermes_cli' / (name + '.py')).write_text('')
    (tmp_path / 'hermes_logging.py').write_text('')
    (tmp_path / 'hermes_cli' / 'runtime_provider.py').write_text(
        'def _get_model_config():\n'
        '    return {"default": "configured-model", "provider": "configured-provider"}\n'
        'def resolve_runtime_provider(**kwargs):\n'
        '    assert kwargs == dict(requested="configured-provider", target_model="configured-model")\n'
        '    return {}\n')
    (tmp_path / 'model_tools.py').write_text(
        'def get_tool_definitions(**kwargs):\n'
        '    assert kwargs == dict(enabled_toolsets=[], disabled_toolsets=["*"], quiet_mode=True)\n'
        '    return []\n')
    forbidden = tmp_path / 'history.txt'
    (tmp_path / 'run_agent.py').write_text('''
class AIAgent:
    def __init__(self, **kwargs):
        assert all(kwargs.pop(key) is None for key in (
            'api_key', 'base_url', 'provider', 'requested_provider', 'api_mode', 'credential_pool'))
        assert kwargs == dict(model='configured-model', enabled_toolsets=[], disabled_toolsets=['*'],
            quiet_mode=True, skip_memory=True, skip_context_files=True,
            save_trajectories=False, max_iterations=1, skip_background_review=True)
        self.tools = []
    def run_conversation(self, user, system_message):
        assert self.tools == [] and self._persist_disabled
        print(user)  # Must never reach parent output or a file.
        try:
            open(FORBIDDEN, 'w').write(user)
        except PermissionError:
            pass
        else:
            raise AssertionError('write was allowed')
        if FAIL:
            raise RuntimeError('secret provider error and key')
        return dict(completed=True, failed=False, final_response='RESPONSE',
                    messages=[dict(role='assistant', content='answer')])
    def close(self):
        pass
'''.replace('FORBIDDEN', repr(str(forbidden))).replace('FAIL', repr(fail))
        .replace('RESPONSE', '{"ok":true}' if probe else 'answer'))
    # Execute the production launch builder, not a separately hand-fixed command.
    # A fake installation supplies modules, but both launcher and child are real
    # isolated Python processes. Ambient imports must not choose the bridge.
    import shlex
    launcher = tmp_path / 'hermes'
    bootstrap = (f'import sys, runpy; sys.path.insert(0, {str(tmp_path)!r}); '
                 "runpy.run_module('runpy', run_name='__main__', alter_sys=True)")
    command = [sys.executable, '-I', '-c', bootstrap]
    launcher.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable)
                        + ' -I -c ' + shlex.quote(
                            f'import json; print(json.dumps({command!r}))') + '\n')
    launcher.chmod(0o700)
    monkeypatch.setenv('EIMEMORY_HERMES_BIN', str(launcher))
    monkeypatch.setenv('PYTHONPATH', str(tmp_path / 'untrusted'))
    monkeypatch.chdir(tmp_path)
    argv = bridge.runtime_command()
    assert argv[:4] == command[:2] + ['-B', '-c']
    assert argv[-1].startswith(bootstrap.rsplit('runpy.run_module', 1)[0])
    assert str(Path(bridge.__file__).resolve()) in argv[-1]
    code, out, err = run_bounded_command(argv, b'{"check_tools":true}', timeout_seconds=10)
    assert (code, json.loads(out), err) == (0, {'tools': []}, b'')
    request = b'{"synthetic_probe":true}' if probe else b'{"system":"system","user":"private query"}'
    code, out, err = run_bounded_command(argv, request, timeout_seconds=10)
    assert code == int(fail)
    assert err == b''
    if probe:
        frames = [json.loads(line) for line in out.splitlines()]
        assert frames[-1] == ({'stage': 'conversation', 'failure': 'unavailable', 'blocked': ['write']}
                              if fail else {'blocked': ['write']})
        assert b'secret' not in out and b'private query' not in out and b'"ok"' not in out
        if not fail:
            assert {'stage': 'synthetic_ok'} in frames
    elif fail:
        assert out == b''
    else:
        assert json.loads(out) == {'text': 'answer', 'tools': []}
    assert not forbidden.exists()


@pytest.mark.parametrize('argv', [[], ['/python', 'bridge.py'],
    ['/python', '-c', '-I', 'code'],
    ['/python', '-I', '-c', launcher_argv()[3] + '; print("unexpected")']])
def test_runtime_contract_changes_fail_closed(monkeypatch, argv):
    from eimemory.llm import command_client
    monkeypatch.setenv('EIMEMORY_HERMES_BIN', '/installed/hermes')
    monkeypatch.setattr(command_client, 'run_bounded_command',
                        lambda *a, **k: (0, json.dumps(argv).encode(), b''))
    with pytest.raises(RuntimeError, match='hermes_runtime_unavailable'):
        bridge.runtime_command()


def test_installed_hermes_real_launch():
    import os
    from eimemory.llm.command_client import run_bounded_command
    if os.environ.get('EIMEMORY_TEST_INSTALLED_HERMES') != '1':
        pytest.skip('opt in to installed Hermes bootstrap and dependency leases')
    code, out, err = run_bounded_command(bridge.runtime_command(),
                                       b'{"check_tools":true}', timeout_seconds=30)
    assert code == 0  # Never include launcher/provider stderr in assertion diagnostics.
    assert json.loads(out) == {'tools': []}
    assert err == b''


def test_configured_runtime_preserves_default_model_and_protocol(monkeypatch):
    import sys
    import types
    pool = object()
    route = dict(provider='configured-provider', requested_provider='configured-provider',
                 api_mode='anthropic_messages', base_url='https://example.invalid',
                 api_key='synthetic-key', credential_pool=pool)
    def resolve(**kwargs):
        assert kwargs == dict(requested='configured-provider', target_model='configured-model')
        return route
    module = types.ModuleType('hermes_cli.runtime_provider')
    module._get_model_config = lambda: dict(default='configured-model', provider='configured-provider')
    module.resolve_runtime_provider = resolve
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', module)
    assert bridge.configured_runtime() == dict(model='configured-model', **route)
    module._get_model_config = lambda: {}
    with pytest.raises(RuntimeError, match='configured_model_unavailable'):
        bridge.configured_runtime()


def test_installed_hermes_real_synthetic_completion():
    import os
    from eimemory.llm.command_client import run_bounded_command
    if os.environ.get('EIMEMORY_TEST_INSTALLED_HERMES_COMPLETION') != '1':
        pytest.skip('opt in to a harmless completion using the configured provider')
    code, out, err = run_bounded_command(bridge.runtime_command(),
                                       b'{"synthetic_probe":true}', timeout_seconds=90)
    assert code == 0
    assert not err
    frames = [json.loads(line) for line in out.splitlines()]
    assert {'stage': 'tools_empty'} in frames
    assert {'stage': 'validated'} in frames
    assert {'stage': 'synthetic_ok'} in frames
