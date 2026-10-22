from __future__ import annotations

from threading import RLock
from typing import Any
from weakref import WeakValueDictionary
from pathlib import Path
from contextlib import contextmanager

from eimemory.adapters.hermes.profile_context import current_home


_LOCK = RLock()
_PROVIDERS: WeakValueDictionary[tuple[str,str], Any] = WeakValueDictionary()


def _owner(provider):
    home=str(getattr(provider,'_hermes_home','') or '')
    return str(Path(home).expanduser().resolve()) if home else ''


def bind_hermes_provider(*, session_id: str, provider: Any) -> None:
    """Bind the MemoryManager-owned provider to one official Hermes session."""

    normalized = str(session_id or "").strip()
    if not normalized:
        raise ValueError("session_id is required")
    with _LOCK:
        key=(_owner(provider),normalized)
        previous=_PROVIDERS.get(key)
        if previous is not None and previous is not provider:
            raise ValueError('hermes_provider_session_already_bound')
        _PROVIDERS[key] = provider


def move_hermes_provider(*, old_session_id: str, new_session_id: str, provider: Any) -> None:
    """Move a provider binding after Hermes rotates its session identifier."""

    old_session = str(old_session_id or "").strip()
    new_session = str(new_session_id or "").strip()
    if not new_session:
        raise ValueError("new_session_id is required")
    with _LOCK:
        old_key=(_owner(provider),old_session);new_key=(_owner(provider),new_session)
        previous=_PROVIDERS.get(new_key)
        if previous is not None and previous is not provider:
            raise ValueError('hermes_provider_session_already_bound')
        if old_session and _PROVIDERS.get(old_key) is provider:
            _PROVIDERS.pop(old_key, None)
        _PROVIDERS[new_key] = provider


@contextmanager
def moving_hermes_provider(*, old_session_id: str, new_session_id: str, provider: Any):
    """Reserve the new owner before the provider mutates its session state."""
    old=(_owner(provider),str(old_session_id or '').strip())
    new=(_owner(provider),str(new_session_id or '').strip())
    if not new[1]:raise ValueError('new_session_id is required')
    with _LOCK:
        current=_PROVIDERS.get(new)
        if current is not None and current is not provider:
            raise ValueError('hermes_provider_session_already_bound')
        _PROVIDERS[new]=provider
    try:yield
    except BaseException:
        with _LOCK:
            if new!=old and _PROVIDERS.get(new) is provider:_PROVIDERS.pop(new,None)
        raise
    else:
        with _LOCK:
            if old!=new and _PROVIDERS.get(old) is provider:_PROVIDERS.pop(old,None)


def get_hermes_provider(session_id: str, *, hermes_home: str | None = None) -> Any | None:
    """Return the exact provider instance owned by Hermes MemoryManager."""

    normalized = str(session_id or "").strip()
    if not normalized:
        return None
    try:home=current_home() if hermes_home is None else str(Path(hermes_home).expanduser().resolve())
    except (OSError,ValueError,RuntimeError):return None
    with _LOCK:
        if home:return _PROVIDERS.get((home,normalized))
        # Offline embedders without a native home can inspect an unambiguous
        # binding. A duplicated session in two profiles never picks a winner.
        candidates=[provider for (_home,session),provider in _PROVIDERS.items() if session==normalized]
        return candidates[0] if len(candidates)==1 else None


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
