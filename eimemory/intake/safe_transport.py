from __future__ import annotations

from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
from typing import Any, Mapping
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit


REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


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
) -> SafeHTTPResponse:
    """Open an HTTP URL while pinning every connection to its validated DNS answer."""

    try:
        redirect_limit = max(0, int(max_redirects))
        final_timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout and max_redirects must be numeric") from exc
    if final_timeout <= 0:
        raise ValueError("timeout must be positive")

    current_url = str(url or "").strip()
    request_headers = _normalize_headers(headers)
    for redirect_count in range(redirect_limit + 1):
        parsed, host, port = _parse_and_validate_url(current_url)
        addresses = _resolve_validated_addresses(host, port)
        sock, peer_ip = _connect_pinned(
            addresses,
            port=port,
            timeout=final_timeout,
        )
        try:
            if parsed.scheme == "https":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
                peer_ip = _verified_peer_ip(sock, expected=peer_ip)
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
            )
            raw_response = http.client.HTTPResponse(sock)
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
        response.close()
        if redirect_count >= redirect_limit:
            raise UnsafeURL("too many redirects")
        current_url = urljoin(current_url, location)

    raise UnsafeURL("too many redirects")  # pragma: no cover - loop always returns or raises


def _normalize_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in dict(headers or {}).items():
        name = str(raw_name or "").strip()
        value = str(raw_value or "").strip()
        if not name or any(char in name for char in "\r\n:"):
            raise ValueError("invalid HTTP header name")
        if "\r" in value or "\n" in value:
            raise ValueError("invalid HTTP header value")
        lowered = name.lower()
        if lowered in {"host", "connection", "content-length", "transfer-encoding"}:
            continue
        normalized[name] = value
    return normalized


def _parse_and_validate_url(url: str):
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
    if direct_address is not None and _is_disallowed_address(direct_address):
        raise UnsafeURL("unsafe fetch URL host: private address")
    return parsed, host, int(port)


def _resolve_validated_addresses(host: str, port: int) -> tuple[str, ...]:
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
    if any(_is_disallowed_address(address) for address in addresses):
        raise UnsafeURL("unsafe fetch URL host: private address in DNS resolution")
    return tuple(str(address) for address in addresses)


def _connect_pinned(addresses: tuple[str, ...], *, port: int, timeout: float) -> tuple[Any, str]:
    last_error: OSError | None = None
    for address in addresses:
        try:
            sock = socket.create_connection((address, port), timeout=timeout)
            try:
                return sock, _verified_peer_ip(sock, expected=address)
            except Exception:
                sock.close()
                raise
        except OSError as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise UnsafeURL("fetch URL has no validated address")


def _verified_peer_ip(sock: Any, *, expected: str) -> str:
    peer = sock.getpeername()
    actual = _coerce_ip_address(str(peer[0] if isinstance(peer, tuple) else peer))
    expected_ip = _coerce_ip_address(expected)
    if actual is None or expected_ip is None or actual != expected_ip or _is_disallowed_address(actual):
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
) -> None:
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    host_value = f"[{host}]" if ":" in host else host
    if not default_port:
        host_value = f"{host_value}:{port}"
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host_value}",
        "Connection: close",
        "Accept-Encoding: identity",
    ]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


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


def _is_disallowed_address(address: Any) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour]
        if address.teredo is not None:
            embedded.extend(address.teredo)
        if any(candidate is not None and _is_disallowed_address(candidate) for candidate in embedded):
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
