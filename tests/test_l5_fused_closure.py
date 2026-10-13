from pathlib import Path


def test_fused_closure_script_keeps_memory_plane_before_l5_gates() -> None:
    text = Path("deploy/run_memory_l5_fused_closure.sh").read_text(encoding="utf-8")
    assert "stage=memory_plane_eval" in text
    assert "eval production-query collect" in text
    assert "eval production-query status" in text
    assert "learn release-closure" in text
    assert text.index("stage=memory_plane_eval") < text.index("eval production-query collect")
    assert text.index("eval production-query status") < text.index("learn release-closure")
    assert "install_immutable_release.sh" not in text
    assert "production_query_not_ready" in text


import json
import os
import subprocess
import sys
import pytest


TIMER = 'eimemory-l1-extract.timer'
SERVICE = 'eimemory-l1-extract.service'


def _run_isolated(tmp_path, *, states=None, missing_repo=False, prior='1' * 40,
                  stop_failure='', restore_failure='', eval_failure=False,
                  failed_stop_paused=False, failed_state_after_stop=''):
    repo = tmp_path / 'repo'
    repo.mkdir()
    current = tmp_path / 'current'
    binary = current / '.venv/bin/python'
    binary.parent.mkdir(parents=True)
    controls = tmp_path / 'controls.json'
    controls.write_text(json.dumps(dict(states=states or {TIMER: 'active', SERVICE: 'active'},
        stop_failure=stop_failure, restore_failure=restore_failure,
        failed_stop_paused=failed_stop_paused, eval_failure=eval_failure,
        failed_state_after_stop=failed_state_after_stop)))
    trace = tmp_path / 'trace.jsonl'
    fake_dir = tmp_path / 'bin'
    fake_dir.mkdir()
    fake_systemctl = fake_dir / 'systemctl'
    fake_systemctl.write_text('#!' + sys.executable + '\n'
        'import json, os, sys\nfrom pathlib import Path\n'
        'path=Path(os.environ["FUSED_TEST_CONTROLS"]); data=json.loads(path.read_text())\n'
        'args=sys.argv[1:]; action=args[1]; unit=args[-1]\n'
        'with open(os.environ["FUSED_TEST_TRACE"],"a") as handle: handle.write(json.dumps(["systemctl",action,unit])+"\\n")\n'
        'if action=="show":\n'
        '    if data.get("last_stopped")==unit and data["failed_state_after_stop"]==unit: raise SystemExit(1)\n'
        '    print(data["states"].get(unit,"unknown")); raise SystemExit(0)\n'
        'if action=="stop":\n'
        '    failed=data["stop_failure"]==unit; data["last_stopped"]=unit\n'
        '    if not failed or data["failed_stop_paused"]: data["states"][unit]="inactive"\n'
        '    path.write_text(json.dumps(data)); raise SystemExit(3 if failed else 0)\n'
        'if action=="start":\n'
        '    if data["restore_failure"]==unit: raise SystemExit(5)\n'
        '    data["states"][unit]="active"; path.write_text(json.dumps(data)); raise SystemExit(0)\n'
        'raise SystemExit(99)\n')
    fake_systemctl.chmod(0o755)
    binary.write_text('#!' + sys.executable + '\n'
        'import json, os, sys\n'
        'with open(os.environ["FUSED_TEST_TRACE"],"a") as handle: handle.write(json.dumps(["python",*sys.argv[1:]])+"\\n")\n'
        'if "eimemory.cli.l1_worker" in sys.argv:\n'
        '    with open(os.environ["FUSED_TEST_CONTROLS"]) as handle: data=json.load(handle)\n'
        '    raise SystemExit(8 if data["eval_failure"] else 0)\n'
        'print("1" if "-c" in sys.argv else json.dumps({"ok":True,"ready":True}))\n')
    binary.chmod(0o755)
    log = tmp_path / 'closure.log'
    env = {**os.environ, 'PATH': str(fake_dir) + os.pathsep + os.environ.get('PATH', '/usr/bin:/bin'),
        'EIMEMORY_TRUSTED_REPOSITORY_ROOT': '' if missing_repo else str(repo),
        'EIMEMORY_DEPLOYMENT_REPO_ROOT': '', 'EIMEMORY_DEPLOYMENT_CURRENT_LINK': str(current),
        'EIMEMORY_PRIOR_COMMIT': prior, 'EIMEMORY_L5_FUSED_LOG': str(log),
        'EIMEMORY_L5_QUERY_STATUS_JSON': str(tmp_path / 'status.json'),
        'FUSED_TEST_CONTROLS': str(controls), 'FUSED_TEST_TRACE': str(trace)}
    script = Path(__file__).resolve().parents[1] / 'deploy/run_memory_l5_fused_closure.sh'
    result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=15)
    calls = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
    return result, calls, log.read_text(), json.loads(controls.read_text())['states']


@pytest.mark.parametrize('invalid', ['repo', 'prior'])
def test_invalid_fused_configuration_never_changes_l1(tmp_path, invalid):
    result, calls, log, _ = _run_isolated(tmp_path, missing_repo=invalid == 'repo',
        prior='invalid' if invalid == 'prior' else '1' * 40)
    assert result.returncode == 2
    assert calls == []
    assert 'error=' in log


@pytest.mark.parametrize('initial', ['inactive', 'failed'])
def test_fused_closure_does_not_start_previously_idle_resources(tmp_path, initial):
    result, calls, log, states = _run_isolated(tmp_path, states={TIMER: initial, SERVICE: initial})
    assert result.returncode == 0, log
    assert not [call for call in calls if call[:2] == ['systemctl', 'start']]
    assert all(value == 'inactive' for value in states.values())
    assert ['python', '-m', 'eimemory.cli.l1_worker'] in calls


@pytest.mark.parametrize('stop_failure', [TIMER, SERVICE])
def test_fused_stop_failure_blocks_evaluation_and_restores_only_paused_resources(tmp_path, stop_failure):
    result, calls, log, _ = _run_isolated(tmp_path, stop_failure=stop_failure)
    assert result.returncode == 2
    assert not [call for call in calls if call[0] == 'python']
    restored = [call[2] for call in calls if call[:2] == ['systemctl', 'start']]
    assert restored == ([] if stop_failure == TIMER else [TIMER])
    assert 'error=l1_stop_failed unit=' + stop_failure in log


def test_failed_stop_that_actually_paused_is_restored_but_still_fails(tmp_path):
    result, calls, log, states = _run_isolated(tmp_path, stop_failure=TIMER, failed_stop_paused=True)
    assert result.returncode == 2
    assert not [call for call in calls if call[0] == 'python']
    assert ['systemctl', 'start', TIMER] in calls
    assert states[TIMER] == 'active'


@pytest.mark.parametrize('eval_failure', [False, True])
def test_fused_restores_service_before_timer_on_success_and_evaluation_failure(tmp_path, eval_failure):
    result, calls, log, states = _run_isolated(tmp_path, eval_failure=eval_failure)
    assert result.returncode == (8 if eval_failure else 0), log
    assert [call[2] for call in calls if call[:2] == ['systemctl', 'start']] == [SERVICE, TIMER]
    assert all(value == 'active' for value in states.values())


@pytest.mark.parametrize('eval_failure', [False, True])
def test_fused_restore_failure_remains_visible_and_nonzero(tmp_path, eval_failure):
    result, calls, log, _ = _run_isolated(tmp_path, restore_failure=SERVICE, eval_failure=eval_failure)
    assert result.returncode == (8 if eval_failure else 2)
    assert 'error=l1_restore_failed unit=' + SERVICE in log
    assert ['systemctl', 'start', TIMER] in calls


def test_unknown_l1_state_is_rejected_before_stopping_any_resource(tmp_path):
    result, calls, log, _ = _run_isolated(tmp_path, states={TIMER: 'active', SERVICE: 'deactivating'})
    assert result.returncode == 2
    assert all(call[:2] == ['systemctl', 'show'] for call in calls)
    assert 'error=l1_state_unavailable' in log


def test_successful_pause_with_failed_state_probe_still_restores_owned_unit(tmp_path):
    result, calls, log, states = _run_isolated(tmp_path, failed_state_after_stop=TIMER)
    assert result.returncode == 2
    assert not [call for call in calls if call[0] == 'python']
    assert ['systemctl', 'start', TIMER] in calls
    assert states[TIMER] == 'active'
    assert 'error=l1_stop_failed unit=' + TIMER in log
