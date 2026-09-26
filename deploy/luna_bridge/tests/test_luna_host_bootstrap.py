"""Bridge imports must activate the host's dependency contract before its SDK.

The fake host models an upgrade whose dependencies are not in the launch venv.
No network, credentials, or production storage is used.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import pytest

BRIDGE = Path(__file__).resolve().parents[1] / 'luna_review_command.py'


@pytest.mark.parametrize('reexec', [False, True])
def test_bridge_bootstraps_host_before_provider_import(tmp_path, reexec):
    (tmp_path / 'agent').mkdir()
    (tmp_path / 'agent/__init__.py').write_text('')
    (tmp_path / 'hermes_bootstrap.py').write_text(
        'import builtins\nbuiltins._bridge_host_ready = True\n')
    (tmp_path / 'agent/auxiliary_client.py').write_text('''
import builtins
from types import SimpleNamespace
if not getattr(builtins, '_bridge_host_ready', False):
    raise ModuleNotFoundError('host dependency environment not activated')
def resolve_provider_client(provider, *, model):
    def create(**kwargs):
        assert kwargs['model'] == 'fixture-model'
        assert kwargs['reasoning_effort'] == 'low'
        return SimpleNamespace(model=model, choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"ok":true}', tool_calls=None))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), model
''')
    env = {'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
           'EIMEMORY_HERMES_AGENT_ROOT': str(tmp_path)}
    request = {'system_prompt': 'fixture', 'user_prompt': 'fixture', 'json_mode': True,
               'provider': 'fixture-provider', 'model': 'fixture-model', 'reasoning_effort': 'low'}
    argv = [sys.executable, '-B', str(BRIDGE)]
    if reexec:
        argv = [sys.executable, '-I', '-B', '-c',
                f"import runpy; runpy.run_path({str(BRIDGE)!r}, run_name='__main__')"]
    result = subprocess.run(argv, input=json.dumps(request),
                            capture_output=True, text=True, env=env, timeout=15)
    assert result.returncode == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload['provider_id'] == 'fixture-provider'
    assert payload['model_id'] == 'fixture-model'
    assert json.loads(payload['text']) == {'ok': True}


def test_host_bootstrap_failure_stays_fail_closed_and_redacted(tmp_path):
    (tmp_path / 'hermes_bootstrap.py').write_text(
        "raise RuntimeError('fixture-sensitive-marker')\n")
    result = subprocess.run(
        [sys.executable, '-I', '-B', '-c',
         f"import runpy; runpy.run_path({str(BRIDGE)!r}, run_name='__main__')"],
        input='{}', text=True, capture_output=True, timeout=15,
        env={'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
             'EIMEMORY_HERMES_AGENT_ROOT': str(tmp_path)})
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload['error'] == 'bridge_import_failed'
    assert 'text' not in payload
    assert 'fixture-sensitive-marker' not in result.stdout + result.stderr
