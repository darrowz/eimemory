from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
from math import isfinite
import re
import socket
import ssl
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
# Shared-address-space / CGNAT (RFC 6598). Tailscale assigns 100.64.0.0/10.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


class UnsafeURL(ValueError):
    """The requested URL cannot be fetched through the external-intake boundary."""


@dataclass(slots=True)
class SafeHTTPResponse:
    _response: http.client.HTTPResponse
    _socket: Any
    final_url: str
    peer_ip: str

    @property
    def headers(self):
        return self._response.headers

    @property
    def status(self) -> int:
        return int(self._response.status)

    @property
    def reason(self) -> str:
        return str(self._response.reason or "")

    def geturl(self) -> str:
        return self.final_url

    def read(self, amount: int | None = None) -> bytes:
        if amount is None:
            return self._response.read()
        return self._response.read(amount)

    def close(self) -> None:
        try:
            self._response.close()
        finally:
            self._socket.close()

    def __enter__(self) -> "SafeHTTPResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def safe_urlopen(
    url: str,
    *,
    timeout: float,
    max_redirects: int = 5,
    headers: Mapping[str, str] | None = None,
    method: str = "GET",
    data: bytes | None = None,
    allow_loopback: bool = False,
    allow_cgnat: bool = False,
) -> SafeHTTPResponse:
    """Open an HTTP URL while pinning every connection to its validated DNS answer.

    When ``allow_loopback`` is True, loopback addresses (127.0.0.0/8 and ::1)
    are permitted after the usual DNS-pin / peer checks. When ``allow_cgnat``
    is True, 100.64.0.0/10 (Tailscale / RFC 6598) is also permitted. RFC1918
    private, link-local, metadata, and other non-global addresses remain
    rejected unless separately opted in.
    """

    try:
        redirect_limit = max(0, int(max_redirects))
        final_timeout = float(timeout)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout and max_redirects must be numeric") from exc
    if not isfinite(final_timeout) or final_timeout <= 0:
        raise ValueError("timeout must be finite and positive")

    request_method = str(method or "GET").strip().upper() or "GET"
    if request_method not in {"GET", "POST", "HEAD"}:
        raise ValueError("unsupported HTTP method")
    body = b"" if data is None else bytes(data)
    if request_method == "GET" and body:
        raise ValueError("GET requests cannot carry a body")

    current_url = str(url or "").strip()
    request_headers = _normalize_headers(headers)
    allow_loopback = bool(allow_loopback)
    allow_cgnat = bool(allow_cgnat)
    for redirect_count in range(redirect_limit + 1):
        parsed, host, port = _parse_and_validate_url(
            current_url, allow_loopback=allow_loopback, allow_cgnat=allow_cgnat
        )
        addresses = _resolve_validated_addresses(
            host, port, allow_loopback=allow_loopback, allow_cgnat=allow_cgnat
        )
        sock, peer_ip = _connect_pinned(
            addresses,
            port=port,
            timeout=final_timeout,
            allow_loopback=allow_loopback,
            allow_cgnat=allow_cgnat,
        )
        try:
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
                peer_ip = _verified_peer_ip(
                    sock,
                    expected=peer_ip,
                    allow_loopback=allow_loopback,
                    allow_cgnat=allow_cgnat,
                )
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            _send_request(
                sock,
                path=path,
                host=host,
                port=port,
                scheme=parsed.scheme,
                headers=request_headers,
                method=request_method,
                body=body,
            )
            raw_response = http.client.HTTPResponse(sock, method=request_method)
            raw_response.begin()
        except Exception:
            sock.close()
            raise

        response = SafeHTTPResponse(
            _response=raw_response,
            _socket=sock,
            final_url=current_url,
            peer_ip=peer_ip,
        )
        if response.status >= 400:
            status = response.status
            reason = response.reason
            response_headers = response.headers
            response.close()
            raise HTTPError(current_url, status, reason, response_headers, None)
        location = str(response.headers.get("Location") or "").strip()
        if response.status not in REDIRECT_STATUSES or not location:
            return response
        redirect_status = response.status
        response.close()
        if redirect_count >= redirect_limit:
            raise UnsafeURL("too many redirects")
        next_url = urljoin(current_url, location)
        next_parsed, next_host, next_port = _parse_and_validate_url(
            next_url, allow_loopback=allow_loopback, allow_cgnat=allow_cgnat
        )
        if parsed.scheme == "https" and next_parsed.scheme != "https":
            raise UnsafeURL("HTTPS redirect downgrade is not allowed")
        origin_changed = (parsed.scheme, host, port) != (
            next_parsed.scheme, next_host, next_port
        )
        # RFC: 303 switches to GET; 301/302 historically do for POST; 307/308 keep method+body.
        if request_method == "POST" and redirect_status not in {307, 308}:
            request_method = "GET"
            body = b""
        if origin_changed:
            # Never replay a private RPC/upload body to a different origin.
            if body:
                raise UnsafeURL("cross-origin request body redirect is not allowed")
            # Use an allowlist, not a credential-name denylist: integrations may
            # use arbitrary X-* authentication headers. Same-origin auth stays.
            request_headers = {
                name: value for name, value in request_headers.items()
                if name.lower() in {"accept", "accept-language", "user-agent"}
            }
            # A local-network exception authorizes this origin, not redirect
            # destinations. The next iteration rechecks the tightened policy.
            allow_loopback = False
            allow_cgnat = False
        current_url = next_url

    raise UnsafeURL("too many redirects")  # pragma: no cover - loop always returns or raises


def _normalize_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in dict(headers or {}).items():
        name = str(raw_name or "").strip()
        value = str(raw_value or "").strip()
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name) is None:
            raise ValueError("invalid HTTP header name")
        if any((ord(char) < 32 and char != "\t") or ord(char) == 127 for char in value):
            raise ValueError("invalid HTTP header value")
        try:
            value.encode("latin-1")
        except UnicodeEncodeError:
            raise ValueError("invalid HTTP header value") from None
        lowered = name.lower()
        if lowered in {"host", "connection", "content-length", "transfer-encoding"}:
            continue
        if any(existing.lower() == lowered for existing in normalized):
            raise ValueError("duplicate HTTP header name")
        normalized[name] = value
    return normalized


def _parse_and_validate_url(url: str, *, allow_loopback: bool = False, allow_cgnat: bool = False):
    raw_url = str(url or "")
    if any(ord(char) < 32 or ord(char) == 127 for char in raw_url):
        raise UnsafeURL("invalid control character in fetch URL")
    try:
        parsed = urlsplit(raw_url.strip())
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UnsafeURL("invalid fetch URL") from exc
    if scheme not in {"http", "https"}:
        raise UnsafeURL("unsupported fetch URL scheme")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeURL("credentials in fetch URL are not allowed")
    raw_host = str(parsed.hostname or "").strip().rstrip(".")
    if not raw_host:
        raise UnsafeURL("missing fetch URL host")
    try:
        host = raw_host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise UnsafeURL("invalid fetch URL host") from exc
    direct_address = _coerce_ip_address(host)
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeURL("unsafe fetch URL host: private address")
    if direct_address is not None and _is_disallowed_address(
        direct_address, allow_loopback=allow_loopback, allow_cgnat=allow_cgnat
    ):
        raise UnsafeURL("unsafe fetch URL host: private address")
    return parsed, host, int(port)


def _resolve_validated_addresses(
    host: str, port: int, *, allow_loopback: bool = False, allow_cgnat: bool = False
) -> tuple[str, ...]:
    direct_address = _coerce_ip_address(host)
    if direct_address is not None:
        addresses = [direct_address]
    else:
        try:
            infos = socket.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise UnsafeURL("fetch URL host could not be resolved") from exc
        addresses = []
        for info in infos:
            sockaddr = info[4]
            if not sockaddr:
                continue
            address = _coerce_ip_address(str(sockaddr[0]))
            if address is None:
                raise UnsafeURL("fetch URL returned an invalid address")
            if address not in addresses:
                addresses.append(address)
    if not addresses:
        raise UnsafeURL("fetch URL host could not be resolved")
    allowed = tuple(
        str(address)
        for address in addresses
        if not _is_disallowed_address(address, allow_loopback=allow_loopback, allow_cgnat=allow_cgnat)
    )
    if not allowed:
        raise UnsafeURL("unsafe fetch URL host: private address in DNS resolution")
    # INT-01: a single poisoned A/AAAA record must not reject an otherwise safe set.
    return allowed


def _connect_pinned(
    addresses: tuple[str, ...],
    *,
    port: int,
    timeout: float,
    allow_loopback: bool = False,
    allow_cgnat: bool = False,
) -> tuple[Any, str]:
    last_error: OSError | None = None
    for address in addresses:
        try:
            sock = socket.create_connection((address, port), timeout=timeout)
            try:
                return sock, _verified_peer_ip(
                    sock,
                    expected=address,
                    allow_loopback=allow_loopback,
                    allow_cgnat=allow_cgnat,
                )
            except Exception:
                sock.close()
                raise
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise UnsafeURL("fetch URL has no validated address")


def _verified_peer_ip(
    sock: Any, *, expected: str, allow_loopback: bool = False, allow_cgnat: bool = False
) -> str:
    peer = sock.getpeername()
    actual = _coerce_ip_address(str(peer[0] if isinstance(peer, tuple) else peer))
    expected_ip = _coerce_ip_address(expected)
    if (
        actual is None
        or expected_ip is None
        or actual != expected_ip
        or _is_disallowed_address(actual, allow_loopback=allow_loopback, allow_cgnat=allow_cgnat)
    ):
        raise UnsafeURL("connected peer does not match the validated public address")
    return str(actual)


def _send_request(
    sock: Any,
    *,
    path: str,
    host: str,
    port: int,
    scheme: str,
    headers: Mapping[str, str],
    method: str = "GET",
    body: bytes = b"",
) -> None:
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    host_value = f"[{host}]" if ":" in host else host
    if not default_port:
        host_value = f"{host_value}:{port}"
    request_method = str(method or "GET").strip().upper() or "GET"
    payload = b"" if body is None else bytes(body)
    lines = [
        f"{request_method} {path} HTTP/1.1",
        f"Host: {host_value}",
        "Connection: close",
        "Accept-Encoding: identity",
    ]
    header_names = {str(name).lower() for name in headers}
    if payload and "content-length" not in header_names:
        lines.append(f"Content-Length: {len(payload)}")
    elif not payload and request_method == "POST" and "content-length" not in header_names:
        lines.append("Content-Length: 0")
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload)


def _coerce_ip_address(value: str):
    text = str(value or "").strip().strip("[]")
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    if text.isdigit():
        try:
            number = int(text, 10)
        except ValueError:  # pragma: no cover - guarded by isdigit
            return None
        if 0 <= number <= 0xFFFFFFFF:
            return ipaddress.ip_address(number)
    try:
        return ipaddress.ip_address(socket.inet_ntoa(socket.inet_aton(text)))
    except (OSError, ValueError):
        return None


def _is_cgnat_address(address: Any) -> bool:
    if isinstance(address, ipaddress.IPv4Address):
        return address in _CGNAT_NETWORK
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped in _CGNAT_NETWORK
    return False


def _is_disallowed_address(address: Any, *, allow_loopback: bool = False, allow_cgnat: bool = False) -> bool:
    if allow_loopback and bool(getattr(address, "is_loopback", False)):
        # Explicit deploy-health opt-in: allow 127.0.0.0/8 and ::1 only.
        # Private non-loopback, link-local, metadata, etc. stay rejected.
        return False
    if allow_cgnat and _is_cgnat_address(address):
        # Explicit adapter-RPC opt-in: Tailscale / RFC 6598 100.64.0.0/10 only.
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour]
        if address.teredo is not None:
            embedded.extend(address.teredo)
        if any(
            candidate is not None
            and _is_disallowed_address(
                candidate, allow_loopback=allow_loopback, allow_cgnat=allow_cgnat
            )
            for candidate in embedded
        ):
            return True
        # IPv4-compatible IPv6 addresses have platform-dependent routing
        # semantics.  They are obsolete and never required at this public
        # intake boundary, so reject the whole ::/96 range except ::/::1,
        # which are already rejected by the standard flags below.
        if int(address) < (1 << 32):
            return True
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )
