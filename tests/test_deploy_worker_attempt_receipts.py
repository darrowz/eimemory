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
    (repo / "deploy" / "refresh_release_scope_bindings.py").write_text(
        "import os,sys\nprint('bindings ok')\nsys.exit(int(os.environ.get('FAKE_BINDING_RC','0')))\n")
    (repo / "deploy" / "clean_release_bytecode.py").write_text(
        "import os,sys\nprint('bytecode clean')\nsys.exit(int(os.environ.get('FAKE_CLEANUP_RC','0')))\n")
    (repo / "deploy" / "run_code_deployment_gate.py").write_text(
        "import json,os,sys\n"
        "assert sys.argv[1:]==['--job-key','a'*64,'--operation','post_resume','--worker-finalization']\n"
        "settings=json.load(open(os.environ.get('EIMEMORY_CONFIG_PATH') or os.path.join(os.environ['EIMEMORY_CONFIG_DIR'],'settings.json')))\n"
        "assert settings['trusted_repository_root']==os.environ['REPO_DIR']\n"
        "print('worker final gate after settings')\n"
        "sys.exit(int(os.environ.get('FAKE_GATE_RC','0')))\n")
    # The worker uses the registered manager CLI, not a PATH systemctl. Stub
    # only that OS boundary; every receipt/settings step below remains real.
    (repo / "deploy" / "runtime_manager_control.py").write_text(
        "import sys\n"
        "assert sys.argv[1:] == ['systemctl', 'is-active', 'eimemory-rpc.service']\n"
        "print('active')\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "git").write_text(
        f'#!/usr/bin/env bash\ncase "$1" in\n  rev-parse) echo {COMMIT};;\n  status) ;;\n  *) ;;\nesac\n')
    (bin_dir / "systemctl").write_text('#!/usr/bin/env bash\necho unexpected_direct_systemctl >&2\nexit 99\n')
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

    def run(rc: int, *, inactive=False, binding_rc=0, cleanup_rc=0, gate_rc=0,
            settings_failure=False, automatic=False):
        isolated, settings = deploy_worker_environment
        if settings_failure:
            settings.write_text('[]')
        if inactive:
            (repo / 'deploy' / 'runtime_manager_control.py').write_text("import sys\nprint('inactive')\nsys.exit(3)\n")
            (bin_dir / 'systemctl').write_text('#!/bin/sh\n[ "$1" = --user ] || exit 99\necho inactive\nexit 3\n')
        env = {**isolated, "PATH": f"{bin_dir}:{py_dir}:/usr/bin:/bin", "FAKE_INSTALLER_RC": str(rc), "REPO_DIR": str(repo)}
        env.update(FAKE_BINDING_RC=str(binding_rc), FAKE_CLEANUP_RC=str(cleanup_rc),
                   FAKE_GATE_RC=str(gate_rc), EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE='1' if automatic else '0')
        if not inactive:
            env['EIMEMORY_RUNTIME_TRANSITION_JOB'] = 'a' * 64
        else:
            env.pop('EIMEMORY_RUNTIME_TRANSITION_JOB', None)
        result = subprocess.run([str(worker), "deploy", COMMIT], env=env, capture_output=True, text=True, timeout=60)
        if rc == 0 and not settings_failure:
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
    failed = json.loads(receipt.read_text())
    assert failed["ok"] is False and failed["installer_exit_code"] == 2
    assert run(0).returncode == 0
    published = json.loads(receipt.read_text())
    assert published["ok"] is True
    assert b"rc=0" in (state / f"{COMMIT}.log").read_bytes()
    assert len(_attempts(state)) == 2


def test_inactive_rpc_still_persists_failed_attempt(harness):
    run, state = harness
    result = run(2, inactive=True)
    assert result.returncode != 0
    attempts = _attempts(state)
    assert len(attempts) == 1, result.stderr
    payload = json.loads(attempts[0].read_text())
    assert payload['ok'] is False
    assert payload['installer_exit_code'] == 2
    assert payload['rpc_service'] == 'inactive'
    assert payload['rpc_service_exit_code'] == 3


def test_success_receipt_records_bytecode_cleanup(harness):
    run, state = harness
    assert run(0).returncode == 0
    payload = json.loads((state / f"{COMMIT}.json").read_text())
    assert payload["bytecode_cleanup_exit_code"] == 0
    assert b"bytecode clean" in (state / f"{COMMIT}.log").read_bytes()
    assert payload['settings_exit_code'] == payload['post_resume_gate_exit_code'] == 0
    assert payload['post_resume_gate_required'] is payload['post_resume_gate_attempted'] is True
    text = (state / f'{COMMIT}.log').read_text()
    assert text.index('bindings ok') < text.index('bytecode clean') < text.index('worker final gate after settings')


@pytest.mark.parametrize('fault', ['binding', 'cleanup', 'settings'])
def test_worker_prerequisite_failure_preserves_incomplete_manual_attempt(harness, fault):
    run, state = harness
    result = run(0, binding_rc=2 if fault == 'binding' else 0,
        cleanup_rc=2 if fault == 'cleanup' else 0, settings_failure=fault == 'settings')
    assert result.returncode == 1
    payload = json.loads((state / f'{COMMIT}.json').read_text())
    assert payload['ok'] is False
    assert payload['post_resume_gate_required'] is True
    assert payload['post_resume_gate_attempted'] is False
    assert payload['post_resume_gate_exit_code'] == 125
    assert 'worker final gate after settings' not in Path(payload['log']).read_text()


def test_failed_final_native_gate_cannot_publish_success(harness):
    run, state = harness
    assert run(0, gate_rc=2).returncode == 1
    payload = json.loads((state / f'{COMMIT}.json').read_text())
    assert payload['post_resume_gate_attempted'] is True
    assert payload['post_resume_gate_exit_code'] == 2 and payload['ok'] is False


def test_automatic_owner_keeps_its_existing_finalization_chain(harness):
    run, state = harness
    assert run(0, automatic=True).returncode == 0
    payload = json.loads((state / f'{COMMIT}.json').read_text())
    assert payload['post_resume_gate_required'] is payload['post_resume_gate_attempted'] is False
    assert payload['post_resume_gate_exit_code'] == 0


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
