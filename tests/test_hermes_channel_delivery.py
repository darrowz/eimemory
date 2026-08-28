from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest

from eimemory.adapters.hermes.channel_delivery import (
    register_external_delivery_capture,
)


@dataclass
class _Platform:
    value: str


@dataclass
class _Source:
    platform: _Platform
    chat_id: str = "chat-1"
    chat_type: str = "dm"
    user_id: str = "user-1"
    is_bot: bool = False
    message_id: str = "inbound-1"


@dataclass
class _Event:
    source: _Source
    message_id: str = "inbound-1"
    user_id: str = "user-1"


class _Adapter:
    def __init__(self) -> None:
        self.callbacks: dict[str, object] = {}

    def register_post_delivery_callback(self, session_key, callback, **_kwargs) -> None:
        self.callbacks[session_key] = callback


class _Gateway:
    def __init__(self, adapter: _Adapter, *, authorized: bool = True) -> None:
        self.adapter = adapter
        self.authorized = authorized

    def _session_key_for_source(self, _source) -> str:
        return "agent:main:telegram:dm:chat-1"

    def _adapter_for_source(self, _source) -> _Adapter:
        return self.adapter

    def _is_user_authorized(self, _source) -> bool:
        return self.authorized


def _create_ledger(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE delivery_obligations (
                obligation_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT,
                content TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                owner_pid INTEGER,
                owner_started_at INTEGER,
                last_error TEXT
            )"""
        )


def _insert_obligation(
    path: Path,
    *,
    state: str = "delivered",
    session_key: str = "agent:main:telegram:dm:chat-1",
    platform: str = "telegram",
    chat_id: str = "chat-1",
    obligation_id: str = "",
) -> str:
    now = time.time()
    content = "raw assistant response"
    resolved_obligation_id = obligation_id or sha256(
        f"{session_key}|inbound-1|{content}".encode("utf-8")
    ).hexdigest()[:24]
    with sqlite3.connect(path) as connection:
        connection.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, content, state,
                created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                resolved_obligation_id,
                session_key,
                platform,
                chat_id,
                content,
                state,
                now,
                now,
            ),
        )
    return resolved_obligation_id


def test_hermes_capture_persists_only_after_durable_delivery(tmp_path) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    ledger = hermes_home / "state.db"
    _create_ledger(ledger)
    state_path = tmp_path / "external-delivery.json"
    signal_path = tmp_path / "channel-receipt.signal"
    adapter = _Adapter()
    gateway = _Gateway(adapter)
    event = _Event(source=_Source(platform=_Platform("telegram")))
    commit = "a" * 40

    register_external_delivery_capture(
        event=event,
        gateway=gateway,
        hermes_home=hermes_home,
        runtime_commit=commit,
        state_path=state_path,
        signal_path=signal_path,
    )
    assert not state_path.exists()
    obligation_id = _insert_obligation(ledger)

    callback = adapter.callbacks["agent:main:telegram:dm:chat-1"]
    callback()

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["schema_version"] == "external_channel_delivery.v1"
    assert len(state["entries"]) == 1
    entry = next(iter(state["entries"].values()))
    assert entry == {
        "status": "platform_accepted",
        "transport_owner": "hermes",
        "platform": "telegram",
        "conversation_kind": "direct",
        "inbound_message_id": "inbound-1",
        "delivery_receipt_id": obligation_id,
        "runtime_commit": commit,
        "received_at_ms": entry["received_at_ms"],
        "platform_accepted_at_ms": entry["platform_accepted_at_ms"],
    }
    assert entry["platform_accepted_at_ms"] >= entry["received_at_ms"]
    assert os.stat(state_path).st_mode & 0o777 == 0o600
    assert json.loads(signal_path.read_text(encoding="utf-8")) == {
        "schema_version": "release_closure_channel_receipt_signal.v1",
        "runtime_commit": commit,
        "platform_accepted_at_ms": entry["platform_accepted_at_ms"],
    }


@pytest.mark.parametrize(
    ("source_overrides", "commit"),
    [
        ({"is_bot": True}, "a" * 40),
        ({"message_id": ""}, "a" * 40),
        ({"user_id": ""}, "a" * 40),
        ({"platform": _Platform("local")}, "a" * 40),
        ({"chat_type": "synthetic"}, "a" * 40),
        ({}, "not-a-commit"),
    ],
)
def test_hermes_capture_rejects_non_external_or_unbound_events(
    tmp_path, source_overrides, commit
) -> None:
    source = _Source(platform=_Platform("telegram"))
    for key, value in source_overrides.items():
        setattr(source, key, value)
    adapter = _Adapter()

    register_external_delivery_capture(
        event=_Event(source=source, message_id=source.message_id, user_id=source.user_id),
        gateway=_Gateway(adapter),
        hermes_home=tmp_path,
        runtime_commit=commit,
        state_path=tmp_path / "state.json",
        signal_path=tmp_path / "signal.json",
    )

    assert adapter.callbacks == {}


@pytest.mark.parametrize("state", ["pending", "attempting", "failed"])
def test_hermes_capture_rejects_non_delivered_obligation(tmp_path, state) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    ledger = hermes_home / "state.db"
    _create_ledger(ledger)
    adapter = _Adapter()

    register_external_delivery_capture(
        event=_Event(source=_Source(platform=_Platform("telegram"))),
        gateway=_Gateway(adapter),
        hermes_home=hermes_home,
        runtime_commit="a" * 40,
        state_path=tmp_path / "state.json",
        signal_path=tmp_path / "signal.json",
    )
    _insert_obligation(ledger, state=state)
    next(iter(adapter.callbacks.values()))()

    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "signal.json").exists()


def test_hermes_capture_requires_exact_session_platform_and_chat(tmp_path) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    ledger = hermes_home / "state.db"
    _create_ledger(ledger)
    adapter = _Adapter()

    register_external_delivery_capture(
        event=_Event(source=_Source(platform=_Platform("telegram"))),
        gateway=_Gateway(adapter),
        hermes_home=hermes_home,
        runtime_commit="a" * 40,
        state_path=tmp_path / "state.json",
        signal_path=tmp_path / "signal.json",
    )
    _insert_obligation(ledger, platform="weixin", chat_id="other-chat")
    next(iter(adapter.callbacks.values()))()

    assert not (tmp_path / "state.json").exists()


def test_hermes_capture_requires_obligation_bound_to_inbound_message(tmp_path) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    ledger = hermes_home / "state.db"
    _create_ledger(ledger)
    adapter = _Adapter()

    register_external_delivery_capture(
        event=_Event(source=_Source(platform=_Platform("telegram"))),
        gateway=_Gateway(adapter),
        hermes_home=hermes_home,
        runtime_commit="a" * 40,
        state_path=tmp_path / "state.json",
        signal_path=tmp_path / "signal.json",
    )
    _insert_obligation(ledger, obligation_id="wrong-turn-obligation")
    next(iter(adapter.callbacks.values()))()

    assert not (tmp_path / "state.json").exists()


def test_hermes_capture_rejects_user_before_gateway_authorization(tmp_path) -> None:
    adapter = _Adapter()

    register_external_delivery_capture(
        event=_Event(source=_Source(platform=_Platform("telegram"))),
        gateway=_Gateway(adapter, authorized=False),
        hermes_home=tmp_path,
        runtime_commit="a" * 40,
        state_path=tmp_path / "state.json",
        signal_path=tmp_path / "signal.json",
    )

    assert adapter.callbacks == {}
