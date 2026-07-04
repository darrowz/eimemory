#!/usr/bin/env python3
"""Compatibility wrapper for the packaged OpenClaw loop ledger."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_roots() -> list[Path]:
    script = Path(__file__).resolve()
    values = [
        os.environ.get("OPENCLAW_LOOP_REPO"),
        os.environ.get("EIMEMORY_REPO"),
        str(script.parents[1]),
        str(script.parents[1] / "eimemory"),
        "/dev-project/eimemory",
        "/home/darrow/.openclaw/workspace/eimemory",
        "/opt/eimemory/current",
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        root = Path(value).expanduser()
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


for root in _candidate_roots():
    if (root / "eimemory" / "ops" / "openclaw_loop.py").exists():
        sys.path.insert(0, str(root))
        break

from eimemory.ops.openclaw_loop import *  # noqa: F401,F403
from eimemory.ops.openclaw_loop import main


if __name__ == "__main__":
    raise SystemExit(main())
