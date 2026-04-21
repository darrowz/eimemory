from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.api.runtime import Runtime


class _RPCHandler(BaseHTTPRequestHandler):
    bridge: EIBrainRPCBridge

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            request = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request body must be a JSON object")
            response = self.bridge.handle(request)
            self._send_json(200, response)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send_json(
                400,
                {"ok": False, "error": "invalid_request", "detail": str(exc)},
            )
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json(
                500,
                {"ok": False, "error": "internal_error", "detail": str(exc)},
            )

    def _send_json(self, status_code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class EIBrainRPCServer:
    def __init__(self, runtime: Runtime, *, host: str, port: int) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        handler = type("EIMemoryRPCHandler", (_RPCHandler,), {})
        handler.bridge = EIBrainRPCBridge(runtime)
        self._server = ThreadingHTTPServer((host, port), handler)
        self.address = self._server.server_address
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def request(self, payload: dict) -> dict:
        url = f"http://{self.address[0]}:{self.address[1]}/"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
