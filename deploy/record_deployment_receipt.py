#!/usr/bin/env python3
"""Record a receipt with the current trusted deploy code and target venv."""

from __future__ import annotations

from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eimemory.cli.main import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["learn", "deployment-receipt", *sys.argv[1:]]))
