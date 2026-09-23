"""HTTP framing/auth/resource primitives independent of Runtime and governance."""
from __future__ import annotations

import hmac
from collections.abc import Mapping
from http.server import ThreadingHTTPServer
from math import isfinite
import threading


class RequestBoundaryError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.status = status


def header_values(headers, name: str) -> list[str]:
    """Return all values for one header name.

    Production RPC uses ``email.message.Message`` / ``HTTPMessage`` (``get_all``).
    Synthetic callers and tests may pass a plain ``Mapping``; those cannot carry
    duplicate header names, so a single value is treated as a one-element list.
    Missing or unsupported header objects yield an empty list (fail closed for
    auth / framing checks that require exactly one value).
    """
    if headers is None:
        return []
    getter = getattr(headers, "get_all", None)
    if callable(getter):
        raw = getter(name, [])
        if not raw:
            return []
        return [str(item) for item in raw]
    if isinstance(headers, Mapping):
        target = str(name or "").lower()
        for key, value in headers.items():
            if str(key).lower() != target:
                continue
            if value is None:
                return []
            if isinstance(value, (list, tuple)):
                return [str(item) for item in value]
            return [str(value)]
        return []
    get = getattr(headers, "get", None)
    if callable(get):
        value = get(name)
        if value is None:
            return []
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value]
        return [str(value)]
    return []


def content_length(headers, *, max_bytes: int) -> int:
    """Accept one unambiguous fixed-length body; chunked RPC is not supported."""
    if header_values(headers, "Transfer-Encoding"):
        raise RequestBoundaryError("transfer_encoding_not_supported")
    values = header_values(headers, "Content-Length")
    if not values:
        raise RequestBoundaryError("content_length_required", 411)
    if len(values) != 1:
        raise RequestBoundaryError("ambiguous_content_length")
    value = values[0].strip()
    if not value or not value.isascii() or not value.isdecimal():
        raise RequestBoundaryError("invalid_content_length")
    # Strip leading zeros before the bounded integer conversion.
    value = value.lstrip("0") or "0"
    if len(value) > len(str(max_bytes)):
        raise RequestBoundaryError("request_too_large", 413)
    length = int(value)
    if length > max_bytes:
        raise RequestBoundaryError("request_too_large", 413)
    return length


def bearer_matches(headers, token: str) -> bool:
    """Malformed/duplicate/non-ASCII authentication is unauthorized, not a 500."""
    values = header_values(headers, "Authorization")
    if len(values) != 1 or not isinstance(token, str) or not token:
        return False
    header = values[0]
    if not isinstance(header, str) or not header.startswith("Bearer "):
        return False
    supplied = header[len("Bearer "):].strip()
    if not supplied or not supplied.isascii() or not token.isascii():
        return False
    return hmac.compare_digest(supplied, token)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Cap live request threads; apply an inactivity timeout to accepted sockets.

    The cap is not a business-job timeout. A running bridge call keeps its slot
    until it really exits; socket timeouts do not cancel Python side effects.
    Excess connections are closed without spawning another worker.
    """
    daemon_threads = True

    def __init__(self, *args, max_workers: int = 32, request_timeout: float = 5.0, **kwargs):
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        timeout = float(request_timeout)
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("request_timeout must be finite and positive")
        self._request_slots = threading.BoundedSemaphore(max_workers)
        self._request_timeout = timeout
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        try:
            request.settimeout(self._request_timeout)
            return request, address
        except BaseException:
            request.close()
            raise

    def process_request(self, request, client_address):
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()
