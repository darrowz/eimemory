#!/usr/bin/env python3
"""Emit a bounded, non-sensitive summary of a release-closure report."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any


# This standalone CLI also imports from the immutable release when invoked
# without -B. Pin suppression before loading any local package modules.
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eimemory.governance.release.closure_contracts import (
    acceptance_failure_details,
    channel_wait_report_ok,
    live_acceptance_report_ok,
    legacy_release_replay_ok,
)

from eimemory.governance.release.closure_verdict import (  # noqa: E402
    summarize_release_closure, _reported_release_summary,
    _release_closure_summary_contract_ok, _deployment_identity_matches,
    _release_authority_matches, _exact_int,
)

MAX_REPORT_BYTES = 16 * 1024 * 1024


def _read_report(path: Path) -> object:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        handle = os.fdopen(descriptor, "rb", closefd=True)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    with handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("release closure report must be a regular non-symlink file")
        if metadata.st_size > MAX_REPORT_BYTES:
            raise ValueError("release closure report exceeds size limit")
        raw = handle.read(MAX_REPORT_BYTES + 1)
    if len(raw) > MAX_REPORT_BYTES:
        raise ValueError("release closure report exceeds size limit")
    from eimemory.core.strict_json import loads as strict_json_loads

    return strict_json_loads(raw, max_bytes=MAX_REPORT_BYTES, max_depth=64)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = _read_report(args.path)
        summary = summarize_release_closure(report)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"release closure summary failed: {exc}\n")
    # Emit only after validation. JSON and the process status share one decision.
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return summary["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
