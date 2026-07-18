from __future__ import annotations

from io import BytesIO
import socket
from urllib.error import HTTPError

import pytest

from eimemory.intake.safe_transport import UnsafeURL, safe_urlopen


class _FakeSocket:
    def __init__(self, *, peer_ip: str, response: bytes) -> None:
        self.peer_ip = peer_ip
        self.response = response
        self.sent = b""
        self.closed = False

    def sendall(self, payload: bytes) -> None:
        self.sent += payload

    def makefile(self, *_args, **_kwargs):
        return BytesIO(self.response)

    def getpeername(self):
        return (self.peer_ip, 443)

    def close(self) -> None:
        self.closed = True


class _FakeTLSContext:
    def __init__(self) -> None:
        self.server_names: list[str] = []

    def wrap_socket(self, sock, *, server_hostname: str):
        self.server_names.append(server_hostname)
        return sock


def _public_answer(host: str, port: int, *_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))]


def test_safe_transport_connects_to_validated_ip_without_second_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    dns_calls: list[tuple[str, int]] = []
    connected: list[tuple[str, int]] = []
    tls = _FakeTLSContext()
    sock = _FakeSocket(
        peer_ip="93.184.216.34",
        response=b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 2\r\n\r\nok",
    )

    def fake_getaddrinfo(host: str, port: int, *_args, **_kwargs):
        dns_calls.append((host, port))
        if len(dns_calls) > 1:
            return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port))]
        return _public_answer(host, port)

    def fake_create_connection(address, *_args, **_kwargs):
        connected.append(address)
        return sock

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_create_connection)
    monkeypatch.setattr("ssl.create_default_context", lambda: tls)

    with safe_urlopen("https://example.com/data?q=1", timeout=2) as response:
        assert response.read() == b"ok"
        assert response.peer_ip == "93.184.216.34"

    assert dns_calls == [("example.com", 443)]
    assert connected == [("93.184.216.34", 443)]
    assert tls.server_names == ["example.com"]
    assert b"GET /data?q=1 HTTP/1.1\r\n" in sock.sent
    assert b"Host: example.com\r\n" in sock.sent


def test_redirect_to_private_address_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSocket(
        peer_ip="93.184.216.34",
        response=(
            b"HTTP/1.1 302 Found\r\n"
            b"Location: http://127.0.0.1/admin\r\n"
            b"Content-Length: 0\r\n\r\n"
        ),
    )
    connected: list[tuple[str, int]] = []
    monkeypatch.setattr(socket, "getaddrinfo", _public_answer)
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda address, *_args, **_kwargs: connected.append(address) or sock,
    )

    with pytest.raises(UnsafeURL, match="private|unsafe"):
        safe_urlopen("http://public.example/redirect", timeout=2)

    assert connected == [("93.184.216.34", 80)]


def test_http_error_status_is_rejected_before_body_can_be_ingested(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSocket(
        peer_ip="93.184.216.34",
        response=(
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-Length: 18\r\n\r\n"
            b"<html>error</html>"
        ),
    )
    monkeypatch.setattr(socket, "getaddrinfo", _public_answer)
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: sock)

    with pytest.raises(HTTPError) as exc_info:
        safe_urlopen("http://public.example/error", timeout=2)

    assert exc_info.value.code == 503
    assert sock.closed is True


def test_mixed_public_and_private_dns_answers_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
        ],
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("connection must not start for mixed DNS answers"),
    )

    with pytest.raises(UnsafeURL, match="private|unsafe"):
        safe_urlopen("https://mixed.example/data", timeout=2)


def test_connected_peer_mismatch_is_closed_and_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    sock = _FakeSocket(
        peer_ip="127.0.0.1",
        response=b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
    )
    monkeypatch.setattr(socket, "getaddrinfo", _public_answer)
    monkeypatch.setattr(socket, "create_connection", lambda *_args, **_kwargs: sock)

    with pytest.raises(UnsafeURL, match="peer"):
        safe_urlopen("http://public.example/data", timeout=2)

    assert sock.closed is True


def test_request_target_control_characters_are_rejected_before_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("control-character URL must not connect"),
    )

    with pytest.raises(UnsafeURL, match="invalid"):
        safe_urlopen("http://example.com/data\r\nX-Evil: injected", timeout=2)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://user:password@example.com/data",
        "http://localhost/data",
        "http://[::1]/data",
        "http://[::ffff:127.0.0.1]/data",
        "http://[::ffff:7f00:1]/data",
        "http://[::127.0.0.1]/data",
        "http://[2002:7f00:1::]/data",
    ],
)
def test_unsafe_url_forms_are_rejected_before_connect(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("unsafe URL must be rejected before connect"),
    )

    with pytest.raises(UnsafeURL):
        safe_urlopen(url, timeout=2)
