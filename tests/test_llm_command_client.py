from __future__ import annotations

import json
import sys

import pytest

from eimemory.llm.command_client import CommandLLMClient, llm_client_from_env
from eimemory.llm import openclaw_adapter
from eimemory.api.runtime import Runtime


def test_command_llm_client_is_provider_neutral() -> None:
    script = (
        "import json,sys; p=json.load(sys.stdin); "
        "print(json.dumps({'text':p['user_prompt'],'provider_id':'local','model_id':'test-model'}))"
    )
    client = CommandLLMClient([sys.executable, "-c", script], timeout_seconds=10)

    result = client.complete(system_prompt="policy", user_prompt="hello", json_mode=False)

    assert result.text == "hello"
    assert result.provider_id == "local"
    assert result.model_id == "test-model"


def test_command_llm_client_preserves_literal_arguments() -> None:
    client = CommandLLMClient([sys.executable, "-c", "print('ok')", "  literal  "])

    assert client.argv[-1] == "  literal  "


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_command_llm_client_stops_oversized_child_output(stream: str) -> None:
    target = "sys.stdout" if stream == "stdout" else "sys.stderr"
    script = f"import sys; {target}.write('x' * 2100000); {target}.flush()"
    client = CommandLLMClient([sys.executable, "-c", script], timeout_seconds=10)

    with pytest.raises(ValueError, match="oversized"):
        client.complete(system_prompt="policy", user_prompt="request")


def test_feature_specific_llm_command_overrides_global(tmp_path, monkeypatch) -> None:
    global_command = json.dumps([sys.executable, "-c", "raise SystemExit(9)"])
    feature_command = json.dumps(
        [
            sys.executable,
            "-c",
            "import json,sys; json.load(sys.stdin); print(json.dumps({'text':'{}','provider_id':'p','model_id':'m'}))",
        ]
    )
    monkeypatch.setenv("EIMEMORY_LLM_COMMAND", global_command)
    monkeypatch.setenv("EIMEMORY_SOURCE_EXPANSION_LLM_COMMAND", feature_command)

    client = llm_client_from_env("SOURCE_EXPANSION")
    assert client is not None

    assert client.complete(system_prompt="policy", user_prompt="request", json_mode=True).provider_id == "p"


def test_invalid_prompt_safety_command_does_not_crash_runtime_but_stays_unavailable(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EIMEMORY_PROMPT_SAFETY_COMMAND", "not-json")

    runtime = Runtime.create(root=tmp_path)
    try:
        assert runtime.prompt_safety_executor is None
        assert runtime.prompt_safety_config_error == "ValueError"
    finally:
        runtime.close()


def test_openclaw_llm_adapter_uses_default_model_chain_when_model_is_blank(monkeypatch) -> None:
    observed = {}

    def run(argv, **kwargs):
        observed["argv"] = argv

        class Completed:
            returncode = 0
            stdout = json.dumps(
                {
                    "ok": True,
                    "provider": "openai",
                    "model": "gpt-5.6-sol",
                    "outputs": [{"text": "{\"decision\":\"approve\"}"}],
                }
            )

        return Completed()

    monkeypatch.setattr(openclaw_adapter.subprocess, "run", run)
    monkeypatch.delenv("EIMEMORY_LLM_MODEL", raising=False)

    result = openclaw_adapter.complete_request(
        {"system_prompt": "Be conservative.", "user_prompt": "Return JSON.", "json_mode": True}
    )

    assert "--model" not in observed["argv"]
    prompt = observed["argv"][observed["argv"].index("--prompt") + 1]
    assert "JSON_MODE=true" in prompt
    assert "strict JSON" in prompt
    assert result["provider_id"] == "openai"
    assert result["model_id"] == "openai/gpt-5.6-sol"
