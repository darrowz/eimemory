from __future__ import annotations

import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse


def _console_path() -> Path:
    return Path(os.environ.get("EIMEMORY_CONSOLE_PATH", "/var/lib/eimemory/governance/evolution-console.html"))


def _console_token() -> str:
    return os.environ.get("EIMEMORY_CONSOLE_TOKEN", "").strip()


class ConsoleHandler(BaseHTTPRequestHandler):
    server_version = "eimemory-console/1.0"

    def do_GET(self) -> None:
        token = _console_token()
        request_path = unquote(urlparse(self.path).path).strip("/")
        if token and request_path != token:
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
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_text(self, status: HTTPStatus, text: str) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


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
