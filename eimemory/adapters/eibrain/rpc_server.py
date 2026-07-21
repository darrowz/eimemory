from __future__ import annotations

import errno
import hmac
import ipaddress
import json
import os
from pathlib import Path
from typing import Mapping
import socket
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.runtime.host_auth import attestation_tokens_from_private_file
from eimemory.api.runtime import Runtime
from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION
from eimemory.ei_bridge.protocol import EIMemoryRPCRequest, EIMemoryRPCResponse
from eimemory.version import __version__
from eimemory.runtime_identity import package_import_root, runtime_package_tree_digest


_CLIENT_DISCONNECT_ERRNOS = {errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED}
MAX_RPC_BODY_BYTES = 1_000_000
MIN_RPC_AUTH_TOKEN_LENGTH = 32
MIN_RPC_AUTH_TOKEN_DISTINCT_CHARS = 12


def _is_loopback_bind(host: str) -> bool:
    value = str(host or "").strip()
    if not value:
        return False
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(value, None, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError):
            return False
        return bool(addresses) and all(address.is_loopback for address in addresses)


def _is_strong_auth_token(token: str) -> bool:
    value = str(token or "").strip()
    return len(value) >= MIN_RPC_AUTH_TOKEN_LENGTH and len(set(value)) >= MIN_RPC_AUTH_TOKEN_DISTINCT_CHARS


def validate_rpc_auth_configuration(*, host: str, token: str) -> None:
    if not _is_loopback_bind(host) and not _is_strong_auth_token(token):
        raise ValueError("non-loopback RPC bind requires a strong authentication token")


def _is_client_disconnect(exc: OSError) -> bool:
    return isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)) or (
        getattr(exc, "errno", None) in _CLIENT_DISCONNECT_ERRNOS
    )


def _send_json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: EIMemoryRPCResponse) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        handler.send_response(status_code)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    except OSError as exc:
        if _is_client_disconnect(exc):
            handler.close_connection = True
            return
        raise


class _RPCHandler(BaseHTTPRequestHandler):
    bridge: EIBrainRPCBridge
    runtime: Runtime
    listen_host: str
    listen_port: int
    auth_token: str = ""
    attestation_tokens: dict[str, str] = {}
    loopback_health: dict[str, object] | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/health", "/healthz", "/livez", "/readyz"}:
            self._send_json(
                200,
                _compact_health_payload(
                    self.runtime,
                    ready=parsed.path != "/livez",
                    listen_host=self.listen_host,
                    listen_port=self.listen_port,
                    loopback_health=self.loopback_health,
                ),
            )
            return
        if parsed.path not in {"", "/", "/daily-brief", "/diagnostics"}:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        if self._auth_required() and not self._authorized():
            self._send_json(401, {"ok": False, "error": "unauthorized"})
            return
        if parsed.path in {"", "/"}:
            # The RPC root is a compact contract/identity probe.  Building a
            # daily brief here made a successful authentication probe sort and
            # deserialize thousands of historical records even though the
            # caller did not request diagnostic data.
            self._send_json(
                200,
                _compact_health_payload(
                    self.runtime,
                    ready=True,
                    listen_host=self.listen_host,
                    listen_port=self.listen_port,
                    loopback_health=self.loopback_health,
                ),
            )
            return
        query = parse_qs(parsed.query)
        scope = {
            "tenant_id": _first_query_value(query, "tenant_id", "default"),
            "agent_id": _first_query_value(query, "agent_id", "hongtu"),
            "workspace_id": _first_query_value(query, "workspace_id", "embodied"),
            "user_id": _first_query_value(query, "user_id", "darrow"),
        }
        brief = self.runtime.build_daily_brief(scope=scope)
        payload = {
            "ok": True,
            "service": "eimemory-rpc",
            "contract_version": EIMEMORY_RPC_CONTRACT_VERSION,
            "news_digest": brief.get("news_digest", {}),
            "research_digest": brief.get("research_digest", {}),
            "source_health": brief.get("source_health", {}),
        }
        if parsed.path == "/diagnostics":
            payload["diagnostics"] = {
                "brief_payload": True,
                "health_endpoint": "/health",
                "compact_health_endpoint": "/livez",
            }
        self._send_json(200, payload)

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0:
                raise ValueError("negative content length")
            if length > MAX_RPC_BODY_BYTES:
                self._send_json(413, {"ok": False, "error": "request_too_large"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            request: EIMemoryRPCRequest = json.loads(raw.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("request body must be a JSON object")
            method = request.get("method")
            if method == "adapter.attest_tool_result":
                producer = self._attestation_producer()
                if not producer:
                    self._send_json(401, {"ok": False, "error": "attestation_unauthorized"})
                    return
                response: EIMemoryRPCResponse = self.bridge.handle(request, attestation_producer=producer)
            else:
                if self._auth_required() and not self._authorized():
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                response = self.bridge.handle(request)
            status = 400 if response.get("ok") is False else 200
            self._send_json(status, response)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"ok": False, "error": "invalid_request"})
        except Exception as exc:  # pragma: no cover - defensive server boundary
            self._send_json(500, {"ok": False, "error": "internal_error"})

    def _send_json(self, status_code: int, payload: EIMemoryRPCResponse) -> None:
        _send_json_response(self, status_code, payload)

    def _auth_required(self) -> bool:
        return True

    def _authorized(self) -> bool:
        token = str(self.auth_token or "").strip()
        header = str(self.headers.get("Authorization", "") or "")
        prefix = "Bearer "
        if not token or not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix) :].strip(), token)

    def _attestation_producer(self) -> str:
        header = str(self.headers.get("Authorization", "") or "")
        if not header.startswith("Bearer "):
            return ""
        candidate = header[len("Bearer ") :].strip()
        for token, producer in self.attestation_tokens.items():
            if hmac.compare_digest(candidate, token):
                return producer
        return ""

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class _HealthOnlyHandler(BaseHTTPRequestHandler):
    runtime: Runtime
    listen_host: str
    listen_port: int
    loopback_health: dict[str, object] | None = None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path not in {"/health", "/healthz", "/livez", "/readyz"}:
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        self._send_json(
            200,
            _compact_health_payload(
                self.runtime,
                ready=parsed.path != "/livez",
                listen_host=self.listen_host,
                listen_port=self.listen_port,
                loopback_health=self.loopback_health,
            ),
        )

    def _send_json(self, status_code: int, payload: EIMemoryRPCResponse) -> None:
        _send_json_response(self, status_code, payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class EIBrainRPCServer:
    def __init__(
        self,
        runtime: Runtime,
        *,
        host: str,
        port: int,
        loopback_health_host: str = "",
        loopback_health_port: int | None = None,
        auth_token: str | None = None,
        attestation_tokens: Mapping[str, str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.host = host
        self.port = port
        self.auth_token = str(auth_token if auth_token is not None else os.environ.get("EIMEMORY_RPC_AUTH_TOKEN", "")).strip()
        configured_attestation_tokens = (
            dict(attestation_tokens)
            if attestation_tokens is not None
            else attestation_tokens_from_private_file()
        )
        self.attestation_tokens = {
            str(token).strip(): str(producer).strip().lower()
            for token, producer in configured_attestation_tokens.items()
            if _is_strong_auth_token(str(token)) and str(producer).strip().lower() in {"codex", "hermes"}
        }
        validate_rpc_auth_configuration(host=host, token=self.auth_token)
        if self.auth_token and self.auth_token in self.attestation_tokens:
            raise ValueError("runtime RPC and attestation producer credentials must be distinct")
        runtime._attestation_available_channels = frozenset(self.attestation_tokens.values())
        runtime._attestation_unavailable_reason = (
            ""
            if self.attestation_tokens
            else "operator_separated_attestation_profile_not_configured"
        )
        handler = type("EIMemoryRPCHandler", (_RPCHandler,), {})
        handler.bridge = EIBrainRPCBridge(runtime)
        handler.runtime = runtime
        handler.auth_token = self.auth_token
        handler.attestation_tokens = dict(self.attestation_tokens)
        self._server = ThreadingHTTPServer((host, port), handler)
        self.address = self._server.server_address
        handler.listen_host = str(self.address[0])
        handler.listen_port = int(self.address[1])
        self._thread: threading.Thread | None = None
        self._loopback_health_server: ThreadingHTTPServer | None = None
        self._loopback_health_thread: threading.Thread | None = None
        self.loopback_health_address: tuple[str, int] | None = None
        if loopback_health_host and loopback_health_port is not None:
            health_handler = type("EIMemoryLoopbackHealthHandler", (_HealthOnlyHandler,), {})
            health_handler.runtime = runtime
            health_handler.listen_host = str(self.address[0])
            health_handler.listen_port = int(self.address[1])
            self._loopback_health_server = ThreadingHTTPServer((loopback_health_host, loopback_health_port), health_handler)
            self.loopback_health_address = self._loopback_health_server.server_address
            loopback_health = {
                "host": str(self.loopback_health_address[0]),
                "port": int(self.loopback_health_address[1]),
                "path": "/health",
            }
            health_handler.loopback_health = loopback_health
            handler.loopback_health = loopback_health

    def start(self) -> None:
        if self._loopback_health_server is not None:
            self._loopback_health_thread = threading.Thread(
                target=self._loopback_health_server.serve_forever,
                daemon=True,
            )
            self._loopback_health_thread.start()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        if self._loopback_health_server is not None:
            self._loopback_health_thread = threading.Thread(
                target=self._loopback_health_server.serve_forever,
                daemon=True,
            )
            self._loopback_health_thread.start()
        try:
            self._server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self._server.server_close()
            if self._loopback_health_server is not None:
                self._loopback_health_server.shutdown()
                self._loopback_health_server.server_close()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._loopback_health_server is not None:
            self._loopback_health_server.shutdown()
            self._loopback_health_server.server_close()
        if self._loopback_health_thread is not None:
            self._loopback_health_thread.join(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def request(self, payload: EIMemoryRPCRequest) -> EIMemoryRPCResponse:
        url = f"http://{self.address[0]}:{self.address[1]}/"
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))


def _first_query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key) or []
    if not values:
        return default
    return str(values[0] or default)


def _attestation_tokens_from_env() -> dict[str, str]:
    """Backward-compatible helper name; credentials are file-only."""
    return attestation_tokens_from_private_file()


def _compact_health_payload(
    runtime: Runtime,
    *,
    ready: bool,
    listen_host: str,
    listen_port: int,
    loopback_health: dict[str, object] | None = None,
) -> EIMemoryRPCResponse:
    root = getattr(getattr(runtime, "store", None), "root", None)
    store_root = Path(root) if root else None
    store_ready = bool(store_root and store_root.exists())
    import_root = package_import_root()
    payload: EIMemoryRPCResponse = {
        "ok": store_ready,
        "service": "eimemory-rpc",
        "version": __version__,
        "commit": _current_commit(),
        "contract_version": EIMEMORY_RPC_CONTRACT_VERSION,
        "import_root": str(import_root),
        "package_tree_digest": runtime_package_tree_digest(),
        "paths": {
            "current": str(_current_path()),
            "release": str(_release_path()),
        },
        "listen_host": listen_host,
        "listen_port": int(listen_port),
        "store": {
            "ready": store_ready,
            "root": str(store_root) if store_root else "",
        },
        "checks": {
            "process": True,
            "store": store_ready,
            "ready": bool(ready and store_ready),
        },
    }
    if loopback_health:
        payload["loopback_health"] = loopback_health
    return payload


def build_health_payload(
    runtime: Runtime,
    *,
    listen_host: str,
    listen_port: int,
    ready: bool = True,
    loopback_health: dict[str, object] | None = None,
) -> EIMemoryRPCResponse:
    return _compact_health_payload(
        runtime,
        ready=ready,
        listen_host=listen_host,
        listen_port=listen_port,
        loopback_health=loopback_health,
    )


def _current_commit() -> str:
    for key in ("EIMEMORY_COMMIT", "GIT_COMMIT", "SOURCE_VERSION"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    release_name = _release_path().name
    if _looks_like_commit(release_name):
        return release_name
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _current_path() -> Path:
    current_link = Path("/opt/eimemory/current")
    release_path = _release_path()
    try:
        if current_link.exists() and current_link.resolve() == release_path.resolve():
            return current_link
    except OSError:
        pass
    return release_path


def _release_path() -> Path:
    cwd = Path.cwd().resolve()
    for path in (cwd, *Path(__file__).resolve().parents):
        parts = path.parts
        if "releases" not in parts:
            continue
        index = parts.index("releases")
        if index + 1 < len(parts) and _looks_like_commit(parts[index + 1]):
            return Path(*parts[: index + 2])
    return cwd


def _looks_like_commit(value: str) -> bool:
    text = str(value or "").strip().lower()
    return len(text) >= 7 and all(char in "0123456789abcdef" for char in text)
