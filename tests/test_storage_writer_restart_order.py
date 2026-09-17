"""Behavioral restart sequencing tests; no real systemd or production IO."""
from pathlib import Path
import shlex
import subprocess

import pytest


def function(name):
    script = Path('deploy/install_immutable_release.sh').read_text()
    return name + '() {' + script.split(name + '() {', 1)[1].split('\n}', 1)[0] + '\n}\n'


@pytest.mark.parametrize('failure', ['', 'rpc_start', 'rpc_ready', 'gateway_ready', 'watcher', 'worker_start'])
def test_captured_cores_are_ready_before_monitors(tmp_path, failure):
    release = tmp_path / ('a' * 40)
    release.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(release)
    setup = f'''
set -u
STORAGE_WRITERS_STOPPED=1
USER_SYSTEMD_ENABLE_SERVICE=1
CURRENT_LINK={shlex.quote(str(current))}
EXPECTED_RELEASE={shlex.quote(str(release))}
COMMIT={'b' * 40}
FAILURE={failure!r}
RPC_READY=0
GATEWAY_READY=0
WATCHER_READY=0
LOOP_TIMERS_READY=0
WORKER_READY=0
ACTIVE_STORAGE_WRITER_UNITS=(eimemory-timer-monitor.timer eimemory-timer-monitor.service eimemory-vector-sync.service openclaw-loop-watch.timer openclaw-gateway.service hermes-gateway.service eimemory-rpc.service)
_user_systemctl() {{
  printf 'start:%s\n' "$2"
  if [ "$2" = eimemory-rpc.service ] && [ "$FAILURE" = rpc_start ]; then return 9; fi
  if [ "$2" = eimemory-vector-sync.service ] && [ "$FAILURE" = worker_start ]; then return 9; fi
  if [ "$2" = openclaw-loop-watch.timer ]; then LOOP_TIMERS_READY=1; fi
  if [ "$2" = eimemory-vector-sync.service ]; then WORKER_READY=1; fi
  if [[ "$2" = eimemory-timer-monitor.* ]]; then
    [ "$RPC_READY" = 1 ] && [ "$GATEWAY_READY" = 1 ] && [ "$WATCHER_READY" = 1 ] && [ "$LOOP_TIMERS_READY" = 1 ] && [ "$WORKER_READY" = 1 ] || return 9
  fi
}}
_verify_release_health() {{
  printf 'ready:rpc\n'
  [ "$1" = "$EXPECTED_RELEASE" ] && [ "$2" = {'a' * 40} ] || return 8
  [ "$FAILURE" != rpc_ready ] || return 9
  RPC_READY=1
}}
_wait_openclaw_gateway_ready() {{
  printf 'ready:gateway\n'
  [ "$FAILURE" != gateway_ready ] || return 9
  GATEWAY_READY=1
}}
_resume_release_closure_reconcile() {{
  printf 'ready:watcher\n'
  [ "$FAILURE" != watcher ] || return 9
  WATCHER_READY=1
}}
'''
    script = setup + function('_restart_storage_writers') + '''
if _restart_storage_writers; then rc=0; else rc=$?; fi
printf 'result:%s stopped:%s\n' "$rc" "$STORAGE_WRITERS_STOPPED"
'''
    result = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == 'start:eimemory-rpc.service'
    if failure:
        assert lines[-1] == 'result:2 stopped:1'
        if failure != 'worker_start':
            assert 'start:eimemory-timer-monitor.service' not in lines
            assert 'start:eimemory-vector-sync.service' not in lines
    else:
        assert lines[-1] == 'result:0 stopped:0'
        assert lines.index('ready:rpc') < lines.index('start:eimemory-timer-monitor.service')
        assert lines.index('ready:gateway') < lines.index('start:eimemory-timer-monitor.service')
        assert lines.index('ready:watcher') < lines.index('start:eimemory-timer-monitor.service')
        assert lines.index('start:eimemory-vector-sync.service') < lines.index('start:eimemory-timer-monitor.service')
        assert all(lines.count('start:' + unit) == 1 for unit in (
            'eimemory-rpc.service', 'openclaw-gateway.service', 'hermes-gateway.service',
            'eimemory-timer-monitor.service', 'eimemory-vector-sync.service'))


def test_uncaptured_cores_are_not_started():
    setup = '''
STORAGE_WRITERS_STOPPED=1
USER_SYSTEMD_ENABLE_SERVICE=1
ACTIVE_STORAGE_WRITER_UNITS=(eimemory-vector-sync.timer)
_user_systemctl() { printf '%s %s\n' "$1" "$2"; }
_verify_release_health() { return 99; }
_wait_openclaw_gateway_ready() { return 99; }
'''
    result = subprocess.run(['bash', '-c', setup + function('_restart_storage_writers')
                             + '_restart_storage_writers'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['start eimemory-vector-sync.timer',
                                         'storage_writer_restart=complete restored=1']


@pytest.mark.parametrize('stopped,strict,starts_now', [('1', '0', False), ('0', '1', False), ('0', '0', True)])
def test_learning_metadata_does_not_start_quiesced_or_strict_timers(stopped, strict, starts_now):
    setup = f'''
STORAGE_WRITERS_STOPPED={stopped}
EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE={strict}
RELEASE_DIR=/unused
USER_SYSTEMD_DIR=/unused
_run_as_service_user() {{ :; }}
_install_as_service_user() {{ :; }}
_user_systemctl() {{ printf '%s\n' "$*"; }}
_start_learning_runtime_timers() {{ printf 'deferred-policy-start\n'; }}
'''
    script = Path('deploy/install_immutable_release.sh').read_text()
    if 'LEARNING_TIMER_UNITS=(' in script:
        setup += 'LEARNING_TIMER_UNITS=(' + script.split('LEARNING_TIMER_UNITS=(', 1)[1].split('\n)', 1)[0] + '\n)\n'
    result = subprocess.run(['bash', '-c', setup + function('_install_learning_runtime_policy')
                             + '_install_learning_runtime_policy /unused'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    commands = result.stdout.splitlines()
    assert len([line for line in commands if line.startswith('enable ')]) == 7
    immediate = any(line.startswith('enable --now ') or line.startswith('start ')
                    or line == 'deferred-policy-start' for line in commands)
    assert immediate is starts_now


@pytest.mark.parametrize('failure,strict', [('', '0'), ('rpc_health', '0'), ('gateway_ready', '0'), ('', '1')])
def test_core_restart_never_starts_background_timers(tmp_path, failure, strict):
    release = tmp_path / ('a' * 40)
    release.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(release)
    setup = f'''
USER_SYSTEMD_ENABLE_SERVICE=1
EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE={strict}
CURRENT_LINK={shlex.quote(str(current))}
FAILURE={failure!r}
_pause_release_closure_reconcile() {{ :; }}
_user_systemctl() {{ printf '%s\n' "$*"; }}
_verify_release_health() {{ printf 'rpc-ready\n'; [ "$FAILURE" != rpc_health ]; }}
_openclaw_is_enabled() {{ return 0; }}
_wait_openclaw_gateway_ready() {{ printf 'gateway-ready\n'; [ "$FAILURE" != gateway_ready ]; }}
_restart_hermes_gateway() {{ printf 'hermes-ready\n'; }}
_start_learning_runtime_timers() {{ printf 'policy-start\n'; }}
'''
    result = subprocess.run(['bash', '-c', setup + function('_restart_current_services') + '''
if _restart_current_services; then echo result:0; else echo result:failed; fi
'''], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert 'rpc-ready' in lines
    if failure:
        assert lines[-1] == 'result:failed'
        assert 'policy-start' not in lines
    else:
        assert lines[-1] == 'result:0'
        assert 'policy-start' not in lines
        assert lines.index('rpc-ready') < lines.index('gateway-ready') < lines.index('hermes-ready')
    assert not any(line.startswith('start ') and '.timer' in line for line in lines)


def test_strict_policy_timers_start_after_durable_commit():
    script = Path('deploy/install_immutable_release.sh').read_text()
    main = script[script.rindex('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"'):]
    assert main.index('COMMITTED=1') < main.index('_start_managed_runtime_timers')
    assert main.rindex('_restart_storage_writers') < main.index('_start_managed_runtime_timers')


def test_all_paths_restore_background_only_after_core_and_watcher():
    script = Path('deploy/install_immutable_release.sh').read_text()
    main = script[script.rindex('mv -Tf "$CURRENT_LINK.next" "$CURRENT_LINK"'):]
    rollback = function('_rollback_current_release')
    for body in (main, rollback):
        assert body.index('_restart_current_services') < body.index('_resume_release_closure_reconcile') < body.index('_restart_storage_writers')
        assert body.count('_restart_current_services') == 1
    recover = script.rsplit('if [ "$DEPLOY_MODE" = "--recover-only" ]; then', 1)[1].split('\nfi', 1)[0]
    assert '_restart_current_services' not in recover
    assert '_verify_release_health' in recover


def test_managed_policy_starter_preserves_seven_timer_activation():
    script = Path('deploy/install_immutable_release.sh').read_text()
    array = 'LEARNING_TIMER_UNITS=(' + script.split('LEARNING_TIMER_UNITS=(', 1)[1].split('\n)', 1)[0] + '\n)\n'
    setup = 'USER_SYSTEMD_ENABLE_SERVICE=1\n_user_systemctl() { printf "%s\\n" "$*"; }\n'
    result = subprocess.run(['bash', '-c', setup + array + function('_start_learning_runtime_timers')
                             + '_start_learning_runtime_timers'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 7
    assert all(line.startswith('start ') for line in result.stdout.splitlines())
