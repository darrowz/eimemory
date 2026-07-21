"""Standalone Hermes MemoryProvider registration for eimemory."""

from __future__ import annotations

import os
from typing import Any

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # Allows package validation without making Hermes an eimemory dependency.
    class MemoryProvider:
        pass

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore, hermes_client_from_env


class EIMemoryProvider(HermesMemoryProviderCore, MemoryProvider):
    """Hermes-native provider backed by the authenticated eimemory RPC."""


def register(ctx) -> None:
    provider = EIMemoryProvider()
    ctx.register_memory_provider(provider)

    def post_tool_call(tool_name: str, args: Any, result: Any, task_id: str, duration_ms: int, **kwargs: Any) -> None:
        """Host-owned Hermes evidence producer; never a model-callable provider method."""
        token = os.getenv("EIMEMORY_ATTESTATION_TOKEN", "").strip()
        if not token:
            return
        session_id = str(kwargs.get("session_id") or provider._session_id or "").strip()
        run_id = str(kwargs.get("turn_id") or kwargs.get("api_request_id") or task_id or "").strip()
        tool_call_id = str(kwargs.get("tool_call_id") or kwargs.get("tool_correlation_id") or task_id or "").strip()
        if not all((session_id, run_id, tool_call_id, str(tool_name or "").strip())):
            return
        client = hermes_client_from_env()
        client.auth_token = token
        try:
            client.call_or_bypass(
                "adapter.attest_tool_result",
                {
                    "channel": "hermes", "scope": dict(provider._scope), "session_id": session_id,
                    "run_id": run_id, "tool_call_id": tool_call_id, "tool_name": str(tool_name)[:200],
                    "result": result, "duration_ms": int(duration_ms) if isinstance(duration_ms, int) else 0,
                },
            )
        except Exception:
            return

    ctx.register_hook("post_tool_call", post_tool_call)
