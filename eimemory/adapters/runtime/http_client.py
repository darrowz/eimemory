from __future__ import annotations

import json
from math import isfinite
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
import urllib.error

from eimemory.intake.safe_transport import UnsafeURL, safe_urlopen
from eimemory.core.strict_json import StrictJSONError, loads as strict_json_loads


DEFAULT_MAX_FAILURE_LEDGER_BYTES = 256 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024


class AgentRuntimeTransportError(RuntimeError):
    def __init__(self, reason: str, *, http_status: int | None = None) -> None:
        # Only fixed codes cross the public/logging boundary. Exception text,
        # URLs and HTTP response bodies may contain credentials or private data.
        safe_reasons = {"configuration_missing", "timeout", "connection_error",
                        "http_error", "response_too_large", "invalid_response"}
        self.diagnostic = {"reason": reason if reason in safe_reasons else "transport_error"}
        if type(http_status) is int and 100 <= http_status <= 599:
            self.diagnostic["http_status"] = http_status
        super().__init__(self.diagnostic["reason"])


class AgentRuntimeRPCClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth_token: str,
        timeout_seconds: float = 0.8,
        failure_ledger_path: str | Path | None = None,
        circuit_failure_threshold: int = 3,
        circuit_reset_seconds: float = 30.0,
        max_failure_ledger_bytes: int = DEFAULT_MAX_FAILURE_LEDGER_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        self.base_url = str(base_url or "").strip()
        self.auth_token = str(auth_token or "").strip()
        timeout = float(timeout_seconds)
        reset = float(circuit_reset_seconds)
        if not isfinite(timeout) or not isfinite(reset):
            raise ValueError("RPC timeouts must be finite")
        self.timeout_seconds = max(0.01, timeout)
        self.failure_ledger_path = Path(failure_ledger_path) if failure_ledger_path else None
        self.circuit_failure_threshold = max(1, int(circuit_failure_threshold))
        self.circuit_reset_seconds = max(0.1, reset)
        self.max_failure_ledger_bytes = max(1_024, int(max_failure_ledger_bytes))
        self.max_response_bytes = max(1_024, int(max_response_bytes))
        self._failure_count = 0
        self._circuit_opened_at: float | None = None
        self._lock = threading.Lock()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise AgentRuntimeTransportError("configuration_missing")
        if not self.auth_token:
            raise AgentRuntimeTransportError("configuration_missing")
        body = json.dumps({"method": str(method), "params": dict(params)}, ensure_ascii=False).encode("utf-8")
        try:
            with safe_urlopen(
                self.base_url,
                timeout=self.timeout_seconds,
                # The configured authenticated endpoint is not a redirect grant.
                max_redirects=0,
                method="POST",
                data=body,
                headers={
                    "Authorization": f"Bearer {self.auth_token}",
                    "Content-Type": "application/json",
                },
                # Adapter RPC targets the local eibrain loopback or Tailscale CGNAT listener.
                allow_loopback=True,
                allow_cgnat=True,
            ) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise AgentRuntimeTransportError("response_too_large")
                payload = strict_json_loads(raw, max_bytes=self.max_response_bytes)
        except urllib.error.HTTPError as exc:
            raise AgentRuntimeTransportError("http_error", http_status=exc.code) from exc
        except (UnicodeDecodeError, json.JSONDecodeError, StrictJSONError) as exc:
            raise AgentRuntimeTransportError("invalid_response") from exc
        except UnsafeURL as exc:
            raise AgentRuntimeTransportError("connection_error") from exc
        except (OSError, urllib.error.URLError) as exc:
            cause = getattr(exc, "reason", exc)
            reason = "timeout" if isinstance(cause, TimeoutError) else "connection_error"
            raise AgentRuntimeTransportError(reason) from exc
        if not isinstance(payload, dict):
            raise AgentRuntimeTransportError("invalid_response")
        return {**payload, "bypassed": False}

    def call_or_bypass(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._circuit_is_open():
            self._record_failure(method=method, error="circuit_open")
            return self._bypass("circuit_open")
        try:
            result = self.call(method, params)
        except AgentRuntimeTransportError as exc:
            with self._lock:
                self._failure_count += 1
                if self._failure_count >= self.circuit_failure_threshold:
                    self._circuit_opened_at = monotonic()
            self._record_failure(method=method, error="adapter_unavailable", diagnostic=exc.diagnostic)
            return {**self._bypass("adapter_unavailable"), "diagnostic": exc.diagnostic}
        with self._lock:
            self._failure_count = 0
            self._circuit_opened_at = None
        return result

    def _circuit_is_open(self) -> bool:
        with self._lock:
            if self._circuit_opened_at is None:
                return False
            if monotonic() - self._circuit_opened_at >= self.circuit_reset_seconds:
                self._failure_count = 0
                self._circuit_opened_at = None
                return False
            return True

    def _record_failure(self, *, method: str, error: str, diagnostic: dict | None = None) -> None:
        if self.failure_ledger_path is None:
            return
        entry = json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "transport": "eimemory_rpc",
                "method": str(method or "")[:256],
                "error": error,
                **({"diagnostic": diagnostic} if diagnostic else {}),
            },
            ensure_ascii=False,
            sort_keys=True,
        ) + "\n"
        encoded = entry.encode("utf-8")
        if len(encoded) > self.max_failure_ledger_bytes:
            return
        with self._lock:
            path = self.failure_ledger_path
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Bound input reads too: a pre-existing oversized ledger must
                # not be read in full before its output is truncated.
                max_keep = self.max_failure_ledger_bytes - len(encoded)
                try:
                    with path.open("rb") as stream:
                        size = stream.seek(0, 2)
                        start = max(0, size - max_keep)
                        stream.seek(start)
                        existing = stream.read(max_keep)
                    if start:
                        newline = existing.find(b"\n")
                        existing = existing[newline + 1 :] if newline >= 0 else b""
                except FileNotFoundError:
                    existing = b""
                path.write_bytes(existing + encoded)
            except OSError:
                return

    @staticmethod
    def _bypass(error: str) -> dict[str, Any]:
        return {"ok": False, "bypassed": True, "error": error, "result": None}
