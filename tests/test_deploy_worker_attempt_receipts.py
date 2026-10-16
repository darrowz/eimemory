"""Deploy worker: a failed same-commit re-run must not clobber a successful receipt.

On honrui a re-run of the live commit 4dce1405 was (correctly) refused by the
installer, but the worker truncated ``<commit>.log`` and replaced the
successful ``<commit>.json`` with ``ok=false``, destroying the deploy evidence.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "deploy" / "eimemory-deploy-worker"
COMMIT = "4dce1405875a62b786acf8a68d9c8f02790fd252"


@pytest.fixture()
def harness(tmp_path, deploy_worker_environment):
    if shutil.which("bash") is None or shutil.which("flock") is None:
        pytest.skip("bash and flock are required")
    repo = tmp_path / "repo"
    (repo / "deploy").mkdir(parents=True)
    state = tmp_path / "state"
    current = tmp_path / "opt" / "releases" / COMMIT
    (current / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, current / ".venv" / "bin" / "python")
    link = tmp_path / "opt" / "current"
    link.symlink_to(current)
    installer = repo / "deploy" / "install_immutable_release.sh"
    installer.write_text('#!/usr/bin/env bash\necho "installer attempt rc=$FAKE_INSTALLER_RC"\nexit "$FAKE_INSTALLER_RC"\n')
    installer.chmod(0o755)
    (repo / "deploy" / "collect_release_health.py").write_text(
        f'import json; print(json.dumps({{"ok": True, "commit": "{COMMIT}"}}))\n')
    (repo / "deploy" / "refresh_release_scope_bindings.py").write_text("print('bindings ok')\n")
    (repo / "deploy" / "clean_release_bytecode.py").write_text("print('bytecode clean')\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(
        f'#!/usr/bin/env bash\ncase "$1" in\n  rev-parse) echo {COMMIT};;\n  status) ;;\n  *) ;;\nesac\n')
    (bin_dir / "systemctl").write_text('#!/usr/bin/env bash\necho active\n')
    for tool in ("git", "systemctl"):
        (bin_dir / tool).chmod(0o755)
    script = SOURCE.read_text()
    script = (script
              .replace("STATE_DIR=/var/lib/eimemory/state/deploy-jobs", f"STATE_DIR={state}")
              .replace("LOCK_FILE=/var/lib/eimemory/state/eimemory-deploy-worker.lock", f"LOCK_FILE={tmp_path}/worker.lock")
              .replace("/opt/eimemory/current", str(link))
              .replace('PYTHONPATH="$REPO"', 'PYTHONPATH="$PYTHONPATH:$REPO"')
              .replace("/usr/bin/python3", sys.executable))
    worker = tmp_path / "worker"
    worker.write_text(script)
    worker.chmod(0o755)
    py_dir = Path(sys.executable).parent

    def run(rc: int):
        isolated, settings = deploy_worker_environment
        env = {**isolated, "PATH": f"{bin_dir}:{py_dir}:/usr/bin:/bin", "FAKE_INSTALLER_RC": str(rc), "REPO_DIR": str(repo)}
        result = subprocess.run([str(worker), "deploy", COMMIT], env=env, capture_output=True, text=True, timeout=60)
        if rc == 0:
            assert json.loads(settings.read_text()) == {"trusted_repository_root": str(repo), "preserve": True}
        return result

    return run, state


def _attempts(state: Path) -> list[Path]:
    return sorted(state.glob(f"{COMMIT}.attempt-*.json"))


def test_failed_rerun_keeps_successful_receipt_and_log(harness):
    run, state = harness
    first = run(0)
    assert first.returncode == 0, first.stderr
    receipt = state / f"{COMMIT}.json"
    log = state / f"{COMMIT}.log"
    ok_receipt, ok_log = receipt.read_bytes(), log.read_bytes()
    assert json.loads(ok_receipt)["ok"] is True
    assert b"rc=0" in ok_log

    second = run(2)
    assert second.returncode != 0
    assert receipt.read_bytes() == ok_receipt
    assert log.read_bytes() == ok_log
    attempts = [json.loads(p.read_text()) for p in _attempts(state)]
    assert [a["ok"] for a in attempts].count(False) == 1
    failed = next(a for a in attempts if a["ok"] is False)
    assert failed["installer_exit_code"] == 2
    assert b"rc=2" in Path(failed["log"]).read_bytes()
    assert '"publish":"kept_successful_receipt"' in second.stdout


def test_first_failure_is_published_and_later_success_replaces_it(harness):
    run, state = harness
    assert run(2).returncode != 0
    receipt = state / f"{COMMIT}.json"
    assert json.loads(receipt.read_text())["ok"] is False
    assert run(0).returncode == 0
    published = json.loads(receipt.read_text())
    assert published["ok"] is True
    assert b"rc=0" in (state / f"{COMMIT}.log").read_bytes()
    assert len(_attempts(state)) == 2


def test_success_receipt_records_bytecode_cleanup(harness):
    run, state = harness
    assert run(0).returncode == 0
    payload = json.loads((state / f"{COMMIT}.json").read_text())
    assert payload["bytecode_cleanup_exit_code"] == 0
    assert b"bytecode clean" in (state / f"{COMMIT}.log").read_bytes()


@pytest.mark.parametrize("deploy_worker_environment", ["path", "dir"], indirect=True)
def test_settings_target_is_explicit_and_backed_up(harness, deploy_worker_environment):
    run, _ = harness
    _, settings = deploy_worker_environment
    before = settings.read_bytes()
    assert run(0).returncode == 0
    backups = list(settings.parent.glob(settings.name + ".bak-trusted-root-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == before
    assert run(0).returncode == 0
    assert len(list(settings.parent.glob(settings.name + ".bak-trusted-root-*"))) == 1
