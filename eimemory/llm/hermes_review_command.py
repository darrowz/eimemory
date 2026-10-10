"""Isolated STDIO review bridge inside the discovered Hermes installation.

Import only the SDK router, never a CLI conversation, agent, tools or plugins.
No deployment-specific interpreter, home, provider or model is embedded here.
"""
from __future__ import annotations

from contextlib import contextmanager
import inspect
import importlib
import importlib.util
import json
import logging
import os
import sys
import time


@contextmanager
def _stage(timings, name):
    started = time.monotonic()
    try:
        yield
    finally:
        timings[name] = round((time.monotonic() - started) * 1000, 3)


def _configuration():
    from hermes_cli.runtime_provider import _get_model_config
    config = _get_model_config()
    if (not isinstance(config, dict) or not isinstance(config.get("default"), str)
            or not config["default"].strip()
            or (config.get("provider") is not None and not isinstance(config["provider"], str))):
        raise ValueError("research_review_hermes_configuration_invalid")
    return config


def _load_environment():
    # Hermes owns home/profile credential loading, URL routing and wire adapters.
    loader = None
    if importlib.util.find_spec("hermes_cli.env_loader") is not None:
        loader = getattr(importlib.import_module("hermes_cli.env_loader"), "load_hermes_dotenv", None)
    if not callable(loader):
        from hermes_cli.config import load_env
        values = load_env()
        if not isinstance(values, dict):
            raise ValueError("invalid_host_environment")
        for key, value in values.items():
            if (isinstance(key, str) and isinstance(value, str)
                    and key not in {"HOME", "PATH", "HERMES_HOME", "USERPROFILE"}
                    and not key.startswith(("PYTHON", "EIMEMORY_"))):
                os.environ[key] = value
    else:
        loader()


def _client():
    _load_environment()
    config = _configuration()
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from agent.auxiliary_client import resolve_provider_client
    runtime = resolve_runtime_provider(requested=config.get("provider"), target_model=config["default"])
    if not isinstance(runtime, dict) or runtime.get("api_mode") in {"codex_app_server", "acp"}:
        raise ValueError("tool_free_runtime_unavailable")
    provider = runtime.get("provider")
    if not isinstance(provider, str) or not provider.strip() or provider == "auto":
        raise ValueError("provider_identity_unavailable")
    model = runtime.get("model") or config["default"]
    kwargs = {"model": model}
    optional = {"explicit_base_url": runtime.get("base_url"),
                "explicit_api_key": runtime.get("api_key"), "api_mode": runtime.get("api_mode"),
                "main_runtime": runtime}
    parameters = inspect.signature(resolve_provider_client).parameters
    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())
    for key, value in optional.items():
        if key in parameters or accepts_kwargs:
            kwargs[key] = value
        elif key != "main_runtime" and value is not None:
            # An older router must support the resolved transport/endpoint;
            # silently dropping one could send private artifacts to another route.
            raise ValueError("runtime_router_incompatible")
    client, resolved_model = resolve_provider_client(provider, **kwargs)
    if client is None or not isinstance(resolved_model, str) or not resolved_model.strip():
        raise ValueError("model_unavailable")
    actual_provider = getattr(client, "_hermes_aux_effective_provider", None)
    if isinstance(actual_provider, str) and actual_provider.strip():
        provider = actual_provider
    return client, resolved_model, provider, runtime.get("base_url")


def _reasoning_configuration(model):
    from hermes_cli import config as host_config
    import hermes_constants
    read_config = getattr(host_config, "load_config_readonly", None) or host_config.load_config
    config = read_config()
    resolve_reasoning = getattr(hermes_constants, "resolve_reasoning_config", None)
    if callable(resolve_reasoning):
        return resolve_reasoning(config, model)
    else:
        agent_config = config.get("agent") or {}
        if agent_config.get("reasoning_overrides"):
            raise ValueError("reasoning_router_incompatible")
        return hermes_constants.parse_reasoning_effort(agent_config.get("reasoning_effort", ""))


def _completion_kwargs(provider, model, base_url, messages, remaining):
    from agent import auxiliary_client
    reasoning = _reasoning_configuration(model)
    builder = getattr(auxiliary_client, "_build_call_kwargs", None)
    if callable(builder):
        _check_builder(builder)
        # The installed provider profiles translate reasoning to the correct
        # SDK wire shape; do not hardcode OpenAI fields for other transports.
        payload = builder(provider=provider, model=model, messages=messages,
                          timeout=remaining, base_url=base_url, reasoning_config=reasoning)
    else:
        if reasoning is not None:
            raise ValueError("reasoning_router_incompatible")
        payload = {"model": model, "messages": messages, "timeout": remaining}
    if (not isinstance(payload, dict) or payload.get("model") != model
            or payload.get("messages") != messages or payload.get("tools") or payload.get("functions")
            or not isinstance(payload.get("timeout"), (int, float))
            or not 0 < payload["timeout"] <= remaining):
        raise ValueError("tool_free_request_invalid")
    return payload


def _check_builder(builder):
    # Check the installed API rather than pinning a Hermes release number.
    inspect.signature(builder).bind(provider="", model="", messages=[], timeout=1,
                                    base_url=None, reasoning_config=None)


def complete(request, timings):
    system, user = request["system_prompt"], request["user_prompt"]
    if (not isinstance(system, str) or not isinstance(user, str)
            or len((system + user).encode()) > 131072):
        raise ValueError("invalid_prompt")
    with _stage(timings, "bridge_client_setup_ms"):
        client, model, provider, base_url = _client()
        remaining = request["deadline_unix_ms"] / 1000 - time.time()
        if remaining <= 0:
            raise ValueError("deadline_expired")
        kwargs = _completion_kwargs(provider, model, base_url, [
            {"role": "system", "content": system}, {"role": "user", "content": user}], remaining)
    remaining = request["deadline_unix_ms"] / 1000 - time.time()
    if remaining <= 0:
        raise ValueError("deadline_expired")
    try:
        with _stage(timings, "provider_response_ms"):
            kwargs["timeout"] = min(kwargs["timeout"], remaining)
            result = client.chat.completions.create(**kwargs)
        with _stage(timings, "bridge_response_validation_ms"):
            if getattr(result, "model", None) != model:
                raise ValueError("response_model_mismatch")
            message = result.choices[0].message
            if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
                raise ValueError("unexpected_tool_call")
            text = message.content
            if not isinstance(text, str) or not text.strip():
                raise ValueError("empty_response")
            if request.get("json_mode"):
                json.loads(text)
            return {"text": text, "provider_id": provider, "model_id": result.model}
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def main():
    sys.dont_write_bytecode = True
    started = time.monotonic()
    output = os.dup(1)
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    os.close(null)
    logging.disable(logging.CRITICAL)
    timings = {}
    category = "bridge_output_invalid"
    check = False
    try:
        request = json.loads(sys.stdin.buffer.read(131073))
        check = request == {"configuration_check": True}
        category = "bridge_import_failed"
        with _stage(timings, "bridge_import_ms"):
            from hermes_cli.runtime_provider import _get_model_config  # noqa: F401
            from agent.auxiliary_client import resolve_provider_client  # noqa: F401
        if check:
            _load_environment()
            config = _configuration()
            reasoning = _reasoning_configuration(config["default"])
            from agent import auxiliary_client
            if reasoning is not None and not callable(getattr(auxiliary_client, "_build_call_kwargs", None)):
                raise ValueError("reasoning_router_incompatible")
            builder = getattr(auxiliary_client, "_build_call_kwargs", None)
            if callable(builder):
                _check_builder(builder)
            payload = {"configuration_ok": True, "error": ""}
        else:
            category = "bridge_client_setup_failed"
            payload = complete(request, timings)
            timings["bridge_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            payload["diagnostics"] = timings
        os.write(output, json.dumps(payload, ensure_ascii=False, allow_nan=False).encode())
        return 0
    except Exception as exc:
        if check:
            error = ("research_review_hermes_configuration_invalid"
                     if str(exc) == "research_review_hermes_configuration_invalid"
                     else "research_review_hermes_runtime_unavailable")
            payload = {"configuration_ok": False, "error": error}
        else:
            if "bridge_response_validation_ms" in timings:
                category = "bridge_response_validation_failed"
            elif "provider_response_ms" in timings:
                category = "provider_request_failed"
            timings["bridge_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            payload = {"schema": "eimemory.command-failure.v1", "error": category, "diagnostics": timings}
        os.write(output, json.dumps(payload, allow_nan=False).encode())
        return 1
    finally:
        os.close(output)


if __name__ == "__main__":
    raise SystemExit(main())
