"""Real isolated-process Hermes contracts; no network or production credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake import closure_review
from eimemory.intake.closure import REVIEW_STATUS_UNAVAILABLE
from eimemory.llm.command_client import CommandCompletionError
from eimemory.llm.research_review import research_review_client, research_review_configuration
from eimemory.scheduler.research_review_diagnostics import research_review_diagnostics
from test_nightly_evidence_workflow import SCOPE, _closure


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / "relocated install with spaces"
    home = tmp_path / "independent profile"
    root.mkdir()
    home.mkdir()
    for key in ("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", "EIMEMORY_LLM_COMMAND",
                "EIMEMORY_HERMES_AGENT_ROOT", "EIMEMORY_ALLOWED_REVIEW_MODELS",
                "EIMEMORY_LUNA_PROVIDER", "EIMEMORY_LUNA_MODEL"):
        monkeypatch.delenv(key, raising=False)
    (root / "hermes_bootstrap.py").write_text("")
    for package in ("hermes_cli", "agent"):
        (root / package).mkdir()
        (root / package / "__init__.py").write_text("")
    (root / "hermes_cli/runtime_provider.py").write_text('''
import json, os
from pathlib import Path
def config():
    return json.loads((Path(os.environ['HERMES_HOME']) / 'actual-config.json').read_text())
def _get_model_config():
    value = config()
    return dict(default=value['model'], provider=value['provider'])
def resolve_runtime_provider(*, requested, target_model):
    value = config()
    assert (requested, target_model) == (value['provider'], value['model'])
    return dict(provider=value['provider'], model=value['model'], base_url=value['url'],
                api_key=os.environ['HOST_CREDENTIAL'], api_mode=value['mode'])
''')
    (root / "hermes_cli/env_loader.py").write_text('''
import os
def load_hermes_dotenv():
    os.environ['HOST_CREDENTIAL'] = 'private-host-token'
''')
    (root / "hermes_cli/config.py").write_text('''
from hermes_cli.runtime_provider import config
def load_config_readonly():
    return {'agent': {'reasoning_effort': config().get('effort', '')}}
load_config = load_config_readonly
''')
    (root / "hermes_constants.py").write_text('''
def resolve_reasoning_config(config, model):
    raw = config['agent']['reasoning_effort']
    return {'enabled': True, 'effort': raw} if raw else None
''')
    (root / "agent/auxiliary_client.py").write_text('''
import json, os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from hermes_cli.runtime_provider import config
def resolve_provider_client(provider, model=None, *, explicit_base_url=None,
                            explicit_api_key=None, api_mode=None, main_runtime=None):
    value = config()
    assert provider == value['provider'] and model == value['model']
    assert explicit_base_url == value['url'] and explicit_api_key == 'private-host-token'
    assert api_mode == value['mode'] and main_runtime['provider'] == provider
    assert 'private-parent-token' not in os.environ.values()
    print('private-router-log')
    def create(**kwargs):
        key = '_reasoning_config' if value['mode'] == 'anthropic_messages' else 'reasoning_effort'
        assert set(kwargs) == {'model', 'messages', 'timeout', key}
        assert kwargs[key] == ({'enabled': True, 'effort': value['effort']}
                              if key == '_reasoning_config' else value['effort'])
        assert [item['role'] for item in kwargs['messages']] == ['system', 'user']
        assert 0 < kwargs['timeout'] <= 90
        assert kwargs['messages'][1]['content'] not in repr(sys.argv)
        marker = Path(os.environ['HERMES_HOME']) / 'sdk-calls'
        marker.write_text(marker.read_text() + 'x' if marker.exists() else 'x')
        print('private-model-log', file=sys.stderr)
        if value.get('failure'):
            raise RuntimeError('private-provider-token')
        review = dict(verdict='approve', rationale='Evidence checked', required_followup='', risk='low')
        message = NS(content=json.dumps(review), tool_calls=value.get('tools'), function_call=None)
        return NS(model=value.get('response_model', model), choices=[NS(message=message)])
    return NS(chat=NS(completions=NS(create=create)), close=lambda: None), model
def _build_call_kwargs(provider, model, messages, timeout, base_url, reasoning_config):
    assert reasoning_config == {'enabled': True, 'effort': config()['effort']}
    payload = dict(model=model, messages=messages, timeout=timeout)
    if config()['mode'] == 'anthropic_messages':
        payload['_reasoning_config'] = reasoning_config
    else:
        payload['reasoning_effort'] = reasoning_config['effort']
    return payload
''')
    path = home / "actual-config.json"
    value = dict(provider="custom-review", model="gpt-6.1-sol", effort="low", url="https://private.invalid/v1", mode="chat_completions")
    path.write_text(json.dumps(value))
    binary = root / "hermes"
    prefix = ("import os,sys,runpy; " + f"sys.path.insert(0,{str(root)!r}); "
              + f"os.environ['HERMES_HOME']=os.environ.get('HERMES_HOME') or {str(home)!r}; "
              + "import hermes_bootstrap; ")
    launcher = [sys.executable, "-I", "-c", prefix
                + 'runpy.run_module("runpy", alter_sys=True, run_name="__main__")']
    binary.write_text(f"#!{sys.executable}\nimport json,sys\n"
                      "assert sys.argv[1:] == ['--print-runtime-command','--module','runpy']\n"
                      + f"print({json.dumps(launcher)!r})\n")
    binary.chmod(0o700)
    monkeypatch.setenv("EIMEMORY_HERMES_BIN", str(binary))
    monkeypatch.setenv("EIMEMORY_HERMES_HOME", str(home))
    monkeypatch.setenv("UNRELATED_SECRET", "private-parent-token")
    return root, home, binary, path, value


@pytest.mark.parametrize("discovery", ["explicit", "path", "user_bin", "modern_root", "legacy_root", "console"])
def test_installed_runtime_and_current_profile_are_discovered(host, monkeypatch, tmp_path, discovery):
    root, home, binary, path, value = host
    if discovery != "explicit":
        monkeypatch.delenv("EIMEMORY_HERMES_BIN")
    if discovery == "path":
        monkeypatch.setenv("PATH", str(root))
    elif discovery == "user_bin":
        user_bin = tmp_path / "user/.local/bin"
        user_bin.mkdir(parents=True)
        (user_bin / "hermes").symlink_to(binary)
        monkeypatch.setenv("HOME", str(tmp_path / "user"))
        monkeypatch.setenv("PATH", str(tmp_path / "no-binary"))
    elif discovery == "modern_root":
        directory = root / ".hermes/bin"
        directory.mkdir(parents=True)
        (directory / "hermes").symlink_to(binary)
        monkeypatch.setenv("EIMEMORY_HERMES_AGENT_ROOT", str(root))
    elif discovery == "legacy_root":
        directory = root / ".venv/bin"
        directory.mkdir(parents=True)
        (directory / "python").symlink_to(sys.executable)
        monkeypatch.setenv("EIMEMORY_HERMES_AGENT_ROOT", str(root))
    elif discovery == "console":
        environment = tmp_path / "sdk environment with spaces"
        venv.EnvBuilder(symlinks=True).create(environment)
        python = environment / "bin/python"
        purelib = subprocess.check_output([str(python), "-I", "-B", "-c",
                                           "import sysconfig;print(sysconfig.get_path('purelib'))"], text=True).strip()
        (Path(purelib) / "hermes.pth").write_text(str(root) + "\n")
        binary.write_text("#!/bin/sh\n'''exec' " + repr(str(python))
                          + ' "$0" "$@"\n\' \'\'\'\nfrom hermes_cli.main import main\nmain()\n')
        monkeypatch.setenv("EIMEMORY_HERMES_BIN", str(binary))
    client = research_review_client()
    assert client is not None
    client.check_configuration()
    assert not (home / "sdk-calls").exists()
    first = client.complete(system_prompt="system", user_prompt="private artifact", json_mode=True)
    assert (first.provider_id, first.model_id) == (value["provider"], "gpt-6.1-sol")
    value.update(provider="other-provider", model="variant/model:v2", mode="anthropic_messages", effort="high")
    path.write_text(json.dumps(value))
    second = client.complete(system_prompt="system", user_prompt="private artifact", json_mode=True)
    assert (second.provider_id, second.model_id) == ("other-provider", "variant/model:v2")
    assert (home / "sdk-calls").read_text() == "xx"
    assert os.environ.get("HOST_CREDENTIAL") is None
    assert not list(root.rglob("*.pyc"))


def test_semantic_judge_discovers_the_same_host_launcher_without_literal_quote_requirements(host):
    from eimemory.llm import hermes_tool_free
    argv = hermes_tool_free.runtime_command()
    assert "-B" in argv
    assert str(Path(hermes_tool_free.__file__).resolve()) in argv[-1]
    assert "hermes_review_command.py" not in argv[-1]
    assert not (host[1] / "sdk-calls").exists()


def test_automatic_review_persists_actual_identity_and_keeps_model_policy(host, monkeypatch, tmp_path):
    with Runtime.create(root=tmp_path / "store") as runtime:
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert report["ok"] and report["reviewed"] == 1
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert saved.meta["review_provider_used"] == "custom-review"
        assert saved.meta["review_model_used"] == "gpt-6.1-sol"
        monkeypatch.setenv("EIMEMORY_ALLOWED_REVIEW_MODELS", "another-model")
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert not report["ok"]
        assert report["unavailable_records"][0]["error"] == "review_model_not_allowed"


@pytest.mark.parametrize("change,expected", [
    ({"failure": True}, "provider_request_failed"),
    ({"response_model": "different-model"}, "bridge_response_validation_failed"),
    ({"tools": [{"name": "terminal"}]}, "bridge_response_validation_failed"),
    ({"mode": "codex_app_server"}, "bridge_client_setup_failed"),
])
def test_automatic_review_fails_closed_without_another_model_or_private_diagnostics(host, tmp_path, change, expected):
    _, home, _, path, value = host
    value.update(change)
    path.write_text(json.dumps(value))
    with Runtime.create(root=tmp_path / "store") as runtime:
        record = _closure(runtime)
        report = closure_review.review_pending_research_closures(runtime, scope=SCOPE)
        assert not report["ok"] and report["reviewed"] == 0
        saved = runtime.store.get_by_id(record.record_id, scope=SCOPE)
        assert saved.meta["review_status"] == REVIEW_STATUS_UNAVAILABLE
        assert report["unavailable_records"][0]["error"] == "command_completion_failed:" + expected
        durable = research_review_diagnostics(report)
        assert "private" not in json.dumps(durable)
        if "mode" in change:
            assert not (home / "sdk-calls").exists()
        else:
            assert (home / "sdk-calls").read_text() == "x"


def test_explicit_selected_command_wins_and_never_falls_back_to_installed_hermes(host, monkeypatch):
    monkeypatch.setenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", json.dumps([sys.executable, "-c", "raise SystemExit(1)"]))
    with pytest.raises(CommandCompletionError):
        closure_review.configured_review_exec("unused-model", "private artifact")
    assert not (host[1] / "sdk-calls").exists()
    monkeypatch.setenv("EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND", "[]")
    with pytest.raises(RuntimeError, match="research_review_llm_configuration_invalid"):
        closure_review.configured_review_exec("unused-model", "private artifact")


@pytest.mark.parametrize("model", ["", None, {"unknown": "format"}])
def test_automatic_preflight_checks_config_without_provider_call(host, model):
    _, home, binary, path, value = host
    value["model"] = model
    path.write_text(json.dumps(value))
    report = research_review_configuration({"EIMEMORY_HERMES_BIN": str(binary), "HERMES_HOME": str(home)})
    assert report == {"configuration_ok": False, "error": "research_review_hermes_configuration_invalid", "provider_verified": False}
    assert not (home / "sdk-calls").exists()


def test_managed_preflight_uses_durable_runtime_over_controller_overrides(host, tmp_path):
    _, home, binary, _, _ = host
    config = tmp_path / "governance.env"
    config.write_text(f"EIMEMORY_HERMES_BIN='{binary}'\nEIMEMORY_HERMES_HOME='{home}'\n")
    config.chmod(0o600)
    result = subprocess.run([sys.executable, "-I", "-B", "deploy/run_with_governance_env.py",
                             "--env-file", str(config), "--check-research-review"],
                            env={**os.environ, "EIMEMORY_HERMES_BIN": "/wrong-controller-path",
                                 "EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND": "[]"},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"configuration_ok": True, "error": "", "provider_verified": False}
    assert not (home / "sdk-calls").exists()


@pytest.mark.parametrize("configuration", [None, "EIMEMORY_LLM_COMMAND=\n"])
def test_managed_preflight_rejects_missing_service_runtime_even_with_controller_hermes(
        host, tmp_path, configuration, managed_research_preflight):
    root, home, binary, _, _ = host
    controller_home = tmp_path / "controller account"
    user_bin = controller_home / ".local/bin"
    user_bin.mkdir(parents=True)
    (user_bin / "hermes").symlink_to(binary)
    config = tmp_path / "governance.env"
    if configuration is not None:
        config.write_text(configuration)
        config.chmod(0o600)
    result = managed_research_preflight(
        config, environment={**os.environ, "HOME": str(controller_home),
                             "USERPROFILE": str(controller_home), "PATH": str(root)})
    assert result.returncode == 2, result.stderr
    assert json.loads(result.stdout) == {
        "configuration_ok": False, "error": "research_review_llm_unconfigured", "provider_verified": False}
    assert not (home / "sdk-calls").exists()


@pytest.mark.parametrize("configuration", [None, "EIMEMORY_LLM_COMMAND=\n"])
@pytest.mark.parametrize("discovery", ["account_home", "system_path"])
def test_managed_preflight_discovers_service_hermes_and_rechecks_current_profile(
        host, tmp_path, configuration, discovery, managed_research_preflight):
    root, home, binary, profile, value = host
    account_home = tmp_path / "installed service account"
    account_home.mkdir()
    if discovery == "account_home":
        user_bin = account_home / ".local/bin"
        user_bin.mkdir(parents=True)
        (user_bin / "hermes").symlink_to(binary)
    config = tmp_path / "governance.env"
    if configuration is not None:
        config.write_text(configuration)
        config.chmod(0o600)
    # No durable Hermes route/profile overrides. The installation is available
    # through the account home or system PATH; all controller settings are bogus.
    environment = {**os.environ, "HOME": str(tmp_path / "wrong controller home"),
                   "USERPROFILE": str(tmp_path / "wrong controller home"),
                   "PATH": str(tmp_path / "wrong controller path"),
                   "EIMEMORY_HERMES_BIN": "/wrong-controller-binary",
                   "EIMEMORY_HERMES_AGENT_ROOT": "/wrong-controller-root",
                   "EIMEMORY_HERMES_HOME": "/wrong-controller-profile",
                   "HERMES_HOME": "/wrong-controller-profile",
                   "EIMEMORY_LLM_COMMAND": "[]", "EIMEMORY_RESEARCH_REVIEW_LLM_COMMAND": "[]"}
    for change, expected in [({}, ""),
                             ({"provider": "other-provider", "model": "variant/model:v2",
                               "effort": "high", "mode": "anthropic_messages"}, ""),
                             ({"model": None}, "research_review_hermes_configuration_invalid")]:
        value.update(change)
        profile.write_text(json.dumps(value))
        arguments = {"home": account_home}
        if discovery == "system_path":
            arguments["path"] = root
        result = managed_research_preflight(config, environment=environment, **arguments)
        assert result.returncode == (2 if expected else 0), result.stderr
        assert json.loads(result.stdout) == {
            "configuration_ok": not expected, "error": expected, "provider_verified": False}
        assert not (home / "sdk-calls").exists()
        assert "private" not in result.stdout + result.stderr


def test_configured_reasoning_requires_a_compatible_native_request_builder(host):
    root, home, binary, _, _ = host
    (root / "agent/auxiliary_client.py").write_text(
        "def resolve_provider_client(*args, **kwargs):\n    raise AssertionError('no model call')\n"
        "def _build_call_kwargs(provider, model, messages):\n    raise AssertionError('wrong API')\n")
    report = research_review_configuration({"EIMEMORY_HERMES_BIN": str(binary), "HERMES_HOME": str(home)})
    assert report["error"] == "research_review_hermes_runtime_unavailable"
    assert not report["configuration_ok"] and not report["provider_verified"]
    assert not (home / "sdk-calls").exists()


@pytest.mark.parametrize("broken", ["missing_binary", "malformed_launch", "missing_router", "unsupported_old_router"])
def test_broken_installation_never_uses_other_installs_or_leaks_probe_output(host, monkeypatch, broken):
    root, home, binary, _, _ = host
    if broken == "missing_binary":
        monkeypatch.setenv("EIMEMORY_HERMES_BIN", str(root / "missing"))
    elif broken == "malformed_launch":
        binary.write_text(f"#!{sys.executable}\nprint('private-host-token')\n")
    elif broken == "missing_router":
        (root / "hermes_cli/runtime_provider.py").unlink()
    else:
        (root / "agent/auxiliary_client.py").write_text(
            "def resolve_provider_client(provider, model=None):\n"
            "    raise AssertionError('dropping transport must never call me')\n")
    with pytest.raises((RuntimeError, CommandCompletionError)) as caught:
        closure_review.configured_review_exec("unused-model", "private artifact")
    assert "private" not in str(caught.value)
    assert not (home / "sdk-calls").exists()
