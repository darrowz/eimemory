from __future__ import annotations

import errno
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
from typing import Mapping
import socket
import subprocess
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from eimemory.core.strict_json import loads as strict_json_loads
from eimemory.adapters.runtime.http_boundary import (
    BoundedThreadingHTTPServer, RequestBoundaryError, bearer_matches, content_length,
)
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
        if handler.close_connection:
            handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(body)
    except OSError as exc:
        if _is_client_disconnect(exc):
            handler.close_connection = True
            return
        raise


class _RPCHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
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
            # SEC-2: unauthenticated health is intentionally slim — identity
            # fingerprints stay behind /diagnostics (auth required below).
            self._send_json(
                200,
                _public_health_payload(
                    self.runtime,
                    ready=parsed.path != "/livez",
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
        # One POST per connection: rejected/unread bodies cannot become the
        # next HTTP/1.1 request. Authenticate before blocking on the body.
        self.close_connection = True
        try:
            normal_authorized = self._authorized()
            producer = self._attestation_producer()
            if self._auth_required() and not normal_authorized and not producer:
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            length = content_length(self.headers, max_bytes=MAX_RPC_BODY_BYTES)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RequestBoundaryError("incomplete_request_body")
            request: EIMemoryRPCRequest = strict_json_loads(raw, max_bytes=MAX_RPC_BODY_BYTES)
            if not isinstance(request, dict) or not isinstance(request.get("method"), str):
                raise RequestBoundaryError("invalid_request")
            method = request["method"]
            if method in {"adapter.attest_tool_result", "adapter.record_verified_capability_outcome"}:
                if not producer:
                    self._send_json(401, {"ok": False, "error": "attestation_unauthorized"})
                    return
                response: EIMemoryRPCResponse = self.bridge.handle(request, attestation_producer=producer)
            else:
                # Producer credentials never acquire ordinary runtime authority.
                if self._auth_required() and not normal_authorized:
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                response = self.bridge.handle(request)
            status = 400 if response.get("ok") is False else 200
            self._send_json(status, response)
        except RequestBoundaryError as exc:
            self._send_json(exc.status, {"ok": False, "error": str(exc)})
        except TimeoutError:
            self._send_json(408, {"ok": False, "error": "request_timeout"})
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._send_json(400, {"ok": False, "error": "invalid_request"})
        except Exception:  # pragma: no cover - defensive server boundary
            self._send_json(500, {"ok": False, "error": "internal_error"})

    def _send_json(self, status_code: int, payload: EIMemoryRPCResponse) -> None:
        _send_json_response(self, status_code, payload)

    def _auth_required(self) -> bool:
        return True

    def _authorized(self) -> bool:
        return bearer_matches(self.headers, str(self.auth_token or "").strip())

    def _attestation_producer(self) -> str:
        for token, producer in self.attestation_tokens.items():
            if bearer_matches(self.headers, token):
                return producer
        return ""

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


class _HealthOnlyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
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
        self._server = BoundedThreadingHTTPServer((host, port), handler)
        self.address = self._server.server_address
        handler.listen_host = str(self.address[0])
        handler.listen_port = int(self.address[1])
        self._thread: threading.Thread | None = None
        self._loopback_health_server: ThreadingHTTPServer | None = None
        self._loopback_health_thread: threading.Thread | None = None
        self.loopback_health_address: tuple[str, int] | None = None
        if loopback_health_host and loopback_health_port is not None:
            health_handler = type("EIMemoryLoopbackRPCHandler", (_RPCHandler,), {})
            health_handler.bridge = handler.bridge
            health_handler.runtime = runtime
            health_handler.auth_token = self.auth_token
            health_handler.attestation_tokens = dict(self.attestation_tokens)
            health_handler.listen_host = str(self.address[0])
            health_handler.listen_port = int(self.address[1])
            self._loopback_health_server = BoundedThreadingHTTPServer((loopback_health_host, loopback_health_port), health_handler)
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


_HEALTH_FRAGMENT_CACHE: dict[str, tuple[float, object]] = {}
_HEALTH_FRAGMENT_TTL_SECONDS = 5.0


def _cached_health_fragment(key: str, builder):
    """Reuse expensive in-process health fragments across rapid probe cadences."""

    import time as _time

    cached = _HEALTH_FRAGMENT_CACHE.get(key)
    now = _time.monotonic()
    if cached is not None:
        cached_at, value = cached
        if (now - float(cached_at)) < _HEALTH_FRAGMENT_TTL_SECONDS:
            return value
    value = builder()
    _HEALTH_FRAGMENT_CACHE[key] = (now, value)
    return value


def _public_health_payload(runtime: Runtime, *, ready: bool) -> EIMemoryRPCResponse:
    """Unauthenticated liveness/readiness — no deploy fingerprints."""
    root = getattr(getattr(runtime, "store", None), "root", None)
    store_ready = bool(root and Path(root).exists())
    return {
        "ok": bool(store_ready),
        "service": "eimemory-rpc",
        "version": __version__,
        "contract_version": EIMEMORY_RPC_CONTRACT_VERSION,
        "checks": {
            "process": True,
            "store": store_ready,
            "ready": bool(ready and store_ready),
        },
    }


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
    sqlite_store = getattr(getattr(runtime, "store", None), "sqlite", None)
    pending_migrations_fn = getattr(sqlite_store, "pending_storage_migrations", None)
    store_cache_key = str(getattr(sqlite_store, "path", None) or id(sqlite_store))

    def _load_pending_migrations() -> list[str]:
        try:
            return list(pending_migrations_fn()) if callable(pending_migrations_fn) else []
        except Exception:
            return ["storage.migration_status_unavailable"]

    pending_migrations = list(
        _cached_health_fragment(f"pending_migrations:{store_cache_key}", _load_pending_migrations)
    )
    import_root = package_import_root()
    release_path = _release_path()
    current_commit = _current_commit()
    configured_commit = os.environ.get("EIMEMORY_RUNTIME_COMMIT", "").strip()
    runtime_identity_required = _production_runtime_identity_required(
        store_root=store_root,
        release_path=release_path,
    )
    runtime_identity_ok = (
        configured_commit == current_commit
        if configured_commit
        else not runtime_identity_required
    )
    payload: EIMemoryRPCResponse = {
        "ok": bool(store_ready and runtime_identity_ok),
        "service": "eimemory-rpc",
        "version": __version__,
        "commit": current_commit,
        "configured_runtime_commit": configured_commit,
        "contract_version": EIMEMORY_RPC_CONTRACT_VERSION,
        "import_root": str(import_root),
        "package_tree_digest": runtime_package_tree_digest(),
        "paths": {
            "current": str(_current_path()),
            "release": str(release_path),
        },
        "listen_host": listen_host,
        "listen_port": int(listen_port),
        "store": {
            "ready": store_ready,
            "root": str(store_root) if store_root else "",
            "migration_complete": not pending_migrations,
            "pending_migrations": pending_migrations[:16],
        },
        "checks": {
            "process": True,
            "store": store_ready,
            "runtime_identity": runtime_identity_ok,
            "ready": bool(ready and store_ready and runtime_identity_ok),
        },
    }
    if loopback_health:
        payload["loopback_health"] = loopback_health
    candidate_health = _cached_health_fragment(
        f"candidate_health:{store_cache_key}",
        lambda: _candidate_source_health(runtime),
    )
    if candidate_health is not None:
        payload["retrieval"] = {"candidate_source": candidate_health}
    return payload


def _production_runtime_identity_required(
    *,
    store_root: Path | None,
    release_path: Path,
) -> bool:
    if store_root is not None and store_root.as_posix().rstrip("/") == "/var/lib/eimemory":
        return True
    parts = tuple(str(part).lower() for part in release_path.parts)
    marker = ("opt", "eimemory", "releases")
    return any(parts[index : index + len(marker)] == marker for index in range(len(parts)))


def _candidate_source_health(runtime: Runtime) -> dict[str, object] | None:
    memory = getattr(runtime, "memory", None)
    engine = getattr(memory, "recall_engine", None)
    source = getattr(engine, "candidate_source", None)
    health_fn = getattr(source, "health", None)
    if not callable(health_fn):
        return None
    try:
        raw = health_fn()
    except Exception:
        return {
            "enabled": True,
            "configured": False,
            "available": False,
            "circuit": "open",
            "lag_seconds": None,
            "watermark": "",
            "last_error": "candidate_health_unavailable",
        }
    if not isinstance(raw, Mapping):
        return None
    circuit = str(raw.get("circuit") or "")
    if circuit not in {"closed", "open", "half_open"}:
        circuit = "open"
    last_error = str(raw.get("last_error") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{0,80}", last_error):
        last_error = "candidate_source_unavailable"
    watermark = str(raw.get("watermark") or "")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{0,256}", watermark):
        watermark = ""
    lag = raw.get("lag_seconds")
    try:
        lag_value = None if lag is None else max(0.0, min(10_000_000.0, float(lag)))
    except (TypeError, ValueError, OverflowError):
        lag_value = None
    return {
        "enabled": raw.get("enabled") is True,
        "configured": raw.get("configured") is True,
        "available": raw.get("available") is True,
        "index_verified": raw.get("index_verified") is True,
        "query_valid": raw.get("query_valid") is True,
        "last_query_status": (
            raw.get("last_query_status")
            if raw.get("last_query_status") in ("not_run", "available", "budget_exhausted", "unavailable")
            else "unknown"
        ),
        "circuit": circuit,
        "lag_seconds": lag_value,
        "watermark": watermark,
        "last_error": last_error,
    }


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
