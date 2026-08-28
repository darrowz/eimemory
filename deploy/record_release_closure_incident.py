#!/usr/bin/env python3
"""Persist a bounded system incident for an actionable closure failure."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import sys


RELEASE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RELEASE_ROOT))

from eimemory.api.runtime import Runtime  # noqa: E402
from eimemory.core.clock import now_iso  # noqa: E402
from eimemory.ops.release_closure_failure import record_release_closure_failure  # noqa: E402


MAX_REPORT_BYTES = 16 * 1024 * 1024


def _read_report(path: Path) -> dict:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb", closefd=True) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_REPORT_BYTES:
            raise ValueError("release closure report is not a bounded regular file")
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("release closure report exceeds size limit")
    value = json.loads(raw.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("release closure report must be an object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    parser.add_argument("--scope-agent", required=True)
    parser.add_argument("--scope-workspace", required=True)
    parser.add_argument("--scope-user", required=True)
    args = parser.parse_args(argv)
    try:
        report = _read_report(args.path)
        runtime = Runtime.create()
        try:
            result = record_release_closure_failure(
                runtime,
                scope={
                    "agent_id": args.scope_agent,
                    "workspace_id": args.scope_workspace,
                    "user_id": args.scope_user,
                },
                closure_report=report,
                detected_at=now_iso(),
            )
        finally:
            runtime.close()
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"release closure incident recording failed: {type(exc).__name__}\n")
    print(
        json.dumps(
            {
                "ok": result.get("ok") is True,
                "status": str(result.get("status") or ""),
                "incident_record_id": str(result.get("incident_record_id") or ""),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
