"""Behavioral restart sequencing tests; no real systemd or production IO."""
from pathlib import Path
import shlex
import subprocess

import pytest


LEARNING_TIMERS = (
    'eimemory-real-effect.timer',
    'eimemory-l1-extract.timer',
    'eimemory-nightly.timer',
    'eimemory-learn-watch.timer',
    'eimemory-learn-think.timer',
    'eimemory-learn-dashboard.timer',
    'eimemory-l5-effect-review.timer',
    'eimemory-audit-verify.timer',
    'eimemory-timer-monitor.timer',
    'eimemory-backup.timer',
)


@pytest.mark.parametrize('failure', ['', 'eimemory-l1-extract.service', 'eimemory-l1-extract.timer'])
def test_learning_policy_installs_l1_before_enable_and_propagates_failure(tmp_path, failure):
    script = Path('deploy/install_immutable_release.sh').read_text()
    array = 'LEARNING_TIMER_UNITS=(' + script.split('LEARNING_TIMER_UNITS=(', 1)[1].split('\n)', 1)[0] + '\n)\n'
    setup = f'''
USER_SYSTEMD_DIR={shlex.quote(str(tmp_path / 'units'))}
_runtime_manager_enabled() {{ return 1; }}
_run_as_service_user() {{ :; }}
_install_as_service_user() {{
  [ "${{2##*/}}" != {shlex.quote(failure or 'none')} ] || return 42
  mkdir -p "$(dirname "$3")"
  install -m "$1" "$2" "$3"
}}
_user_systemctl() {{
  printf '%s\n' "$*"
  if [ "$1" = enable ]; then
    test -f "$USER_SYSTEMD_DIR/eimemory-l1-extract.service" || return 99
    test -f "$USER_SYSTEMD_DIR/eimemory-l1-extract.timer" || return 99
  fi
}}
'''
    result = _run_bash(setup + array + function('_install_learning_runtime_policy') +
        'if _install_learning_runtime_policy "$PWD"; then exit 0; else exit $?; fi', tmp_path=tmp_path)
    assert result.returncode == (42 if failure else 0), result.stderr
    if failure:
        assert 'enable ' not in result.stdout
    else:
        assert 'enable eimemory-l1-extract.timer' in result.stdout
        assert 'start eimemory-l1-extract' not in result.stdout
        for suffix in ('service', 'timer'):
            assert (tmp_path / 'units' / f'eimemory-l1-extract.{suffix}').read_bytes() == Path(
                f'deploy/systemd/eimemory-l1-extract.{suffix}').read_bytes()


@pytest.mark.parametrize('inventory_status', [0, 43])
def test_registered_manager_never_installs_or_enables_user_l1(tmp_path, inventory_status):
    setup = f'''
_runtime_manager_enabled() {{ return 0; }}
_runtime_manager_control() {{ [ "$*" = inventory ] || exit 99; return {inventory_status}; }}
_install_as_service_user() {{ exit 98; }}
_user_systemctl() {{ exit 97; }}
'''
    result = _run_bash(setup + function('_install_learning_runtime_policy') +
        '_install_learning_runtime_policy /unused', tmp_path=tmp_path)
    assert result.returncode == inventory_status


def test_first_install_l1_is_discovered_before_unit_publication(tmp_path):
    result = subprocess.run(['bash', 'deploy/discover_python_runtime_units.sh', str(tmp_path / 'missing')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines().count('eimemory-l1-extract.service') == 1


def test_metadata_failure_never_restarts_rpc_or_hides_failure(tmp_path):
    setup = '''
USER_SYSTEMD_ENABLE_SERVICE=1
_runtime_manager_enabled() { return 1; }
_install_current_runtime_metadata() { return 42; }
_user_systemctl() { echo forbidden-restart; }
'''
    result = _run_bash(setup + function('_refresh_current_runtime_metadata') +
        'if _refresh_current_runtime_metadata; then exit 0; else exit $?; fi', tmp_path=tmp_path)
    assert result.returncode == 42
    assert result.stdout == ''


@pytest.mark.parametrize('policy', ['_install_learning_runtime_policy', '_install_code_implementation_owner_policy'])
@pytest.mark.parametrize('kind,code', [('prepare', 41), ('install', 42), ('reload', 43), ('enable', 44)])
def test_learning_policy_stops_at_each_required_failure_under_conditional(tmp_path, kind, code, policy):
    script = Path('deploy/install_immutable_release.sh').read_text()
    array = 'LEARNING_TIMER_UNITS=(' + script.split('LEARNING_TIMER_UNITS=(', 1)[1].split('\n)', 1)[0] + '\n)\n'
    setup = '''
set -e
USER_SYSTEMD_DIR=/fixture/units
USER_SYSTEMD_ENABLE_SERVICE=1
_runtime_manager_enabled() { return 1; }
step() { printf '%s\n' "$*"; [ "$*" != "$FAIL_STEP" ] || return "$FAIL_CODE"; }
_run_as_service_user() { step prepare "$@"; }
_install_as_service_user() { step install "$@"; }
_user_systemctl() {
  case "$1" in
    daemon-reload) step reload "$@" ;;
    enable) step enable "$@" ;;
    start) step start "$@" ;;
    *) return 0 ;;
  esac
}
'''
    functions = array + function(policy) + function('_start_learning_runtime_timers')
    def run(failure=''):
        return _run_bash(setup + f'FAIL_STEP={shlex.quote(failure)}\nFAIL_CODE={code}\n' + functions + f'''
if {policy} "$PWD"; then
  _start_learning_runtime_timers
else
  exit $?
fi
''', tmp_path=tmp_path)
    baseline = run()
    assert baseline.returncode == 0, baseline.stderr
    steps = baseline.stdout.splitlines()
    failures = [index for index, item in enumerate(steps) if item.startswith(kind + ' ')]
    assert failures
    for index in failures:
        result = run(steps[index])
        assert result.returncode == code, (steps[index], result.stdout, result.stderr)
        assert result.stdout.splitlines() == steps[:index + 1]


@pytest.mark.parametrize('failure', ['mkdir', 'vector-service', 'vector-timer', 'reload-vector',
    'enable-vector', 'identity-dropin', 'rpc-service', 'learning', 'owner', 'hermes', 'reload-final', 'enable-rpc'])
def test_current_metadata_stops_at_required_failure_under_conditional(tmp_path, failure):
    # Execute the actual orchestration; only the external copy/Python/manager
    # effects and child-policy results are synthetic. No service is touched.
    repo = shlex.quote(str(Path.cwd()))
    setup = f'''
set -e
exec 3>&1
USER_SYSTEMD_ENABLE_SERVICE=1
USER_SYSTEMD_DIR=/fixture/units
RELEASE_DIR={repo}
REPO_DIR={repo}
INSTALL_ROOT=/fixture/install
SERVICE_USER=fixture
PYTHON_BIN=fixture_python
CONTROL_PYCACHE_PREFIX=/fixture/cache
EVIDENCE_RECEIPT_ENV_FILE=/fixture/evidence.env
FAIL_STEP={shlex.quote(failure)}
step() {{ printf '%s\n' "$1" >&3; [ "$1" != "$FAIL_STEP" ] || return 42; }}
_runtime_manager_enabled() {{ return 1; }}
id() {{ printf '1001\n'; }}
_run_as_service_user() {{
  case "$1" in
    mkdir) step mkdir ;;
    bash) printf 'eimemory-rpc.service\n' ;;
    *) step capture ;;
  esac
}}
_install_as_service_user() {{
  case "$2" in
    *vector-sync.service) step vector-service ;;
    *vector-sync.timer) step vector-timer ;;
    *rpc.service) step rpc-service ;;
  esac
}}
fixture_python() {{
  case "$*" in
    *runtime_identity_policy.py*) printf 'zzzz-eimemory-python-runtime.conf\n' ;;
    *install_managed_systemd_dropin.py*) step identity-dropin ;;
  esac
}}
RELOADS=0
_user_systemctl() {{
  if [ "$1" = daemon-reload ]; then
    RELOADS=$((RELOADS + 1))
    if [ "$RELOADS" = 1 ]; then step reload-vector; else step reload-final; fi
  elif [ "$*" = 'enable eimemory-rpc.service' ]; then step enable-rpc
  else step enable-vector; fi
}}
_is_unselected_openclaw_unit() {{ return 1; }}
_refresh_user_control_plane_commit() {{ :; }}
_install_learning_runtime_policy() {{ step learning; }}
_install_code_implementation_owner_policy() {{ step owner; }}
_install_hermes_integration() {{ step hermes; }}
'''
    result = _run_bash(setup + function('_install_current_runtime_metadata') + '''
if _install_current_runtime_metadata "$REPO_DIR" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$REPO_DIR"; then
  step forbidden-start
else
  exit $?
fi
''', tmp_path=tmp_path)
    assert result.returncode == 42, (result.stdout, result.stderr)
    assert result.stdout.splitlines()[-1] == failure


@pytest.mark.parametrize('failure', ['gateway-directory', 'gateway-dropin'])
def test_hermes_metadata_stops_before_later_gateway_after_failure(tmp_path, failure):
    setup = f'''
set -e
exec 3>&1
USER_SYSTEMD_ENABLE_SERVICE=1
USER_SYSTEMD_DIR=/fixture/units
HERMES_PYTHON=fixture_python
PYTHON_BIN=fixture_python
CONTROL_PYCACHE_PREFIX=/fixture/cache
HERMES_HOME_DIR=/fixture/hermes
SERVICE_HOME=/fixture/home
SERVICE_USER=fixture
REPO_DIR="$PWD"
INSTALL_ROOT=/fixture/install
EVIDENCE_RECEIPT_ENV_FILE=/fixture/evidence.env
FAIL_STEP={shlex.quote(failure)}
step() {{ printf '%s\n' "$1" >&3; [ "$1" != "$FAIL_STEP" ] || return 42; }}
id() {{ printf '1001\n'; }}
_hermes_is_installed() {{ return 0; }}
_user_systemctl() {{ return 0; }}
_run_as_service_user() {{
  case "$1" in
    mkdir) step gateway-directory ;;
    bash) printf 'hermes-gateway-second.service\n' ;;
    *) return 0 ;;
  esac
}}
fixture_python() {{
  case "$*" in
    *runtime_identity_policy.py*) printf 'zzzz-eimemory-python-runtime.conf\n' ;;
    *install_managed_systemd_dropin.py*) step gateway-dropin ;;
  esac
}}
'''
    result = _run_bash(setup + function('_install_hermes_integration') + '''
if _install_hermes_integration "$PWD" aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa "$PWD"; then
  step forbidden-start
else
  exit $?
fi
''', tmp_path=tmp_path)
    assert result.returncode == 42, (result.stdout, result.stderr)
    assert result.stdout.splitlines() == (['gateway-directory'] if failure == 'gateway-directory'
                                          else ['gateway-directory', 'gateway-dropin'])


def function(name):
    script = Path('deploy/install_immutable_release.sh').read_text()
    return name + '() {' + script.split(name + '() {', 1)[1].split('\n}', 1)[0] + '\n}\n'

def _run_bash(script: str, *, tmp_path: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run installer snippets; stub `systemctl` when the host has none."""
    import os

    env = None
    if tmp_path is not None:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        systemctl = bin_dir / "systemctl"
        if not systemctl.exists():
            systemctl.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            systemctl.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)



@pytest.mark.parametrize('failure', ['', 'rpc_start', 'rpc_ready', 'gateway_start', 'gateway_ready', 'hermes_start', 'watcher', 'worker_start'])
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
  if [ "$2" = openclaw-gateway.service ] && [ "$FAILURE" = gateway_start ]; then return 9; fi
  if [ "$2" = hermes-gateway.service ] && [ "$FAILURE" = hermes_start ]; then return 9; fi
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
    result = _run_bash(script, tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == 'start:eimemory-rpc.service'
    assert 'start:hermes-gateway.service' in lines
    if failure == 'rpc_start':
        assert 'ready:rpc' not in lines
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


def test_uncaptured_cores_are_not_started(tmp_path):
    setup = '''
STORAGE_WRITERS_STOPPED=1
USER_SYSTEMD_ENABLE_SERVICE=1
ACTIVE_STORAGE_WRITER_UNITS=(eimemory-vector-sync.timer)
_user_systemctl() { printf '%s %s\n' "$1" "$2"; }
_verify_release_health() { return 99; }
_wait_openclaw_gateway_ready() { return 99; }
'''
    result = _run_bash(setup + function('_restart_storage_writers') + '_restart_storage_writers', tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['start eimemory-vector-sync.timer',
                                         'storage_writer_restart=complete restored=1']


@pytest.mark.parametrize('stopped,strict', [('1', '0'), ('0', '1'), ('0', '0'), ('1', '1')])
def test_learning_metadata_never_dispatches_timers_before_receipt(stopped, strict):
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
    assert [line for line in commands if line.startswith('enable ')] == [
        'enable ' + unit for unit in LEARNING_TIMERS
    ]
    immediate = any(line.startswith('enable --now ') or line.startswith('start ')
                    or line == 'deferred-policy-start' for line in commands)
    # Metadata installation precedes the first verified native receipt in
    # every mode. Dispatch is a later, separately checked installation phase.
    assert immediate is False


@pytest.mark.parametrize('failure,strict', [('', '0'), ('rpc_start', '0'), ('rpc_health', '0'), ('current_missing', '0'), ('gateway_ready', '0'), ('hermes_start', '0'), ('', '1')])
def test_core_restart_never_starts_background_timers(tmp_path, failure, strict):
    release = tmp_path / ('a' * 40)
    release.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(release)
    if failure == 'current_missing':
        current.unlink()
    setup = f'''
USER_SYSTEMD_ENABLE_SERVICE=1
EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE={strict}
CURRENT_LINK={shlex.quote(str(current))}
FAILURE={failure!r}
_pause_release_closure_reconcile() {{ :; }}
_user_systemctl() {{ printf '%s\n' "$*"; [ "$*:$FAILURE" != 'restart eimemory-rpc.service:rpc_start' ]; }}
_verify_release_health() {{ printf 'rpc-ready\n'; [ "$FAILURE" != rpc_health ]; }}
_openclaw_is_enabled() {{ return 0; }}
_wait_openclaw_gateway_ready() {{ printf 'gateway-ready\n'; [ "$FAILURE" != gateway_ready ]; }}
_restart_hermes_gateway() {{ printf 'hermes-ready\n'; [ "$FAILURE" != hermes_start ]; }}
_start_learning_runtime_timers() {{ printf 'policy-start\n'; }}
'''
    result = _run_bash(setup + function('_restart_current_services') + '''
if _restart_current_services; then echo result:0; else echo result:failed; fi
''', tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert 'hermes-ready' in lines
    assert ('rpc-ready' in lines) is (failure not in ('rpc_start', 'current_missing'))
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


def test_managed_runtime_timers_start_l1_once_with_learning_policy(tmp_path):
    setup = '''
USER_SYSTEMD_ENABLE_SERVICE=1
_openclaw_is_enabled() { return 1; }
_user_systemctl() { printf '%s\\n' "$*"; }
'''
    script = Path('deploy/install_immutable_release.sh').read_text()
    array = 'LEARNING_TIMER_UNITS=(' + script.split('LEARNING_TIMER_UNITS=(', 1)[1].split('\n)', 1)[0] + '\n)\n'
    result = _run_bash(setup + array + function('_start_learning_runtime_timers') + function('_start_managed_runtime_timers')
                       + '_start_managed_runtime_timers', tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines.count('start eimemory-l1-extract.timer') == 1


def test_managed_policy_starter_preserves_all_timer_activation():
    script = Path('deploy/install_immutable_release.sh').read_text()
    array = 'LEARNING_TIMER_UNITS=(' + script.split('LEARNING_TIMER_UNITS=(', 1)[1].split('\n)', 1)[0] + '\n)\n'
    setup = 'USER_SYSTEMD_ENABLE_SERVICE=1\n_user_systemctl() { printf "%s\\n" "$*"; }\n'
    result = subprocess.run(['bash', '-c', setup + array + function('_start_learning_runtime_timers')
                             + '_start_learning_runtime_timers'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['start ' + unit for unit in LEARNING_TIMERS]


@pytest.mark.parametrize('entry', ['rollback', 'recover_validation', 'recover_finalize'])
@pytest.mark.parametrize('failure', ['', 'rpc_start', 'rpc_health', 'hermes_start'])
def test_rollback_recover_boundary_restores_communication_and_keeps_failure(tmp_path, entry, failure):
    prior = tmp_path / 'releases' / ('a' * 40)
    candidate = tmp_path / 'releases' / ('b' * 40)
    prior.mkdir(parents=True)
    candidate.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(prior)
    marker = tmp_path / 'transaction.json'
    marker.write_text('{}')
    setup = f'''
set -eu
USER_SYSTEMD_ENABLE_SERVICE=1
CURRENT_LINK={shlex.quote(str(current))}
PREVIOUS_CURRENT={shlex.quote(str(prior))}
PREVIOUS_COMMIT={'a' * 40}
RELEASE_DIR={shlex.quote(str(candidate))}
COMMIT={'b' * 40}
INSTALL_ROOT={shlex.quote(str(tmp_path))}
EIMEMORY_ROOT={shlex.quote(str(tmp_path))}
REPO_DIR={shlex.quote(str(tmp_path))}
STORAGE_TRANSACTION_MARKER={shlex.quote(str(marker))}
STORAGE_TRANSACTION_CLEARING="$STORAGE_TRANSACTION_MARKER.clearing"
STORAGE_TRANSACTION_RECOVERY="$STORAGE_TRANSACTION_MARKER.recovery"
STORAGE_TRANSACTION_CURRENT_LINK="$CURRENT_LINK"
STORAGE_TRANSACTION_CANDIDATE_COMMIT="$COMMIT"
STORAGE_TRANSACTION_PRIOR_COMMIT="$PREVIOUS_COMMIT"
STORAGE_TRANSACTION_PHASE=rollback_validating
STORAGE_TRANSACTION_ACTIVE=1
STORAGE_SNAPSHOT_READY=0
STORAGE_SNAPSHOT_MANIFEST_SHA256=''
STORAGE_WRITERS_STOPPED=1
CURRENT_SWITCHED=0
ACTIVE_STORAGE_WRITER_UNITS=(eimemory-rpc.service hermes-gateway.service eimemory-vector-sync.service)
FAILURE={failure!r}
ACTION={'finalize_rollback' if entry == 'recover_finalize' else 'resume_rollback_validation'}
PYTHON_BIN=fake_python
STORAGE_TRANSACTION_HELPER=unused
fake_python() {{ echo "$ACTION"; }}
_load_storage_release_transaction() {{ :; }}
_retire_system_rpc_unit() {{ :; }}
_stop_storage_writers() {{ :; }}
_refresh_openclaw_gateway_metadata() {{ :; }}
_install_current_runtime_metadata() {{ :; }}
_install_openclaw_loop_compat_script() {{ :; }}
_install_openclaw_bundled_bridge() {{ :; }}
_refresh_openclaw_plugin_registry() {{ :; }}
_acquire_candidate_validation_lock() {{ :; }}
_update_storage_release_transaction() {{ echo "phase:$1"; }}
_verify_hermes_integration() {{ :; }}
_start_code_implementation_owner() {{ :; }}
_inspect_openclaw_plugin_runtime() {{ :; }}
_verify_effective_runtime_metadata() {{ :; }}
_pause_release_closure_reconcile() {{ :; }}
_openclaw_is_enabled() {{ return 1; }}
_verify_release_health() {{ echo rpc-health; [ "$1" = "$PREVIOUS_CURRENT" ] && [ "$2" = "$PREVIOUS_COMMIT" ] && [ "$FAILURE" != rpc_health ]; }}
_restart_hermes_gateway() {{ echo hermes-attempt; [ "$FAILURE" != hermes_start ]; }}
_user_systemctl() {{
  echo "systemctl:$*"
  if [[ "$*" = *eimemory-rpc.service && "$FAILURE" = rpc_start ]]; then return 42; fi
  if [[ "$*" = *hermes-gateway.service && "$FAILURE" = hermes_start ]]; then return 42; fi
  return 0
}}
_clear_storage_release_transaction() {{ echo clear; }}
_reset_storage_transaction_state() {{ echo reset; }}
_resume_release_closure_reconcile() {{ echo watcher; }}
_start_managed_runtime_timers() {{ echo timers; }}
'''
    script = setup + ''.join(function(name) for name in (
        '_restart_current_services', '_restart_storage_writers',
        '_rollback_current_release', '_reconcile_interrupted_storage_release'))
    script += ('_rollback_current_release resume_validation\n' if entry == 'rollback'
               else '_reconcile_interrupted_storage_release\n')
    script += 'echo boundary-complete\n'
    result = _run_bash(script, tmp_path=tmp_path)
    lines = result.stdout.splitlines()
    assert ('systemctl:start hermes-gateway.service' in lines if entry == 'recover_finalize'
            else 'hermes-attempt' in lines), (result.stdout, result.stderr)
    if failure:
        assert result.returncode != 0
        assert 'boundary-complete' not in lines
        assert 'systemctl:start eimemory-vector-sync.service' not in lines
        assert 'timers' not in lines
        assert 'phase:rollback_validated' not in lines
        assert 'reset' not in lines
    else:
        assert result.returncode == 0, result.stderr
        assert lines[-1] == 'boundary-complete'


def test_ordinary_deploy_uses_user_manager_and_rejects_system_rpc(tmp_path):
    unit = tmp_path / 'eimemory-rpc.service'
    unit.write_text('[Service]\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    systemctl = bin_dir / 'systemctl'
    systemctl.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$SYSTEMCTL_LOG"\nexit 0\n')
    systemctl.chmod(0o755)
    log = tmp_path / 'systemctl.log'
    script = function('_runtime_manager_job_bound') + function('_runtime_manager_enabled')
    script += function('_reject_unsupported_system_rpc_owner')
    script += f'''
set -eu
SYSTEM_RPC_UNIT_PATH={shlex.quote(str(unit))}
DEPLOY_MODE=deploy
export PATH={shlex.quote(str(bin_dir))}:$PATH
export SYSTEMCTL_LOG={shlex.quote(str(log))}
unset EIMEMORY_RUNTIME_TRANSITION_JOB
if _runtime_manager_enabled; then echo manager-enabled; exit 3; fi
_reject_unsupported_system_rpc_owner
'''
    result = _run_bash(script, tmp_path=tmp_path)
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert 'legacy_system_rpc=unsupported_owner' in result.stderr
    assert 'ordinary_deploy=blocked user_systemd_required' in result.stderr
    assert 'probe' not in log.read_text()
    body = function('_user_systemctl')
    assert 'systemctl --user "$@"' in body
    assert '_runtime_manager_job_bound' in function('_runtime_manager_enabled')
    metadata = function('_install_current_runtime_metadata')
    ordinary = metadata.split('local target_release=', 1)[1]
    assert '_refresh_user_control_plane_commit' in ordinary
    assert ordinary.count('--render-commit "$target_commit"') == 1
    assert '_refresh_user_control_plane_commit "$runtime_unit" "$target_commit"' in ordinary
    hermes = function('_install_hermes_integration')
    assert '_refresh_user_control_plane_commit' not in hermes
    assert ordinary.index('_refresh_user_control_plane_commit') < ordinary.rindex(
        '_user_systemctl daemon-reload')
