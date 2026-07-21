from __future__ import annotations

import importlib.util
from abc import ABC, abstractmethod
from pathlib import Path
import sys
import types

from eimemory.version import __version__


PLUGIN_ROOT = Path(__file__).parents[1] / "integrations" / "hermes" / "eimemory"


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
    assert "$HERMES_HOME/plugins/eimemory" in readme
    assert "memory:" in readme and "provider: eimemory" in readme
    assert "EIMEMORY_RPC_URL" in readme
    assert "EIMEMORY_RPC_TOKEN" in readme
    assert "per_channel" in readme
    assert "embodied::channel::hermes" in readme
    assert "fail-open" in readme
    assert "full conversation history" in readme
