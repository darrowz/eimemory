"""Standalone Hermes MemoryProvider registration for eimemory."""

from __future__ import annotations

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # Allows package validation without making Hermes an eimemory dependency.
    class MemoryProvider:
        pass

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore


class EIMemoryProvider(HermesMemoryProviderCore, MemoryProvider):
    """Hermes-native provider backed by the authenticated eimemory RPC."""


def register(ctx) -> None:
    ctx.register_memory_provider(EIMemoryProvider())
