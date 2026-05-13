from __future__ import annotations

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.api.runtime import Runtime
from eimemory.ei_bridge.protocol import EIMemoryRPCRequest, EIMemoryRPCResponse


class _RPCHandler(BaseHTTPRequestHandler):
    bridge: EIBrainRPCBridge
    runtime: Runtime

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"", "/", "/health", "/daily-brief"}:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        query = parse_qs(parsed.query)
        scope = {
            "tenant_id": _first_query_value(query, "tenant_id", "default"),
            "agent_id": _first_query_value(query, "agent_id", "hongtu"),
            "workspace_id": _first_query_value(query, "workspace_id", "embodied"),
            "user_id": _first_query_value(query, "user_id", "darrow"),
        }
        brief = self.runtime.build_daily_brief(scope=scope)
        self._send_json(
            200,
            {
                "ok": True,
                "service": "eimemory-rpc",
                "news_digest": brief.get("news_digest", {}),
                "research_digest": brief.get("research_digest", {}),
                "source_health": brief.get("source_health", {}),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            request: EIMemoryRPCRequest = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request body must be a JSON object")
            response: EIMemoryRPCResponse = self.bridge.handle(request)
            status = 400 if response.get("ok") is False else 200
            self._send_json(status, response)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"ok": False, "error": "invalid_request"})
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json(500, {"ok": False, "error": "internal_error"})

    def _send_json(self, status_code: int, payload: EIMemoryRPCResponse) -> None:
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
        handler.runtime = runtime
        self._server = ThreadingHTTPServer((host, port), handler)
        self.address = self._server.server_address
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._server.server_close()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def request(self, payload: EIMemoryRPCRequest) -> EIMemoryRPCResponse:
        url = f"http://{self.address[0]}:{self.address[1]}/"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


def _first_query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key) or []
    if not values:
        return default
    return str(values[0] or default)
