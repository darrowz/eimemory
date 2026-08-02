"""Standalone Hermes MemoryProvider registration for eimemory."""

from __future__ import annotations

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # Allows package validation without Hermes dependency.
    class MemoryProvider:
        pass

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore
from eimemory.adapters.hermes.provider_registry import (
    bind_hermes_provider,
    move_hermes_provider,
    unbind_hermes_provider,
)


class EIMemoryProvider(HermesMemoryProviderCore, MemoryProvider):
    """Hermes-native provider backed by the authenticated eimemory RPC."""

    def initialize(self, session_id: str, **kwargs) -> None:
        super().initialize(session_id, **kwargs)
        bind_hermes_provider(session_id=self._session_id, provider=self)

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        old_session_id = self._session_id
        super().on_session_switch(
            new_session_id,
            parent_session_id=parent_session_id,
            reset=reset,
            rewound=rewound,
            **kwargs,
        )
        move_hermes_provider(
            old_session_id=old_session_id,
            new_session_id=self._session_id,
            provider=self,
        )

    def shutdown(self) -> None:
        try:
            super().shutdown()
        finally:
            unbind_hermes_provider(self)


def register(ctx) -> None:
    ctx.register_memory_provider(EIMemoryProvider())
