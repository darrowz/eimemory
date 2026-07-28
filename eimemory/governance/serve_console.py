from __future__ import annotations

import hmac
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


def _console_path() -> Path:
    return Path(os.environ.get("EIMEMORY_CONSOLE_PATH", "/var/lib/eimemory/governance/evolution-console.html"))


def _console_token() -> str:
    return os.environ.get("EIMEMORY_CONSOLE_TOKEN", "").strip()


def _request_is_authorized(request_path: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(request_path, token)


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "eimemory-console/1.0"

    def do_GET(self) -> None:
        token = _console_token()
        request_path = unquote(urlparse(self.path).path).strip("/")
        if not token:
            self._send_text(HTTPStatus.SERVICE_UNAVAILABLE, "console token unavailable\n")
            return
        if not _request_is_authorized(request_path, token):
            self._send_text(HTTPStatus.NOT_FOUND, "not found\n")
            return

        path = _console_path()
        try:
            payload = path.read_bytes()
        except OSError:
            self._send_text(HTTPStatus.SERVICE_UNAVAILABLE, "console unavailable\n")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._send_security_headers()
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")


def main() -> int:
    host = os.environ.get("EIMEMORY_CONSOLE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(os.environ.get("EIMEMORY_CONSOLE_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), ConsoleHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
