"""Tests for eimemory patch CLI (1.6.0 harness-patch)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


# Subprocess tests need to import eimemory from the worktree, not the
# installed package. The current process already imported eimemory from
# the worktree (because pytest is run from the worktree root), so we
# pass PYTHONPATH to the subprocess pointing at the worktree root.
WORKTREE_ROOT = Path(__file__).resolve().parent.parent
if not (WORKTREE_ROOT / "eimemory" / "cli" / "main.py").exists():
    # Fall back if the test file was moved; resolve from CWD.
    WORKTREE_ROOT = Path.cwd()


def _run_cli(*args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(WORKTREE_ROOT) + (os.pathsep + existing_pp if existing_pp else "")
    return subprocess.run(
        [sys.executable, "-m", "eimemory.cli.main", *args],
        capture_output=True, text=True, env=env, cwd=str(WORKTREE_ROOT),
    )


def test_cli_patch_propose_outputs_proposal_card(tmp_path) -> None:
    result = _run_cli(
        "patch", "propose",
        "--surface", "INSTRUCTION",
        "--evidence", "r1", "r2",
        "--agent", "eibrain",
        "--tier", "L1",
        "--rollback", "revert patch",
        "--diff-lines", "20",
        "--diff-tokens", "500",
        "--notes", "smoke test",
    )
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    card = json.loads(result.stdout)
    assert card["target_surface"] == "INSTRUCTION"
    assert card["diff_lines"] == 20
    assert card["diff_tokens"] == 500
    assert card["target_agent"] == "eibrain"
    assert card["risk_tier"] == "L1"
    assert card["rollback_plan"] == "revert patch"
    assert card["evidence_record_ids"] == ["r1", "r2"]
    assert card["notes"] == "smoke test"


def test_cli_patch_propose_rejects_invalid_surface(tmp_path) -> None:
    result = _run_cli(
        "patch", "propose",
        "--surface", "INVALID",
        "--evidence", "r1",
        "--agent", "eibrain",
        "--tier", "L1",
        "--rollback", "revert",
    )
    # argparse should reject with non-zero exit
    assert result.returncode != 0


def test_cli_patch_validate_runs_gate(tmp_path) -> None:
    # First produce a card via propose, then validate it
    card_path = tmp_path / "card.json"
    propose = _run_cli(
        "patch", "propose",
        "--surface", "INSTRUCTION",
        "--evidence", "r1",
        "--agent", "eibrain",
        "--tier", "L1",
        "--rollback", "revert",
        "--diff-lines", "10",
        "--diff-tokens", "200",
    )
    assert propose.returncode == 0, propose.stderr
    card_path.write_text(propose.stdout, encoding="utf-8")

    result = _run_cli("patch", "validate", "--card", str(card_path))
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    body = json.loads(result.stdout)
    assert body["verdict"] in {"ACCEPT", "WARN", "REJECT"}
