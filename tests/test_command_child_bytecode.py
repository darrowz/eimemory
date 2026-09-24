"""A sanitized verifier child must not write into immutable plugin sources."""
import json
import subprocess
import sys

from eimemory.llm.command_client import _subprocess_env


def test_command_child_preserves_no_bytecode_without_inheriting_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')
    monkeypatch.setenv('SYNTHETIC_PRIVATE_CREDENTIAL', 'fixture-not-a-real-secret')
    monkeypatch.delenv('EIMEMORY_LLM_ENV_ALLOW', raising=False)
    plugin = tmp_path / 'plugin.py'
    plugin.write_text('value = 42\n')
    code = ('import importlib.util, json, sys;'
            's=importlib.util.spec_from_file_location("test_plugin",sys.argv[1]);'
            'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
            'print(json.dumps({"value":m.value,"no_bytecode":sys.dont_write_bytecode}))')
    env = _subprocess_env()
    assert 'SYNTHETIC_PRIVATE_CREDENTIAL' not in env
    result = subprocess.run([sys.executable, '-c', code, str(plugin)], env=env,
                            capture_output=True, text=True, check=True, timeout=5)
    assert json.loads(result.stdout) == {'value': 42, 'no_bytecode': True}
    assert not (tmp_path / '__pycache__').exists()
