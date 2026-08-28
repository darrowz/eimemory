from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any

from eimemory.storage.atomic_file import atomic_write_json, locked_json_update


DELIVERY_STATE_SCHEMA = "external_channel_delivery.v1"
SIGNAL_SCHEMA = "release_closure_channel_receipt_signal.v1"
DEFAULT_STATE_PATH = Path("/var/lib/eimemory/external_channel_delivery_state.json")
DEFAULT_SIGNAL_PATH = Path(
    "/var/lib/eimemory/state/release-closure-channel-receipt.signal"
)

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_NAME_RE = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_NON_EXTERNAL_PLATFORMS = frozenset(
    {"local", "deployment-replay", "api", "api_server", "webhook"}
)
_CONVERSATION_KIND = {
    "dm": "direct",
    "direct": "direct",
    "private": "direct",
    "group": "group",
    "channel": "channel",
    "thread": "thread",
    "forum": "forum",
}
_MAX_ENTRIES = 500


def register_external_delivery_capture(
    *,
    event: Any,
    gateway: Any,
    hermes_home: str | Path | None = None,
    runtime_commit: str | None = None,
    state_path: str | Path = DEFAULT_STATE_PATH,
    signal_path: str | Path = DEFAULT_SIGNAL_PATH,
) -> None:
    """Bind a real Hermes inbound turn to its durable successful delivery.

    The pre-dispatch hook has no authority to claim platform acceptance.  It
    only registers a callback.  The callback writes evidence after Hermes has
    marked the exact session's delivery obligation ``delivered``.
    """

    try:
        source = getattr(event, "source", None)
        platform = _platform_name(getattr(source, "platform", None))
        conversation_kind = _conversation_kind(
            getattr(source, "chat_type", None)
            or getattr(event, "chat_type", None)
        )
        inbound_message_id = str(
            getattr(source, "message_id", None)
            or getattr(event, "message_id", None)
            or ""
        ).strip()
        user_id = str(
            getattr(source, "user_id", None)
            or getattr(event, "user_id", None)
            or ""
        ).strip()
        chat_id = str(getattr(source, "chat_id", None) or "").strip()
        commit = str(
            runtime_commit
            if runtime_commit is not None
            else os.environ.get("EIMEMORY_RUNTIME_COMMIT", "")
        ).strip().lower()
        if not (
            source is not None
            and platform
            and platform not in _NON_EXTERNAL_PLATFORMS
            and conversation_kind
            and inbound_message_id
            and user_id
            and chat_id
            and not bool(getattr(source, "is_bot", False))
            and _COMMIT_RE.fullmatch(commit) is not None
        ):
            return

        authorize = getattr(gateway, "_is_user_authorized", None)
        if not callable(authorize) or authorize(source) is not True:
            return

        session_key = str(gateway._session_key_for_source(source) or "").strip()
        adapter = gateway._adapter_for_source(source)
        register_callback = getattr(
            adapter, "register_post_delivery_callback", None
        )
        if not session_key or not callable(register_callback):
            return

        received_at_ms = int(time.time() * 1000)
        home = _hermes_home(hermes_home)

        def capture_after_delivery() -> None:
            try:
                delivered = _latest_delivered_obligation(
                    home / "state.db",
                    session_key=session_key,
                    platform=platform,
                    chat_id=chat_id,
                    inbound_message_id=inbound_message_id,
                    received_at_ms=received_at_ms,
                )
                if delivered is None:
                    return
                obligation_id, updated_at = delivered
                accepted_at_ms = max(received_at_ms, int(updated_at * 1000))
                _persist_delivery(
                    state_path=Path(state_path),
                    signal_path=Path(signal_path),
                    platform=platform,
                    conversation_kind=conversation_kind,
                    inbound_message_id=inbound_message_id,
                    obligation_id=obligation_id,
                    runtime_commit=commit,
                    received_at_ms=received_at_ms,
                    accepted_at_ms=accepted_at_ms,
                )
            except Exception:
                # Evidence capture must never break the user's Hermes turn.
                return

        register_callback(session_key, capture_after_delivery)
    except Exception:
        # Host integration is observational and fail-closed.
        return


def _latest_delivered_obligation(
    database_path: Path,
    *,
    session_key: str,
    platform: str,
    chat_id: str,
    inbound_message_id: str,
    received_at_ms: int,
) -> tuple[str, float] | None:
    if database_path.is_symlink() or not database_path.is_file():
        return None
    connection = sqlite3.connect(
        f"file:{database_path}?mode=ro",
        uri=True,
        timeout=5,
    )
    try:
        row = connection.execute(
            """SELECT obligation_id, content, updated_at
               FROM delivery_obligations
               WHERE session_key = ?
                 AND platform = ?
                 AND chat_id = ?
                 AND state = 'delivered'
                 AND created_at >= ?
               ORDER BY updated_at DESC, obligation_id DESC
               LIMIT 1""",
            (session_key, platform, chat_id, received_at_ms / 1000),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    obligation_id = str(row[0] or "").strip()
    content = str(row[1] or "")
    expected_obligation_id = sha256(
        f"{session_key}|{inbound_message_id}|{content}".encode(
            "utf-8", "replace"
        )
    ).hexdigest()[:24]
    try:
        updated_at = float(row[2])
    except (TypeError, ValueError):
        return None
    if obligation_id != expected_obligation_id or updated_at <= 0:
        return None
    return obligation_id, updated_at


def _persist_delivery(
    *,
    state_path: Path,
    signal_path: Path,
    platform: str,
    conversation_kind: str,
    inbound_message_id: str,
    obligation_id: str,
    runtime_commit: str,
    received_at_ms: int,
    accepted_at_ms: int,
) -> None:
    entry_id = sha256(
        f"hermes:{platform}:{inbound_message_id}:{obligation_id}".encode("utf-8")
    ).hexdigest()
    entry = {
        "status": "platform_accepted",
        "transport_owner": "hermes",
        "platform": platform,
        "conversation_kind": conversation_kind,
        "inbound_message_id": inbound_message_id,
        "delivery_receipt_id": obligation_id,
        "runtime_commit": runtime_commit,
        "received_at_ms": received_at_ms,
        "platform_accepted_at_ms": accepted_at_ms,
    }

    def update(document: Any) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise ValueError("invalid external channel delivery state")
        schema = str(document.get("schema_version") or "")
        entries = document.get("entries")
        if schema != DELIVERY_STATE_SCHEMA or not isinstance(entries, dict):
            raise ValueError("invalid external channel delivery state contract")
        entries[entry_id] = entry
        ordered = sorted(
            entries.items(),
            key=lambda item: (
                int(
                    item[1].get("platform_accepted_at_ms", 0)
                    if isinstance(item[1], dict)
                    else 0
                ),
                item[0],
            ),
            reverse=True,
        )[:_MAX_ENTRIES]
        return {
            "schema_version": DELIVERY_STATE_SCHEMA,
            "entries": dict(ordered),
        }

    locked_json_update(
        state_path,
        update,
        default={"schema_version": DELIVERY_STATE_SCHEMA, "entries": {}},
        expected_type=dict,
    )
    atomic_write_json(
        signal_path,
        {
            "schema_version": SIGNAL_SCHEMA,
            "runtime_commit": runtime_commit,
            "platform_accepted_at_ms": accepted_at_ms,
        },
    )


def _platform_name(value: Any) -> str:
    normalized = str(getattr(value, "value", value) or "").strip().lower()
    return normalized if _NAME_RE.fullmatch(normalized) is not None else ""


def _conversation_kind(value: Any) -> str:
    return _CONVERSATION_KIND.get(str(value or "").strip().lower(), "")


def _hermes_home(value: str | Path | None) -> Path:
    if value is not None:
        return Path(value)
    configured = str(os.environ.get("HERMES_HOME", "") or "").strip()
    return Path(configured) if configured else Path.home() / ".hermes"
