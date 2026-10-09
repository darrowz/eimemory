"""The deploy worker fast-forwards a clean, behind checkout to the verified target."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "eimemory-deploy-worker"


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def _setup(tmp_path: Path):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "master")
    (origin / "deploy").mkdir()
    installer = origin / "deploy" / "install_immutable_release.sh"
    installer.write_text('#!/usr/bin/env bash\necho "installer head=$(git rev-parse HEAD)"\n')
    installer.chmod(0o755)
    (origin / "deploy" / "collect_release_health.py").write_text("import json; print(json.dumps({'ok': True}))\n")
    (origin / "deploy" / "refresh_release_scope_bindings.py").write_text("print('ok')\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "one")
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(repo))
    (origin / "file.txt").write_text("two\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "two")
    target = _git(origin, "rev-parse", "HEAD")
    state = tmp_path / "state"
    current = tmp_path / "releases" / target
    (current / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, current / ".venv" / "bin" / "python")
    link = tmp_path / "current"
    link.symlink_to(current)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "systemctl").write_text("#!/usr/bin/env bash\necho active\n")
    (bin_dir / "systemctl").chmod(0o755)
    script = (SOURCE.read_text()
              .replace("REPO=/dev-project/eimemory", f"REPO={repo}")
              .replace("STATE_DIR=/var/lib/eimemory/state/deploy-jobs", f"STATE_DIR={state}")
              .replace("LOCK_FILE=/var/lib/eimemory/state/eimemory-deploy-worker.lock", f"LOCK_FILE={tmp_path}/w.lock")
              .replace("/opt/eimemory/current", str(link))
              .replace("/usr/bin/python3", sys.executable))
    worker = tmp_path / "worker"
    worker.write_text(script)
    worker.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return repo, target, worker, env, state


def test_check_reports_and_deploy_fast_forwards_clean_checkout(tmp_path) -> None:
    repo, target, worker, env, state = _setup(tmp_path)
    before = _git(repo, "rev-parse", "HEAD")
    check = subprocess.run([str(worker), "check", target], env=env, capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr
    assert '"checkout_fast_forward_required":true' in check.stdout
    assert _git(repo, "rev-parse", "HEAD") == before

    deploy = subprocess.run([str(worker), "deploy", target], env=env, capture_output=True, text=True, timeout=60)
    assert f"checkout_fast_forward={before}..{target}" in deploy.stderr
    assert _git(repo, "rev-parse", "HEAD") == target
    log = next(state.glob(f"{target}.attempt-*.log")).read_text()
    assert f"installer head={target}" in log


def test_diverged_checkout_is_refused_with_fix(tmp_path) -> None:
    repo, target, worker, env, _ = _setup(tmp_path)
    (repo / "local.txt").write_text("x\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local")
    local = _git(repo, "rev-parse", "HEAD")
    result = subprocess.run([str(worker), "deploy", target], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 65
    assert "not an ancestor" in result.stderr
    assert _git(repo, "rev-parse", "HEAD") == local
