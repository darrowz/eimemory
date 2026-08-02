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

    assert set(registered.keys()) == {"pre_llm_call", "post_llm_call", "post_tool_call"}


def test_hermes_hook_plugin_metadata_and_contract() -> None:
    metadata = (HOOK_PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8")
    readme = (HOOK_PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")

    assert "name: eimemory-hook" in metadata
    assert f"version: {__version__}" in metadata
    assert "provides_hooks:" in metadata
    assert "eimemory_hook" in readme
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
