"""Tests for the Karpathy Loop ``program.md`` (Task 2.1).

The program document is the *single source of truth* the autonomous loop reads
to decide what to do. The four hard requirements pinned by these tests:

1. The file exists at ``eimemory/autonomous/program.md`` under the repo root.
2. It has a ``## Goal`` section.
3. It has a ``## Metric`` section (the loop must know what to optimize).
4. It states a per-experiment time box of 5 minutes.

Path resolution is repo-root relative — the test works on Linux, macOS, and
Windows. (The original plan hardcoded ``/dev-project/eimemory``; we deliberately
do not.)
"""
from __future__ import annotations

import re
from pathlib import Path

# Repo root = parent of the ``tests/`` directory that contains this file.
REPO_ROOT = Path(__file__).resolve().parents[1]
PROGRAM_MD = REPO_ROOT / "eimemory" / "autonomous" / "program.md"


def test_program_md_exists() -> None:
    """The program document must exist on disk."""
    assert PROGRAM_MD.exists(), f"missing: {PROGRAM_MD}"


def test_program_md_has_goal_section() -> None:
    """A ``## Goal`` heading is the loop's primary input."""
    content = PROGRAM_MD.read_text(encoding="utf-8")
    assert re.search(r"^##\s+Goal\b", content, re.MULTILINE), "missing '## Goal' section"


def test_program_md_has_metric_section() -> None:
    """A ``## Metric`` heading tells the loop what to optimize."""
    content = PROGRAM_MD.read_text(encoding="utf-8")
    assert re.search(r"^##\s+Metric\b", content, re.MULTILINE), "missing '## Metric' section"


def test_program_md_has_time_box() -> None:
    """The per-experiment time box must be 5 minutes (hard limit)."""
    content = PROGRAM_MD.read_text(encoding="utf-8")
    assert "5 minute" in content or "5 min" in content, "missing 5 minute / 5 min time box"
