from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from typing import Any
from pathlib import Path
import sys
import types

from eimemory.adapters.hermes.provider_registry import get_hermes_provider
from eimemory.version import __version__


PLUGIN_ROOT = Path(__file__).parents[1] / "integrations" / "hermes" / "eimemory"
HOOK_PLUGIN_ROOT = Path(__file__).parents[1] / "integrations" / "hermes" / "eimemory_hook"


def test_hermes_standalone_plugin_registers_memory_provider_without_core_changes(monkeypatch) -> None:
    agent_package = types.ModuleType("agent")
    memory_provider_module = types.ModuleType("agent.memory_provider")

    class MemoryProvider(ABC):
        @property
        @abstractmethod
        def name(self) -> str: ...

        @abstractmethod
        def is_available(self) -> bool: ...

        @abstractmethod
        def initialize(self, session_id: str, **kwargs) -> None: ...

        @abstractmethod
        def get_tool_schemas(self) -> list[dict]: ...

    memory_provider_module.MemoryProvider = MemoryProvider
    monkeypatch.setitem(sys.modules, "agent", agent_package)
    monkeypatch.setitem(sys.modules, "agent.memory_provider", memory_provider_module)
    spec = importlib.util.spec_from_file_location("eimemory_hermes_plugin", PLUGIN_ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Context:
        def __init__(self) -> None:
            self.provider = None

        def register_memory_provider(self, provider) -> None:
            self.provider = provider

    context = Context()
    module.register(context)

    assert context.provider is not None
    assert context.provider.name == "eimemory"
    assert issubclass(module.EIMemoryProvider, MemoryProvider)


def test_hermes_plugin_metadata_and_reproducible_install_contract() -> None:
    metadata = (PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    readme = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "name: eimemory" in metadata
    assert f"version: {__version__}" in metadata
    assert "kind: exclusive" in metadata
    assert "$HERMES_HOME/plugins/eimemory" in readme
    assert "memory:" in readme and "provider: eimemory" in readme
    assert "EIMEMORY_RPC_URL" in readme
    assert "EIMEMORY_RPC_TOKEN" in readme
    assert "per_channel" in readme
    assert "embodied::channel::hermes" in readme
    assert "fail-open" in readme
    assert "full conversation history" in readme


def test_hermes_hook_plugin_registers_official_host_callbacks() -> None:
    registered: dict[str, Any] = {}

    class Context:
        def register_memory_provider(self, provider) -> None:
            raise AssertionError("hook plugin must not register memory provider")

        def register_hook(self, name: str, callback) -> None:
            registered[name] = callback

    spec = importlib.util.spec_from_file_location(
        "eimemory_hermes_hook_plugin", HOOK_PLUGIN_ROOT / "__init__.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.register(Context())

    assert set(registered.keys()) == {
        "pre_gateway_dispatch",
        "pre_llm_call",
        "post_llm_call",
        "post_tool_call",
    }


def test_hermes_hook_plugin_metadata_and_contract() -> None:
    metadata = (HOOK_PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    readme = (HOOK_PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "name: eimemory-hook" in metadata
    assert f"version: {__version__}" in metadata
    assert "provides_hooks:" in metadata
    assert "eimemory_hook" in readme
    assert "pre_gateway_dispatch" in readme
    assert "pre_llm_call" in readme
    assert "post_tool_call" in readme
    assert "EIMEMORY_HERMES_ATTESTATION_TOKEN_FILE" in readme


def test_official_synthetic_loader_and_hook_share_exact_session_provider(monkeypatch) -> None:
    provider_spec = importlib.util.spec_from_file_location(
        "_hermes_user_memory.eimemory",
        PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert provider_spec is not None and provider_spec.loader is not None
    provider_module = importlib.util.module_from_spec(provider_spec)
    monkeypatch.setitem(sys.modules, provider_spec.name, provider_module)
    provider_spec.loader.exec_module(provider_module)

    class ProviderContext:
        provider = None

        def register_memory_provider(self, provider) -> None:
            self.provider = provider

    provider_context = ProviderContext()
    provider_module.register(provider_context)
    provider = provider_context.provider
    assert provider is not None
    provider.initialize("official-session", platform="gateway")

    callbacks: dict[str, Any] = {}

    class HookContext:
        def register_hook(self, name: str, callback) -> None:
            callbacks[name] = callback

    hook_spec = importlib.util.spec_from_file_location(
        "hermes_plugins.eimemory_hook",
        HOOK_PLUGIN_ROOT / "__init__.py",
        submodule_search_locations=[str(HOOK_PLUGIN_ROOT)],
    )
    assert hook_spec is not None and hook_spec.loader is not None
    hook_module = importlib.util.module_from_spec(hook_spec)
    monkeypatch.setitem(sys.modules, hook_spec.name, hook_module)
    hook_spec.loader.exec_module(hook_module)
    hook_module.register(HookContext())

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        provider,
        "on_pre_llm_call",
        lambda **kwargs: seen.append((kwargs["session_id"], kwargs["user_message"])),
    )
    callbacks["pre_llm_call"](
        session_id="official-session",
        user_message="official loader callback",
        conversation_history=[],
        model="test",
        platform="gateway",
    )

    assert get_hermes_provider("official-session") is provider
    assert seen == [("official-session", "official loader callback")]
    provider.shutdown()


def test_execution_middleware_captures_parallel_results_without_observer_suppression(monkeypatch, tmp_path) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from integrations.hermes import eimemory_hook as hook
    from integrations.hermes.eimemory import EIMemoryProvider

    barrier = threading.Barrier(2)
    seen = []
    class Client:
        auth_token = "model-test-token"
        def call_or_bypass(self, method, params):
            barrier.wait(timeout=2)
            seen.append(params)
            return {"ok": True, "result": {"ok": True, "receipt_id": params["tool_call_id"],
                    "receipt": {"passed": True}}}
    monkeypatch.setattr(hook, "hermes_producer_token", lambda: "host-test-token")
    monkeypatch.setattr(hook, "hermes_client_from_env", Client)
    monkeypatch.setenv("EIMEMORY_ADAPTER_RECEIPT_HANDOFF_FILE", str(tmp_path / "handoff.sqlite3"))
    provider = EIMemoryProvider()
    provider.initialize("middleware-session")
    hooks, middleware = {}, {}
    ctx = types.SimpleNamespace(
        register_hook=lambda name, cb: hooks.update({name: cb}),
        register_middleware=lambda name, cb: middleware.update({name: cb}),
    )
    hook.register(ctx)
    assert "tool_execution" in middleware
    ctx._manager = types.SimpleNamespace(_middleware={"tool_execution": [middleware["tool_execution"]]})
    assert "post_tool_call" not in hooks, "do not double-attest the same host result"
    def run(call_id):
        result = {"output": "1 passed", "exit_code": 0, "error": None}
        executions = []
        def execute(args):
            executions.append(args)
            return result
        returned = middleware["tool_execution"](
            tool_name="terminal", args={"command": "python -m pytest -q"},
            next_call=execute, task_id="task", session_id="middleware-session",
            turn_id="host-turn", tool_call_id=call_id,
        )
        assert returned is result
        assert len(executions) == 1
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, ["call-1", "call-2"]))
        assert {p["tool_call_id"] for p in seen} == {"call-1", "call-2"}
        assert list(provider._verified_host_turns) == [("middleware-session", "host-turn")]
        assert len(provider._receipt_handoff.list_ids(channel="hermes", scope=provider._scope,
                   session_id="middleware-session", run_id="host-turn")) == 2
    finally:
        provider.shutdown()


def test_execution_capture_requires_an_exact_inspectable_chain(monkeypatch) -> None:
    from integrations.hermes import eimemory_hook as hook
    calls = []
    class Client:
        auth_token = "model-test-token"
        def call_or_bypass(self, *args):
            calls.append(args)
            return {}
    monkeypatch.setattr(hook, "hermes_producer_token", lambda: "host-test-token")
    monkeypatch.setattr(hook, "hermes_client_from_env", Client)
    monkeypatch.setattr(hook, "get_hermes_provider", lambda session: types.SimpleNamespace(_scope={}))
    middleware = {}
    ctx = types.SimpleNamespace(register_middleware=lambda name, cb: middleware.update({name: cb}))
    hook.register(ctx)
    callback = middleware["tool_execution"]
    for manager in (None, types.SimpleNamespace(), types.SimpleNamespace(_middleware={}),
                    types.SimpleNamespace(_middleware={"tool_execution": [callback, lambda: None]})):
        ctx._manager = manager
        returned = callback("terminal", {}, lambda args: "real-result", session_id="s", turn_id="t", tool_call_id="c")
        assert returned == "real-result"
    assert calls == [], "uncertain executed arguments must not be attested"


def test_session_registry_keeps_concurrent_gateway_providers_isolated() -> None:
    first = __import__(
        "integrations.hermes.eimemory", fromlist=["EIMemoryProvider"]
    ).EIMemoryProvider()
    second = __import__(
        "integrations.hermes.eimemory", fromlist=["EIMemoryProvider"]
    ).EIMemoryProvider()
    first.initialize("gateway-session-a", platform="gateway")
    second.initialize("gateway-session-b", platform="gateway")
    try:
        assert get_hermes_provider("gateway-session-a") is first
        assert get_hermes_provider("gateway-session-b") is second
        first.on_session_switch("gateway-session-a2", parent_session_id="gateway-session-a")
        assert get_hermes_provider("gateway-session-a") is None
        assert get_hermes_provider("gateway-session-a2") is first
        assert get_hermes_provider("gateway-session-b") is second
    finally:
        first.shutdown()
        second.shutdown()
