from __future__ import annotations

import http.client
import json
from math import isfinite
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
import urllib.error

from eimemory.intake.safe_transport import UnsafeURL, safe_urlopen
from eimemory.adapters.runtime.circuit_breaker import CircuitBreaker
from eimemory.storage.bounded_jsonl import append_bounded_jsonl
from eimemory.core.strict_json import StrictJSONError, loads as strict_json_loads


DEFAULT_MAX_FAILURE_LEDGER_BYTES = 256 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024


class AgentRuntimeTransportError(RuntimeError):
    def __init__(self, reason: str, *, http_status: int | None = None) -> None:
        # Only fixed codes cross the public/logging boundary. Exception text,
        # URLs and HTTP response bodies may contain credentials or private data.
        safe_reasons = {"configuration_missing", "timeout", "connection_error",
                        "http_error", "response_too_large", "invalid_response",
                        "invalid_request", "configuration_invalid"}
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
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
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
        self.max_request_bytes = max(1_024, int(max_request_bytes))
        self._circuit = CircuitBreaker(
            failure_threshold=self.circuit_failure_threshold,
            reset_seconds=self.circuit_reset_seconds,
            clock=lambda: monotonic(),
        )
        self.last_failure_ledger_error: str | None = None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url:
            raise AgentRuntimeTransportError("configuration_missing")
        if not self.auth_token:
            raise AgentRuntimeTransportError("configuration_missing")
        try:
            body = json.dumps(
                {"method": str(method), "params": dict(params)},
                ensure_ascii=False, allow_nan=False,
            ).encode("utf-8")
            strict_json_loads(body, max_bytes=self.max_request_bytes)
        except (TypeError, ValueError, UnicodeError, RecursionError, OverflowError):
            raise AgentRuntimeTransportError("invalid_request") from None
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
            raise AgentRuntimeTransportError("http_error", http_status=exc.code) from None
        except http.client.HTTPException:
            raise AgentRuntimeTransportError("invalid_response") from None
        except (UnicodeDecodeError, json.JSONDecodeError, StrictJSONError) as exc:
            raise AgentRuntimeTransportError("invalid_response") from None
        except UnsafeURL as exc:
            raise AgentRuntimeTransportError("connection_error") from None
        except (OSError, urllib.error.URLError) as exc:
            cause = getattr(exc, "reason", exc)
            reason = "timeout" if isinstance(cause, TimeoutError) else "connection_error"
            raise AgentRuntimeTransportError(reason) from None
        except ValueError:
            raise AgentRuntimeTransportError("configuration_invalid") from None
        if not isinstance(payload, dict):
            raise AgentRuntimeTransportError("invalid_response")
        return {**payload, "bypassed": False}

    def call_or_bypass(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        ticket = self._circuit.acquire()
        if ticket is None:
            self._record_failure(method=method, error="circuit_open")
            return self._bypass("circuit_open")
        try:
            result = self.call(method, params)
        except AgentRuntimeTransportError as exc:
            if exc.diagnostic["reason"] in {
                "invalid_request", "configuration_invalid", "configuration_missing",
            }:
                self._circuit.abandon(ticket)
            else:
                self._circuit.failed(ticket)
            self._record_failure(method=method, error="adapter_unavailable", diagnostic=exc.diagnostic)
            return {**self._bypass("adapter_unavailable"), "diagnostic": exc.diagnostic}
        except BaseException:
            # A cancellation/programmer error is not a remote health verdict,
            # but must never leave the half-open probe permanently reserved.
            self._circuit.abandon(ticket)
            raise
        self._circuit.succeeded(ticket)
        return result

    def _circuit_is_open(self) -> bool:
        return self._circuit.is_open()

    def _record_failure(self, *, method: str, error: str, diagnostic: dict | None = None) -> None:
        if self.failure_ledger_path is None:
            return
        try:
            written = append_bounded_jsonl(
                self.failure_ledger_path,
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "transport": "eimemory_rpc",
                    "method": str(method or "")[:256],
                    "error": error,
                    **({"diagnostic": diagnostic} if diagnostic else {}),
                },
                max_bytes=self.max_failure_ledger_bytes,
                lock_timeout=0.05,
            )
            self.last_failure_ledger_error = None if written else "entry_too_large"
        except (OSError, ValueError, TypeError):
            # Diagnostic persistence must not take down the host adapter. This
            # is not the governance ledger; expose a fixed failure indicator.
            self.last_failure_ledger_error = "ledger_write_failed"

    @staticmethod
    def _bypass(error: str) -> dict[str, Any]:
        return {"ok": False, "bypassed": True, "error": error, "result": None}
