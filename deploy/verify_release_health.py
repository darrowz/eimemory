#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from math import isfinite
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# -I removes the script directory and ignores PYTHONPATH. Bind imports to the
# release containing this verified script, never to cwd or a different venv.
sys.dont_write_bytecode = True
_RELEASE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_RELEASE_ROOT))
from eimemory.core.rpc_probe_auth import rpc_probe_headers
from eimemory.core.strict_json import loads as strict_json_loads
from eimemory.runtime_identity import package_tree_digest


MAX_HEALTH_BYTES = 64 * 1024


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_health_payload(
    payload: Any,
    *,
    commit: str,
    version: str,
    release_dir: str | Path,
) -> dict[str, Any]:
    expected_commit = str(commit or "").strip().lower()
    expected_version = str(version or "").strip()
    release = Path(release_dir).expanduser().resolve(strict=True)
    import_root = (release / "eimemory").resolve(strict=True)
    if not isinstance(payload, dict):
        return {"ok": False, "error": "health_payload_not_object"}
    observed_digest = str(payload.get("package_tree_digest") or "").strip().lower()
    health_checks = payload.get("checks")
    paths = payload.get("paths") if isinstance(payload.get("paths"), dict) else {}
    checks = {
        "service_ok": payload.get("ok") is True,
        "service_identity": str(payload.get("service") or "") == "eimemory-rpc",
        "runtime_identity": isinstance(health_checks, dict)
        and health_checks.get("runtime_identity") is True,
        "commit": str(payload.get("commit") or "").strip().lower() == expected_commit,
        "configured_runtime_commit": str(
            payload.get("configured_runtime_commit") or ""
        ).strip().lower()
        == expected_commit,
        "version": str(payload.get("version") or "").strip() == expected_version,
        "import_root": _resolved_equals(payload.get("import_root"), import_root),
        "release_path": _resolved_equals(paths.get("release"), release),
        "digest_shape": bool(re.fullmatch(r"[0-9a-f]{64}", observed_digest)),
    }
    if all(checks.values()):
        checks["digest_content"] = observed_digest == package_tree_digest(import_root)
    else:
        checks["digest_content"] = False
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "ok": not failed,
        "error": "" if not failed else "health_identity_mismatch",
        "failed_checks": failed,
        "commit": expected_commit,
        "version": expected_version,
        "release_dir": str(release),
        "package_tree_digest": observed_digest if not failed else "",
    }


def fetch_health(url: str, *, timeout: float = 8.0) -> dict[str, Any]:
    try:
        if isinstance(timeout, bool) or not isfinite(float(timeout)) or float(timeout) <= 0:
            return {"_fetch_error": "health_timeout_invalid"}
        timeout = max(1.0, min(30.0, float(timeout)))
        parsed = urlsplit(str(url or "").strip())
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or parsed.username is not None or parsed.password is not None or parsed.fragment
                or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
            return {"_fetch_error": "health_url_not_loopback_http"}
    except (TypeError, ValueError, OverflowError):
        return {"_fetch_error": "health_request_invalid"}
    try:
        request = Request(url, headers=rpc_probe_headers())
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(MAX_HEALTH_BYTES + 1)
            if len(raw) > MAX_HEALTH_BYTES:
                return {"_fetch_error": "health_response_too_large"}
            payload = strict_json_loads(raw, max_bytes=MAX_HEALTH_BYTES, max_depth=32)
    except (HTTPError, URLError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {"_fetch_error": type(exc).__name__}
    return payload if isinstance(payload, dict) else {"_fetch_error": "health_payload_not_object"}


def _resolved_equals(value: Any, expected: Path) -> bool:
    try:
        return Path(str(value or "")).expanduser().resolve(strict=True) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify immutable release health identity")
    parser.add_argument("--url", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args(argv)
    payload = fetch_health(args.url, timeout=args.timeout)
    if payload.get("_fetch_error"):
        report = {"ok": False, "error": str(payload["_fetch_error"])}
    else:
        try:
            report = verify_health_payload(
                payload,
                commit=args.commit,
                version=args.version,
                release_dir=args.release_dir,
            )
        except (OSError, RuntimeError, ValueError):
            report = {"ok": False, "error": "release_path_unavailable"}
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
