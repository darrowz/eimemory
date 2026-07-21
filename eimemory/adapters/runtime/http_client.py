from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
import urllib.error
import urllib.request


DEFAULT_MAX_FAILURE_LEDGER_BYTES = 256 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024


class AgentRuntimeTransportError(RuntimeError):
    pass


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
        self.timeout_seconds = max(0.01, float(timeout_seconds))
        self.failure_ledger_path = Path(failure_ledger_path) if failure_ledger_path else None
        self.circuit_failure_threshold = max(1, int(circuit_failure_threshold))
        self.circuit_reset_seconds = max(0.1, float(circuit_reset_seconds))
        self.max_failure_ledger_bytes = max(1_024, int(max_failure_ledger_bytes))
        self.max_response_bytes = max(1_024, int(max_response_bytes))
        self._failure_count = 0
        self._circuit_opened_at: float | None = None
        self._lock = threading.Lock()

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise AgentRuntimeTransportError("adapter base URL is not configured")
        if not self.auth_token:
            raise AgentRuntimeTransportError("adapter authentication token is not configured")
        request = urllib.request.Request(
            self.base_url,
            data=json.dumps({"method": str(method), "params": dict(params)}, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise AgentRuntimeTransportError("adapter response exceeds byte limit")
                payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.URLError) as exc:
            raise AgentRuntimeTransportError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise AgentRuntimeTransportError("adapter response must be a JSON object")
        return {**payload, "bypassed": False}

    def call_or_bypass(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._circuit_is_open():
            self._record_failure(method=method, error="circuit_open")
            return self._bypass("circuit_open")
        try:
            result = self.call(method, params)
        except AgentRuntimeTransportError:
            with self._lock:
                self._failure_count += 1
                if self._failure_count >= self.circuit_failure_threshold:
                    self._circuit_opened_at = monotonic()
            self._record_failure(method=method, error="adapter_unavailable")
            return self._bypass("adapter_unavailable")
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

    def _record_failure(self, *, method: str, error: str) -> None:
        if self.failure_ledger_path is None:
            return
        entry = json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "transport": "eimemory_rpc",
                "method": str(method or "")[:256],
                "error": error,
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
                existing = path.read_bytes() if path.exists() else b""
                if len(existing) + len(encoded) > self.max_failure_ledger_bytes:
                    max_keep = self.max_failure_ledger_bytes - len(encoded)
                    keep = existing[-max_keep:] if max_keep > 0 else b""
                    newline = keep.find(b"\n")
                    existing = keep[newline + 1 :] if newline >= 0 else b""
                path.write_bytes(existing + encoded)
            except OSError:
                return

    @staticmethod
    def _bypass(error: str) -> dict[str, Any]:
        return {"ok": False, "bypassed": True, "error": error, "result": None}
