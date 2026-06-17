"""Sandbox network proxy: allow-list + content sanitization. Stdlib only.

The Karpathy loop runs in a sandbox where outbound HTTP must be
constrained to a known host allow-list and every response body must
be scrubbed of active content (script / iframe) and capped at 10 MB
so the learning loop never ingests a malicious or runaway payload.

This module is the single chokepoint — every outbound call site in
the sandbox should route through :func:`is_allowlisted` first and
:func:`sanitize_response` after, in that order.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse


DEFAULT_ALLOWLIST: list[str] = [
    "api.github.com",
    "raw.githubusercontent.com",
    "arxiv.org",
    "export.arxiv.org",
    "huggingface.co",
    "localhost",
    "127.0.0.1",
]

# Hard cap on response body size. 10 MB keeps the recall context budget
# predictable and stops a hostile or runaway server from filling memory.
MAX_RESPONSE_BYTES: int = 10 * 1024 * 1024

# Tag appended to truncated bodies so downstream consumers can detect it.
_TRUNCATION_TAG = "[TRUNCATED]"

# Active-content patterns. Case-insensitive, dotall so a payload can
# split a tag across newlines. Keep these conservative — the goal is
# to remove anything that can run in a browser, not to be a full
# HTML sanitizer.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
_IFRAME_RE = re.compile(r"<iframe\b[^>]*>.*?</iframe>", re.DOTALL | re.IGNORECASE)


def is_allowlisted(url: str, allowlist: list[str] | None = None) -> bool:
    """Return True iff the URL's host is in the (default or given) allow-list.

    Comparison is case-insensitive and matches on hostname only — scheme
    and port are ignored because the proxy itself controls the transport.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    allow = allowlist if allowlist is not None else DEFAULT_ALLOWLIST
    return host in {a.lower() for a in allow}


def sanitize_response(body: str, *, content_type: str) -> str:
    """Cap the body size and strip active HTML content.

    - Bodies whose UTF-8 byte length exceeds :data:`MAX_RESPONSE_BYTES`
      are truncated to fit and a ``[TRUNCATED]`` tag is appended. The
      cap is in BYTES (not Python str characters) so a 10 MB cap is
      actually 10 MB on the wire, not 10 MB of UTF-16 code units.
    - HTML bodies (``text/html`` or anything containing ``html`` in the
      content type) have ``<script>`` and ``<iframe>`` blocks removed.

    Returns the (possibly shorter) string. The function never raises on
    bad content — the goal is to make the body safe to feed into a
    model prompt, not to validate the payload.
    """
    encoded = body.encode("utf-8", errors="replace")
    if len(encoded) > MAX_RESPONSE_BYTES:
        # Truncate at a char boundary that keeps the byte length <= cap.
        truncated = encoded[:MAX_RESPONSE_BYTES].decode("utf-8", errors="replace")
        body = truncated + "\n" + _TRUNCATION_TAG
    if "html" in content_type.lower():
        body = _SCRIPT_RE.sub("", body)
        body = _IFRAME_RE.sub("", body)
    return body
