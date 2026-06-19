from __future__ import annotations

from pathlib import Path


def test_development_harness_artifacts_are_not_committed() -> None:
    assert not Path(".harness").exists()
    assert not Path("STATUS.md").exists()
    assert not Path("DEPLOY_RUNBOOK.md").exists()


def test_runtime_state_keeps_only_promotion_directory_placeholders() -> None:
    state_root = Path("state/autonomous_learning")
    allowed = {
        state_root / "active" / ".gitkeep",
        state_root / "canary" / ".gitkeep",
        state_root / "rolled_back" / ".gitkeep",
    }
    actual = {path for path in state_root.rglob("*") if path.is_file()}

    assert actual == allowed


def test_one_off_remote_install_helpers_are_not_committed() -> None:
    forbidden = {
        "scripts/_remote_download_lme.sh",
        "scripts/_remote_install.sh",
        "scripts/_remote_install2.sh",
        "scripts/ssh_push_code.py",
        "scripts/ssh_run.py",
        "scripts/ssh_run_script.py",
    }

    assert all(not Path(path).exists() for path in forbidden)
