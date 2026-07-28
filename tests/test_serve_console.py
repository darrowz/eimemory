from __future__ import annotations

import threading
from http import HTTPStatus
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from eimemory.governance.serve_console import ConsoleHandler
from http.server import ThreadingHTTPServer


@pytest.fixture
def console_server(monkeypatch: pytest.MonkeyPatch, tmp_path):
    console = tmp_path / "console.html"
    console.write_text("<h1>eimemory</h1>", encoding="utf-8")
    monkeypatch.setenv("EIMEMORY_CONSOLE_PATH", str(console))
    server = ThreadingHTTPServer(("127.0.0.1", 0), ConsoleHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _error_status(url: str) -> int:
    with pytest.raises(HTTPError) as exc_info:
        urlopen(url, timeout=2)
    return exc_info.value.code


def test_console_refuses_requests_when_token_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    console_server: str,
) -> None:
    monkeypatch.delenv("EIMEMORY_CONSOLE_TOKEN", raising=False)

    assert _error_status(f"{console_server}/anything") == HTTPStatus.SERVICE_UNAVAILABLE


def test_console_hides_endpoint_for_wrong_token(
    monkeypatch: pytest.MonkeyPatch,
    console_server: str,
) -> None:
    monkeypatch.setenv("EIMEMORY_CONSOLE_TOKEN", "correct-token")

    assert _error_status(f"{console_server}/wrong-token") == HTTPStatus.NOT_FOUND


def test_console_serves_only_exact_token_path_with_security_headers(
    monkeypatch: pytest.MonkeyPatch,
    console_server: str,
) -> None:
    monkeypatch.setenv("EIMEMORY_CONSOLE_TOKEN", "correct-token")

    with urlopen(f"{console_server}/correct-token", timeout=2) as response:
        assert response.status == HTTPStatus.OK
        assert response.read() == b"<h1>eimemory</h1>"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
