"""Legacy-path `python -m` entry points must not silently no-op.

Regression guard for the v1.14.x audit finding P1-A: governance shims used to
exit 0 with zero output under ``python -m <legacy dotted path>``, which turned
documented operator commands (independent evidence approve/inspect/...) into
silent no-ops. The shims now delegate to the relocated module via runpy, and
the two legacy names that the eimemory package init chain used to pre-import
(learning_state, memory_graph) must exit cleanly instead of crashing runpy
with a loader mismatch.
"""
from __future__ import annotations

import subprocess
import sys

import pytest


def _run(module: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )


def test_independent_evidence_legacy_path_keeps_cli_usage() -> None:
    completed = _run("eimemory.governance.independent_evidence", "--help")
    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout
    assert "init" in completed.stdout
    assert "approve" in completed.stdout


def test_independent_evidence_new_path_cli_is_equivalent() -> None:
    completed = _run("eimemory.governance.release.independent_evidence", "--help")
    assert completed.returncode == 0, completed.stderr
    assert "usage" in completed.stdout


@pytest.mark.parametrize(
    "module",
    [
        # These two legacy names are imported (by their new paths) through the
        # eimemory package init chain; before the shim __main__ branch they
        # crashed runpy with "loader ... cannot handle ..." and exit 1.
        "eimemory.governance.learning_state",
        "eimemory.governance.memory_graph",
        # A representative no-CLI relocated module: legacy -m stays a silent
        # exit 0, exactly like running the flat module before the split.
        "eimemory.governance.promotion_manager",
    ],
)
def test_legacy_python_m_exits_cleanly(module: str) -> None:
    completed = _run(module, "--help")
    assert completed.returncode == 0, completed.stderr
    assert "cannot handle" not in completed.stderr
    assert "Traceback" not in completed.stderr


def test_prompt_safety_remote_legacy_path_still_executable() -> None:
    # This shim kept its original SystemExit(main()) form; ensure it still
    # runs (it prints a JSON error for missing env, which is the fail-closed
    # CLI contract, not a crash).
    completed = _run("eimemory.governance.prompt_safety_remote")
    assert completed.returncode in (0, 1), completed.stderr
    assert "Traceback" not in completed.stderr
