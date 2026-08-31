from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location("gateway_ready", Path("deploy/wait_openclaw_gateway_ready.py"))
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.mark.parametrize("responses,expected", [
    ([True, True], True), ([True, False, True, True], True),
    ([True, False, False, False, False], False),
    ([False] * 5, False),
])
def test_readiness_requires_consecutive_actual_rpc_health(tmp_path, responses, expected):
    config = tmp_path / "config.json"
    config.write_text("{}")
    now = [0]
    calls = []
    def runner(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == [sys.executable, "gateway", "health", "--json"]
        assert kwargs["env"]["OPENCLAW_CONFIG_PATH"] == str(config)
        assert kwargs["shell"] is False
        ok = responses.pop(0) if responses else False
        return SimpleNamespace(returncode=0 if ok else 1, stdout='{"ok":true,"channels":{},"agents":[],"ts":1}', stderr="secret")
    result = helper.wait_ready(sys.executable, str(config), timeout=8, runner=runner,
        clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert result["ok"] is expected
    assert "secret" not in str(result)


@pytest.mark.parametrize("output", ['{}', '{"ok":true}', 'invalid', '{"ok":false,"channels":{},"agents":[],"ts":1}'])
def test_malformed_or_auth_diagnostic_is_not_readiness(tmp_path, output):
    config = tmp_path / "config.json"
    config.write_text("{}")
    now = [0]
    result = helper.wait_ready(sys.executable, str(config), timeout=2,
        runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=output),
        clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert result["ok"] is False


@pytest.mark.parametrize("configuration,allowed", [
    ('{}', True), ('{"gateway":{}}', True),
    ('{"gateway":{"mode":"local"}}', True),
    ('{"gateway":{"mode":"remote"}}', False),
    ('{"gateway":{"mode":null}}', False),
    ('{"gateway":null}', False), ('[]', False), ('broken', False),
])
def test_managed_readiness_requires_local_configuration(tmp_path, monkeypatch, configuration, allowed):
    config = tmp_path / "config.json"
    config.write_text(configuration)
    monkeypatch.setenv("OPENCLAW_GATEWAY_URL", "wss://untrusted.invalid")
    monkeypatch.setenv("OPENCLAW_GATEWAY_PORT", "1")
    calls = []
    now = [0]
    def runner(*args, **kwargs):
        calls.append(args)
        assert "OPENCLAW_GATEWAY_URL" not in kwargs["env"]
        assert "OPENCLAW_GATEWAY_PORT" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"channels":{},"agents":[],"ts":1}')
    result = helper.wait_ready(sys.executable, str(config), timeout=5, runner=runner,
        clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds))
    assert result["ok"] is allowed
    assert bool(calls) is allowed
