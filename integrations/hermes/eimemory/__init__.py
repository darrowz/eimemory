"""Standalone Hermes MemoryProvider registration for eimemory."""

from __future__ import annotations

from typing import Any

try:
    from agent.memory_provider import MemoryProvider
except ImportError:  # Allows package validation without making Hermes an eimemory dependency.
    class MemoryProvider:
        pass

from eimemory.adapters.hermes.provider_core import HermesMemoryProviderCore, hermes_client_from_env
from eimemory.adapters.runtime.host_auth import (
    producer_token_from_private_file,
    scrub_producer_credential_environment,
)
from eimemory.adapters.runtime.receipt_handoff import ReceiptIdHandoff


class EIMemoryProvider(HermesMemoryProviderCore, MemoryProvider):
    """Hermes-native provider backed by the authenticated eimemory RPC."""


def register(ctx) -> None:
    provider = EIMemoryProvider()
    ctx.register_memory_provider(provider)
    token = producer_token_from_private_file("hermes")
    attestation_client = hermes_client_from_env() if token else None
    if attestation_client is not None:
        if attestation_client.auth_token == token:
            attestation_client = None
        else:
            attestation_client.auth_token = token
    receipt_handoff = ReceiptIdHandoff.from_env()
    scrub_producer_credential_environment()

    def post_tool_call(tool_name: str, args: Any, result: Any, task_id: str, duration_ms: int, **kwargs: Any) -> None:
        """Host-owned Hermes evidence producer; never a model-callable provider method."""
        if attestation_client is None:
            return
        session_id = str(kwargs.get("session_id") or provider._session_id or "").strip()
        run_id = str(kwargs.get("turn_id") or kwargs.get("api_request_id") or task_id or "").strip()
        tool_call_id = str(kwargs.get("tool_call_id") or kwargs.get("tool_correlation_id") or task_id or "").strip()
        if not all((session_id, run_id, tool_call_id, str(tool_name or "").strip())):
            return
        try:
            response = attestation_client.call_or_bypass(
                "adapter.attest_tool_result",
                {
                    "channel": "hermes", "scope": dict(provider._scope), "session_id": session_id,
                    "run_id": run_id, "tool_call_id": tool_call_id, "tool_name": str(tool_name)[:200],
                    "tool_input": args, "result": result,
                    "duration_ms": int(duration_ms) if isinstance(duration_ms, int) else 0,
                },
            )
            response_result = response.get("result") if isinstance(response, dict) else None
            receipt = response_result.get("receipt") if isinstance(response_result, dict) else None
            receipt_id = response_result.get("receipt_id") if isinstance(response_result, dict) else ""
            if (
                receipt_handoff is not None
                and isinstance(receipt, dict)
                and receipt.get("passed") is True
                and isinstance(receipt_id, str)
                and receipt_id
            ):
                receipt_handoff.append(
                    channel="hermes",
                    scope=dict(provider._scope),
                    session_id=session_id,
                    run_id=run_id,
                    receipt_id=receipt_id,
                )
                provider.bind_verified_host_turn(session_id=session_id, turn_id=run_id)
        except Exception:
            return

    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        register_hook("post_tool_call", post_tool_call)
