from __future__ import annotations

import socket
from urllib.error import URLError

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.web_learning import scout_web_learning


def test_web_learning_pins_the_validated_address_without_second_dns_lookup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "web-safe"}
    calls = {"count": 0}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    connected: list[tuple[str, int]] = []

    def offline_connect(address, *_args, **_kwargs):
        connected.append(address)
        raise URLError("offline after pinned connect")

    monkeypatch.setattr(socket, "create_connection", offline_connect)

    report = scout_web_learning(runtime, scope=scope, urls=["http://example.com/rebind"])

    assert report["hypothesis_count"] == 0
    assert report["errors"]
    assert calls["count"] == 1
    assert connected == [("93.184.216.34", 80)]
    assert "offline after pinned connect" in report["errors"][0]["detail"]


def test_web_learning_still_reports_network_errors_after_safe_resolution(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "web-safe"}

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))],
    )
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")))

    report = scout_web_learning(runtime, scope=scope, urls=["http://example.com/offline"])

    assert report["hypothesis_count"] == 0
    assert report["errors"]
    assert "offline" in report["errors"][0]["detail"]
