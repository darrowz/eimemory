from __future__ import annotations

from threading import RLock
from typing import Any
from weakref import WeakValueDictionary


_LOCK = RLock()
_PROVIDERS: WeakValueDictionary[str, Any] = WeakValueDictionary()


def bind_hermes_provider(*, session_id: str, provider: Any) -> None:
    """Bind the MemoryManager-owned provider to one official Hermes session."""

    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("session_id is required")
    with _LOCK:
        _PROVIDERS[normalized] = provider


def move_hermes_provider(*, old_session_id: str, new_session_id: str, provider: Any) -> None:
    """Move a provider binding after Hermes rotates its session identifier."""

    old_session = str(old_session_id or "").strip()
    new_session = str(new_session_id or "").strip()
    if not new_session:
        raise ValueError("new_session_id is required")
    with _LOCK:
        if old_session and _PROVIDERS.get(old_session) is provider:
            _PROVIDERS.pop(old_session, None)
        _PROVIDERS[new_session] = provider


def get_hermes_provider(session_id: str) -> Any | None:
    """Return the exact provider instance owned by Hermes MemoryManager."""

    normalized = str(session_id or "").strip()
    if not normalized:
        return None
    with _LOCK:
        return _PROVIDERS.get(normalized)


def unbind_hermes_provider(provider: Any) -> None:
    """Remove every session binding for a provider during shutdown."""

    with _LOCK:
        stale = [session_id for session_id, value in _PROVIDERS.items() if value is provider]
        for session_id in stale:
            _PROVIDERS.pop(session_id, None)


def advertise_hermes_capabilities(
    *,
    session_id: str,
    adapter_context: dict[str, Any],
    now: str = "",
) -> dict[str, Any]:
    """Route an internal advertisement to the provider owned by Hermes.

    The registry never creates a shadow provider or model wrapper.  Absence is
    reported explicitly so a host cannot mistake a guessed session for a live
    capability statement.
    """

    provider = get_hermes_provider(session_id)
    method = getattr(provider, "advertise_capabilities", None) if provider is not None else None
    if not callable(method):
        return {"ok": False, "status": "unsupported", "reason": "provider_not_bound"}
    try:
        result = method(adapter_context, now=now)
    except Exception:
        return {"ok": False, "status": "rejected", "reason": "provider_advertisement_failed"}
    return result if isinstance(result, dict) else {
        "ok": False,
        "status": "rejected",
        "reason": "provider_advertisement_failed",
    }


def hermes_capability_health(
    *,
    session_id: str,
    binding_id: str,
    capability_scope: str = "global",
    at_time: str = "",
) -> dict[str, Any]:
    """Read one bound Hermes provider's internal advertisement health."""

    provider = get_hermes_provider(session_id)
    method = getattr(provider, "capability_health", None) if provider is not None else None
    if not callable(method):
        return {"ok": False, "status": "unsupported", "reason": "provider_not_bound"}
    try:
        result = method(
            binding_id,
            capability_scope=capability_scope,
            at_time=at_time,
        )
    except Exception:
        return {"ok": False, "status": "unknown", "reason": "provider_health_failed"}
    return result if isinstance(result, dict) else {
        "ok": False,
        "status": "unknown",
        "reason": "provider_health_failed",
    }


def normalize_hermes_capability_outcome(
    *,
    session_id: str,
    event_type: str,
    event: dict[str, Any] | None,
    capability_scope: str = "global",
) -> dict[str, Any]:
    """Route only an explicit outcome envelope to the bound provider."""

    provider = get_hermes_provider(session_id)
    method = getattr(provider, "normalize_capability_outcome", None) if provider is not None else None
    if not callable(method):
        return {"ok": False, "status": "unsupported", "reason": "provider_not_bound"}
    try:
        result = method(event_type, event, capability_scope=capability_scope)
    except Exception:
        return {"ok": False, "status": "unsupported", "reason": "provider_outcome_failed"}
    return result if isinstance(result, dict) else {
        "ok": False,
        "status": "unsupported",
        "reason": "provider_outcome_failed",
    }
