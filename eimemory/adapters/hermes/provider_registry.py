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

