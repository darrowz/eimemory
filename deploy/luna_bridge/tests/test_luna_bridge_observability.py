"""Generated bridge + fake provider + real subprocess, never the production bridge."""
import ast
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from eimemory.llm.command_client import CommandLLMClient, CommandCompletionError
from eimemory.llm.completion_timing import BRIDGE_TIMING_FIELDS, FAILURE_SCHEMA
from make_luna_patch import instrument, Unsupported
from luna_observability import BridgeTrace

HERE = Path(__file__).resolve().parent
BRIDGE_DIR = HERE.parent
FIXTURE = HERE/'fixtures/luna_review_fixture.py'

FAKE_PROVIDER = '''# Test-only; there are no network calls.
import json,os,sys,time
from types import SimpleNamespace
MODE = os.environ.get('FAKE_LUNA_MODE', 'success')
time.sleep(.003)
if MODE == 'import':
    raise ImportError('SECRET_IMPORT_ERROR credential')
print('PRIVATE_PROVIDER_LOG', file=sys.stderr)

def resolve_provider_client(provider, model):
    assert provider == 'openai-codex' and model == 'gpt-5.6-luna'
    time.sleep(.003)
    if MODE == 'setup':
        raise RuntimeError('SECRET_SETUP_ERROR request')
    def create(**kwargs):
        assert kwargs['reasoning_effort'] == 'low'
        assert kwargs['model'] == 'gpt-5.6-luna'
        assert kwargs['timeout'] == 2.5
        time.sleep(.003)
        if MODE == 'api':
            raise TimeoutError('SECRET_API_ERROR token')
        if MODE == 'block':
            time.sleep(5)
        text = '{ "selected" : [] }'
        if MODE == 'json': text = 'SECRET_NOT_JSON'
        if MODE == 'shape': text = '{"private_key":"SECRET"}'
        if MODE == 'empty': text = ''
        message = SimpleNamespace(content=text, tool_calls=['SECRET_TOOL'] if MODE == 'tools' else None)
        return SimpleNamespace(model='other-model' if MODE == 'identity' else 'gpt-5.6-luna',
                               choices=[SimpleNamespace(message=message)])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
'''


@pytest.fixture
def generated(tmp_path, monkeypatch):
    source = FIXTURE.read_text()
    script = tmp_path/'luna_review_command.py'
    script.write_text(instrument(source))
    shutil.copyfile(BRIDGE_DIR/'luna_observability.py', tmp_path/'luna_observability.py')
    agent = tmp_path/'agent'; agent.mkdir(); (agent/'__init__.py').write_text('')
    (agent/'auxiliary_client.py').write_text(FAKE_PROVIDER)
    monkeypatch.setenv('FAKE_LUNA_MODE', 'success')
    # CommandLLMClient forwards only an explicit allow-list. The fake provider
    # mode is test-only and must not be added to the production bridge keys.
    monkeypatch.setenv('EIMEMORY_LLM_ENV_ALLOW', 'FAKE_LUNA_MODE')
    # Python finds fake provider beside the generated script, not a real package.
    return script


def raw_run(script):
    return subprocess.run([sys.executable, str(script)],
        input=json.dumps({'system_prompt': 'fixture', 'user_prompt': 'fixture'}),
        text=True, capture_output=True, timeout=4)


def test_instrumentation_preserves_original_call_arguments_and_validation():
    before = FIXTURE.read_text(); after = instrument(before)
    def calls(source, suffix):
        return [ast.dump(n) for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.Call) and ast.unparse(n.func).endswith(suffix)]
    for suffix in ('resolve_provider_client', 'chat.completions.create', 'json.loads'):
        assert calls(before, suffix) == calls(after, suffix)
    for marker in ('response.model', 'message.tool_calls', 'message.content', "{'selected'}"):
        assert marker in after


def test_generated_success_preserves_exact_inner_text_and_actual_identity(generated):
    result = raw_run(generated)
    assert result.returncode == 0 and result.stderr == ''
    data = json.loads(result.stdout)
    assert data['text'] == '{ "selected" : [] }'
    assert data['model_id'] == 'gpt-5.6-luna' and data['provider_id'] == 'openai-codex'
    assert set(data) == {'text', 'provider_id', 'model_id', 'diagnostics'}
    assert set(data['diagnostics']) == set(BRIDGE_TIMING_FIELDS)
    assert data['diagnostics']['bridge_import_ms'] >= 2
    assert data['diagnostics']['bridge_client_setup_ms'] >= 2
    assert data['diagnostics']['provider_response_ms'] >= 2
    assert all(type(v) in (int, float) and v >= 0 for v in data['diagnostics'].values())


@pytest.mark.parametrize('mode,category,present,absent', [
    ('import', 'bridge_import_failed', ['bridge_import_ms'], ['bridge_client_setup_ms', 'provider_response_ms', 'bridge_response_validation_ms']),
    ('setup', 'bridge_client_setup_failed', ['bridge_import_ms', 'bridge_client_setup_ms'], ['provider_response_ms', 'bridge_response_validation_ms']),
    ('api', 'provider_request_failed', ['bridge_import_ms', 'bridge_client_setup_ms', 'provider_response_ms'], ['bridge_response_validation_ms']),
    ('identity', 'bridge_response_validation_failed', list(BRIDGE_TIMING_FIELDS), []),
    ('tools', 'bridge_response_validation_failed', list(BRIDGE_TIMING_FIELDS), []),
    ('empty', 'bridge_response_validation_failed', list(BRIDGE_TIMING_FIELDS), []),
    ('json', 'bridge_response_validation_failed', list(BRIDGE_TIMING_FIELDS), []),
    ('shape', 'bridge_response_validation_failed', list(BRIDGE_TIMING_FIELDS), []),
])
def test_failed_bridge_returns_safe_bounded_partial_frame(generated, monkeypatch, mode, category, present, absent):
    monkeypatch.setenv('FAKE_LUNA_MODE', mode)
    result = raw_run(generated)
    assert result.returncode == 1 and result.stderr == ''
    assert len(result.stdout.encode()) <= 4096
    assert 'SECRET' not in result.stdout and 'PRIVATE' not in result.stdout
    data = json.loads(result.stdout)
    assert set(data) == {'schema', 'error', 'diagnostics'}
    assert data['schema'] == FAILURE_SCHEMA and data['error'] == category
    for key in present:
        assert key in data['diagnostics']
    for key in absent:
        assert key not in data['diagnostics']
    assert 'bridge_elapsed_ms' in data['diagnostics']


def test_real_command_parent_receives_child_timings(generated):
    command = CommandLLMClient([sys.executable, str(generated)], timeout_seconds=3)
    result = command.complete(system_prompt='fixture', user_prompt='fixture')
    assert all(k in result.diagnostics for k in BRIDGE_TIMING_FIELDS)
    assert result.diagnostics['command_prepared'] is False
    assert 'command_io_ms' in result.diagnostics


def test_real_command_parent_receives_failure_category_without_accepting_answer(generated, monkeypatch):
    monkeypatch.setenv('FAKE_LUNA_MODE', 'api')
    command = CommandLLMClient([sys.executable, str(generated)], timeout_seconds=3)
    with pytest.raises(CommandCompletionError) as exc:
        command.complete(system_prompt='fixture', user_prompt='fixture')
    assert exc.value.failure_category == 'provider_request_failed'
    assert exc.value.completion_timing['provider_response_ms'] >= 2
    assert 'bridge_response_validation_ms' not in exc.value.completion_timing
    assert 'SECRET' not in repr(vars(exc.value))


def test_forced_termination_cannot_claim_unreceived_partial_stages(generated, monkeypatch):
    monkeypatch.setenv('FAKE_LUNA_MODE', 'block')
    command = CommandLLMClient([sys.executable, str(generated)], timeout_seconds=3)
    command.timeout_seconds = .2
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        command.complete(system_prompt='fixture', user_prompt='fixture')
    assert 'command_io_ms' in exc.value.completion_timing
    assert not any(k in exc.value.completion_timing for k in BRIDGE_TIMING_FIELDS)


@pytest.mark.parametrize('replace', [
    ('gpt-5.6-luna', 'different-model'),
    ("reasoning_effort='low'", "reasoning_effort='high'"),
    ("reasoning_effort='low'", "reasoning_effort='low', stream=True"),
    ("model='gpt-5.6-luna'", 'model=unverified_variable'),
    ('from agent.auxiliary_client import resolve_provider_client', 'from other import resolve_provider_client'),
    ('client.chat.completions.create(', 'client.responses.create('),
])
def test_unsupported_or_changed_contract_refuses_generation(replace):
    with pytest.raises(Unsupported):
        instrument(FIXTURE.read_text().replace(*replace))


def test_reinstrumentation_refused():
    with pytest.raises(Unsupported):
        instrument(instrument(FIXTURE.read_text()))


def test_importing_generated_module_does_not_execute_main(generated):
    script = ("import sys,importlib.util;sys.path.insert(0," + repr(str(generated.parent)) + ");"
              "s=importlib.util.spec_from_file_location('fixture_module'," + repr(str(generated)) + ");"
              "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);assert callable(m.main)")
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0 and result.stdout == '' and result.stderr == ''


def test_generator_binds_hash_and_never_overwrites_or_executes_source(tmp_path):
    source = tmp_path/'luna_review_command.py'; source.write_text(FIXTURE.read_text())
    digest = sha256(source.read_bytes()).hexdigest()
    out = tmp_path/'candidate'
    argv = [sys.executable, str(BRIDGE_DIR/'make_luna_patch.py'), '--source', str(source),
            '--expected-sha256', digest, '--output-dir', str(out)]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert sha256(source.read_bytes()).hexdigest() == digest
    manifest = json.loads((out/'manifest.json').read_text())
    assert manifest['source_executed'] is False and manifest['original_overwritten'] is False
    assert (out/'luna_review_command.observability.patch').is_file()
    assert subprocess.run(argv, capture_output=True, timeout=3).returncode == 2
    argv[argv.index(digest)] = '0' * 64
    assert subprocess.run(argv, capture_output=True, timeout=3).returncode == 2


def test_monotonic_deltas_no_wall_clock_assumption():
    ticks = iter([100_000_000, 102_000_000, 110_000_000])
    trace = BridgeTrace(0, clock=lambda: next(ticks))
    with trace.stage('provider_response_ms'):
        pass
    output = trace._finish()
    assert output['provider_response_ms'] == 2
    assert output['bridge_elapsed_ms'] == 110


def test_invalid_request_is_generic_bridge_failure_not_api_failure(generated):
    result = subprocess.run([sys.executable, str(generated)], input='not-json',
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 1 and result.stderr == ''
    data = json.loads(result.stdout)
    assert data['error'] == 'bridge_failed'
    assert set(data['diagnostics']) == {'bridge_elapsed_ms'}


def test_source_without_final_newline_refused_for_reliable_diff():
    with pytest.raises(Unsupported):
        instrument(FIXTURE.read_text().rstrip('\n'))
