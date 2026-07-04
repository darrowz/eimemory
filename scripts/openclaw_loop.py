#!/usr/bin/env python3
"""Compatibility wrapper for the packaged OpenClaw loop ledger."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eimemory.ops.openclaw_loop import *  # noqa: F401,F403
from eimemory.ops.openclaw_loop import main


if __name__ == "__main__":
    raise SystemExit(main())
