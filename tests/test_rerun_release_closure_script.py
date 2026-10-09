"""deploy/rerun_release_closure.sh registers a manual closure capture."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "rerun_release_closure.sh"
COMMIT = "7" * 40


def _release(tmp_path: Path) -> Path:
    release = tmp_path / "releases" / COMMIT
    (release / "deploy").mkdir(parents=True)
    (release / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, release / ".venv" / "bin" / "python")
    (release / "deploy" / "record_release_closure_incident.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
    os.symlink(release, tmp_path / "current")
    return release


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "INSTALL_ROOT": str(tmp_path), "EIMEMORY_ROOT": str(tmp_path / "root"),
           "EIMEMORY_LOG_DIR": str(tmp_path / "logs"), "EIMEMORY_DEPLOY_SCOPE_USER": "darrow"}
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, check=False)


def test_register_existing_capture_goes_through_incident_recorder(tmp_path) -> None:
    _release(tmp_path)
    capture = tmp_path / "manual-closure.json"
    capture.write_text("{}", encoding="utf-8")
    completed = _run(tmp_path, "--register", str(capture), "--exit-status", "2")
    assert completed.returncode == 0, completed.stderr
    argv = json.loads(completed.stdout.strip().splitlines()[-1])
    opts = dict(zip(argv[::2], argv[1::2]))
    assert opts["--path"] == str(capture)
    assert opts["--expected-commit"] == COMMIT
    assert opts["--attempt-id"].startswith(f"{COMMIT}-manual-")
    assert opts["--closure-exit-status"] == "2"
    assert opts["--evidence-dir"] == str(tmp_path / "logs" / "release-closure-captures")
    assert (opts["--scope-agent"], opts["--scope-workspace"], opts["--scope-user"]) == ("hongtu", "embodied", "darrow")
    assert capture.exists()


def test_run_requires_prior_commit(tmp_path) -> None:
    _release(tmp_path)
    completed = _run(tmp_path)
    assert completed.returncode == 64
    assert "--prior-commit" in completed.stderr
