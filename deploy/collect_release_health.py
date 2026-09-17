#!/usr/bin/env python3
"""Canonical post-deploy health collect with stable exit codes.

This replaces brittle ``curl`` pipelines for worker/outer orchestration.
Exit codes are intentionally limited to:
  0 — health identity verified (or probe-only health ok)
  1 — health/identity failed (fail-closed)
  2 — invalid arguments / unusable local release path

Never returns curl-style codes such as 23 (write/pipe errors). Technical
deploy success must not be mislabeled solely because an ancillary collect
hit a write/pipe failure while live identity is actually healthy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from typing import Any


def _load_verify_release_health():
    path = Path(__file__).with_name("verify_release_health.py")
    spec = importlib.util.spec_from_file_location("eimemory_verify_release_health", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("verify_release_health_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_verify = _load_verify_release_health()


def collect_release_health(
    *,
    url: str,
    commit: str = "",
    version: str = "",
    release_dir: str = "",
    timeout: float = 8.0,
    probe_only: bool = False,
) -> dict[str, Any]:
    payload = _verify.fetch_health(str(url or "").strip(), timeout=max(1.0, min(30.0, float(timeout))))
    if payload.get("_fetch_error"):
        return {
            "ok": False,
            "error": str(payload["_fetch_error"]),
            "probe_only": bool(probe_only),
            "exit_class": "health_failed",
        }
    if probe_only or not (commit and version and release_dir):
        service_ok = payload.get("ok") is True
        return {
            "ok": service_ok,
            "error": "" if service_ok else "health_service_not_ok",
            "probe_only": True,
            "exit_class": "health_ok" if service_ok else "health_failed",
            "commit": str(payload.get("commit") or ""),
            "version": str(payload.get("version") or ""),
        }
    try:
        report = _verify.verify_health_payload(
            payload,
            commit=commit,
            version=version,
            release_dir=release_dir,
        )
    except (OSError, RuntimeError, ValueError):
        return {
            "ok": False,
            "error": "release_path_unavailable",
            "probe_only": False,
            "exit_class": "health_failed",
        }
    report = dict(report)
    report["probe_only"] = False
    report["exit_class"] = "health_ok" if report.get("ok") is True else "health_failed"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect immutable release health with stable exits")
    parser.add_argument("--url", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--version", default="")
    parser.add_argument("--release-dir", default="")
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="Only require health.ok; skip commit/version/release identity checks",
    )
    args = parser.parse_args(argv)
    if not args.probe_only and not (args.commit and args.version and args.release_dir):
        report = {
            "ok": False,
            "error": "identity_args_required_or_pass_probe_only",
            "exit_class": "usage_error",
        }
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 2
    report = collect_release_health(
        url=args.url,
        commit=args.commit,
        version=args.version,
        release_dir=args.release_dir,
        timeout=args.timeout,
        probe_only=bool(args.probe_only),
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    if report.get("exit_class") == "usage_error":
        return 2
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
