"""The standard closure owners carry installed authority, never RPC secrets."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'deploy' / 'install_immutable_release.sh'
RERUN = ROOT / 'deploy' / 'rerun_release_closure.sh'


def function_body(name):
    return INSTALLER.read_text().split(name + '() {', 1)[1].split('\n}', 1)[0]


@pytest.mark.parametrize('function', ['_run_pre_switch_production_recall_bootstrap', '_run_post_switch_closure'])
def test_installer_gate_invocations_receive_installed_runtime_authority(function):
    body = function_body(function)
    assert '--optional-runtime-authority-env-file "$EIMEMORY_CONFIG_DIR/runtime-authority.env" --' in body
    assert 'source "$EIMEMORY_CONFIG_DIR/rpc.env"' not in body
    assert '--runtime-authority-env-file "$EIMEMORY_CONFIG_DIR/rpc.env"' not in body


def test_research_configuration_check_does_not_import_runtime_authority():
    source = INSTALLER.read_text()
    prefix = source.split('--check-research-review', 1)[0].rsplit('() {', 1)[1]
    assert '--runtime-authority-env-file' not in prefix
    assert '--optional-runtime-authority-env-file' not in prefix


def test_first_install_keeps_rpc_token_separate_and_runtime_authority_optional():
    source = INSTALLER.read_text()
    provision = source.index('"$RELEASE_DIR/deploy/ensure_rpc_auth.py"')
    assert provision < source.index('\n_observe_pre_switch_l5\n')
    assert provision < source.index('if ! _run_post_deploy_validation', provision)
    for function in ('_run_pre_switch_production_recall_bootstrap', '_run_post_switch_closure'):
        body = function_body(function)
        assert '--optional-runtime-authority-env-file' in body
        assert '[ -f ' not in body and '[ -e ' not in body


def test_root_installer_runs_closure_and_recorder_as_database_service_user():
    body = function_body('_run_post_switch_closure')
    assert 'chown "$SERVICE_USER:$SERVICE_GROUP" "$closure_output"' in body
    assert body.count('_run_as_service_user env EIMEMORY_ROOT=') == 2


@pytest.mark.skipif(os.name != 'posix' or not shutil.which('bash'), reason='Native shell invocation requires POSIX bash')
def test_manual_rerun_supplies_custom_config_authority_file_to_actual_wrapper(tmp_path):
    commit = '7' * 40
    release = tmp_path / 'releases' / commit
    (release / 'deploy').mkdir(parents=True)
    (release / '.venv' / 'bin').mkdir(parents=True)
    (release / '.venv' / 'bin' / 'python').symlink_to(sys.executable)
    (tmp_path / 'current').symlink_to(release)
    capture = tmp_path / 'wrapper-arguments.json'
    (release / 'deploy' / 'run_with_governance_env.py').write_text(
        "import json,os,pathlib,sys\n"
        "pathlib.Path(os.environ['WIRING_CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
        "print('{}')\n")
    (release / 'deploy' / 'record_release_closure_incident.py').write_text("print('{}')\n")
    config = tmp_path / 'configured authority'
    config.mkdir()
    database = tmp_path / 'root' / 'state' / 'eimemory.sqlite'
    database.parent.mkdir(parents=True)
    database.touch()
    env = {**os.environ, 'INSTALL_ROOT': str(tmp_path), 'PYTHON_BIN': sys.executable,
           'EIMEMORY_CONFIG_DIR': str(config), 'EIMEMORY_ROOT': str(tmp_path / 'root'),
           'EIMEMORY_LOG_DIR': str(tmp_path / 'logs'), 'WIRING_CAPTURE': str(capture),
           'EIMEMORY_DEPLOY_SCOPE_AGENT': 'review-agent', 'EIMEMORY_DEPLOY_SCOPE_WORKSPACE': 'review-workspace',
           'EIMEMORY_DEPLOY_SCOPE_USER': 'review-user', 'PYTHONDONTWRITEBYTECODE': '1'}
    result = subprocess.run(['bash', str(RERUN), '--prior-commit', '6' * 40],
                            env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    argv = json.loads(capture.read_text())
    index = argv.index('--optional-runtime-authority-env-file')
    assert argv[index + 1] == str(config / 'runtime-authority.env')
    assert argv.index('--') > index
    assert argv[argv.index('--') + 2:][:2] == ['learn', 'release-closure']


@pytest.mark.skipif(os.name != 'posix', reason='POSIX installed authority boundary')
@pytest.mark.parametrize('state', ['missing', 'unsafe', 'broken-symlink', 'mutually-exclusive'])
def test_optional_authority_wrapper_executes_only_for_safe_initial_install(tmp_path, state):
    authority = tmp_path / 'runtime-authority.env'
    marker = tmp_path / 'child-ran'
    if state == 'unsafe':
        authority.write_text('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE=/private/pin.json\n')
        authority.chmod(0o666)
    elif state == 'broken-symlink':
        authority.symlink_to(tmp_path / 'missing-target')
    command = [sys.executable, '-I', '-B', str(ROOT / 'deploy/run_with_governance_env.py'),
               '--env-file', str(tmp_path / 'missing-governance.env'), '--optional',
               '--optional-runtime-authority-env-file', str(authority)]
    if state == 'mutually-exclusive':
        command += ['--runtime-authority-env-file', str(authority)]
    command += ['--', sys.executable, '-c',
                'from pathlib import Path;Path(' + repr(str(marker)) + ').touch()']
    environment = dict(os.environ)
    environment.pop('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE', None)
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == (0 if state == 'missing' else 2), result.stderr
    assert marker.exists() == (state == 'missing')


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='Native root-to-service-user entry requires Linux root')
def test_root_rerun_delegates_closure_and_capture_to_actual_database_owner():
    import pwd

    try:
        service = pwd.getpwnam('nobody')
    except KeyError:
        pytest.skip('An unprivileged fixture account is required')
    with tempfile.TemporaryDirectory(prefix='eimemory-root-rerun-', dir=os.getenv('TMPDIR', '/var/tmp')) as directory:
        root = Path(directory)
        commit = '7' * 40
        release = root / 'releases' / commit
        (release / 'deploy').mkdir(parents=True)
        (release / '.venv' / 'bin').mkdir(parents=True)
        (release / '.venv' / 'bin' / 'python').symlink_to('/usr/bin/python3')
        (root / 'current').symlink_to(release)
        database = root / 'runtime' / 'state' / 'eimemory.sqlite'
        database.parent.mkdir(parents=True)
        database.touch()
        (root / 'config').mkdir()
        script = root / 'rerun_release_closure.sh'
        script.write_text(RERUN.read_text())
        (root / 'deploy_scope_default.sh').write_text((ROOT / 'deploy/deploy_scope_default.sh').read_text())
        (release / 'deploy/run_with_governance_env.py').write_text(
            'import json,os\nprint(json.dumps({"uid":os.geteuid()}))\n')
        (release / 'deploy/record_release_closure_incident.py').write_text(
            'import json,os,pathlib,sys\n'
            'path=pathlib.Path(sys.argv[sys.argv.index("--path")+1])\n'
            'print(json.dumps({"closure_uid":json.loads(path.read_text())["uid"],'
            '"capture_uid":path.stat().st_uid,"recorder_uid":os.geteuid()}))\n')
        for path in [*root.rglob('*'), root]:
            os.chown(path, service.pw_uid, service.pw_gid, follow_symlinks=False)
        env = {**os.environ, 'SERVICE_USER': service.pw_name, 'INSTALL_ROOT': str(root),
               'EIMEMORY_ROOT': str(root / 'runtime'), 'EIMEMORY_CONFIG_DIR': str(root / 'config'),
               'EIMEMORY_LOG_DIR': str(root / 'logs'), 'PYTHON_BIN': '/usr/bin/python3'}
        result = subprocess.run(['bash', str(script), '--prior-commit', '6' * 40],
                                capture_output=True, text=True, env=env, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip()) == {
            'closure_uid': service.pw_uid, 'capture_uid': service.pw_uid, 'recorder_uid': service.pw_uid}
