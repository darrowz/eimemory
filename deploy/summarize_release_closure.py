#!/usr/bin/env python3
"""Emit a bounded, non-sensitive summary of a release-closure report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
from typing import Any


MAX_REPORT_BYTES = 16 * 1024 * 1024


def summarize_release_closure(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise ValueError("release closure report must be an object")
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    replay = report.get("replay_bootstrap") if isinstance(report.get("replay_bootstrap"), dict) else {}
    live = report.get("live_acceptance") if isinstance(report.get("live_acceptance"), dict) else {}
    rehearsal = report.get("closure_rehearsal") if isinstance(report.get("closure_rehearsal"), dict) else {}
    readiness = report.get("readiness") if isinstance(report.get("readiness"), dict) else {}
    rehearsal_complete = rehearsal.get("closure_complete") is True
    rehearsal_accumulating = rehearsal.get("data_accumulating") is True
    return {
        "ok": report.get("ok") is True,
        "closure_complete": report.get("closure_complete") is True,
        "data_accumulating": report.get("data_accumulating") is True,
        "blocked_stage": str(report.get("blocked_stage") or ""),
        "blocked_reason": str(report.get("blocked_reason") or ""),
        "commit": str(deployment.get("commit") or ""),
        "version": str(deployment.get("version") or ""),
        "receipt_id": str(deployment.get("promotion_request_id") or ""),
        "replay_ok": replay.get("ok") is True,
        "live_acceptance_ok": live.get("ok") is True,
        "live_pass_count": int(live.get("pass_count") or 0),
        "live_case_count": int(live.get("case_count") or 0),
        "rehearsal_ok": rehearsal.get("ok") is True and rehearsal_complete != rehearsal_accumulating,
        "readiness_stage": str(readiness.get("current_stage") or readiness.get("status") or ""),
        "readiness_score": readiness.get("readiness_score"),
    }


def _read_report(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("release closure report must be a regular non-symlink file")
        if metadata.st_size > MAX_REPORT_BYTES:
            raise ValueError("release closure report exceeds size limit")
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("release closure report exceeds size limit")
    return json.loads(raw.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        summary = summarize_release_closure(_read_report(args.path))
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"release closure summary failed: {exc}\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
