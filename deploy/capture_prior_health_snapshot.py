#!/usr/bin/env python3
"""Capture bounded pre-quiesce health evidence for protected bootstrap."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

PRIOR_HEALTH_SNAPSHOT_SCHEMA = "prior_health_snapshot.v1"
MAX_PRIOR_HEALTH_SNAPSHOT_BYTES = 64 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_health(url: str) -> dict[str, Any]:
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        return {"_fetch_error": "health_url_not_loopback_http"}
    try:
        request = Request(url, headers={"Accept": "application/json"})
        with build_opener(_NoRedirect).open(request, timeout=5.0) as response:
            raw = response.read(MAX_PRIOR_HEALTH_SNAPSHOT_BYTES + 1)
            if len(raw) > MAX_PRIOR_HEALTH_SNAPSHOT_BYTES:
                return {"_fetch_error": "health_response_too_large"}
            payload = json.loads(raw.decode("utf-8"))
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {"_fetch_error": "health_fetch_failed"}
    return payload if isinstance(payload, dict) else {"_fetch_error": "health_payload_not_object"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture bounded prior release health")
    parser.add_argument("--health-url", required=True)
    args = parser.parse_args(argv)
    payload = _fetch_health(args.health_url)
    if payload.get("_fetch_error"):
        print("prior_health_capture_failed", file=sys.stderr)
        return 2
    snapshot = {
        "schema": PRIOR_HEALTH_SNAPSHOT_SCHEMA,
        "health_url": str(args.health_url or "").strip(),
        "health": payload,
    }
    try:
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        print("prior_health_capture_failed", file=sys.stderr)
        return 2
    if len(encoded) > MAX_PRIOR_HEALTH_SNAPSHOT_BYTES:
        print("prior_health_capture_failed", file=sys.stderr)
        return 2
    sys.stdout.buffer.write(encoded + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
