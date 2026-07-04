from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def test_immutable_release_installer_runs_openclaw_loop_deploy_verify() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert "openclaw_loop.py\" deploy-verify" in script
    assert "--commit \"$COMMIT\"" in script
    assert "--release-path \"$RELEASE_DIR\"" in script


def test_immutable_release_installer_refreshes_legacy_openclaw_loop_script() -> None:
    script = Path("deploy/install_immutable_release.sh").read_text(encoding="utf-8")

    assert "OPENCLAW_LOOP_COMPAT_SCRIPT" in script
    assert "install -m 0755 \"$RELEASE_DIR/scripts/openclaw_loop.py\" \"$OPENCLAW_LOOP_COMPAT_SCRIPT\"" in script
    assert "ln -sfn \"$RELEASE_DIR/scripts/openclaw_loop.py\" \"$OPENCLAW_LOOP_COMPAT_SCRIPT\"" not in script
    assert "chmod +x \"$RELEASE_DIR/scripts/openclaw_loop.py\"" in script


def test_openclaw_loop_wrapper_is_executable() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-s", "scripts/openclaw_loop.py"],
        check=True,
        text=True,
        capture_output=True,
    )

    assert result.stdout.startswith("100755 ")


def test_openclaw_loop_wrapper_runs_from_workspace_scripts_copy(tmp_path) -> None:
    workspace_scripts = tmp_path / "workspace" / "scripts"
    workspace_scripts.mkdir(parents=True)
    copied = workspace_scripts / "openclaw_loop.py"
    copied.write_text(Path("scripts/openclaw_loop.py").read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(copied), "--help"],
        check=True,
        text=True,
        capture_output=True,
        env={"EIMEMORY_REPO": str(Path.cwd())},
    )

    assert "OpenClaw/eimemory loop ledger" in result.stdout
