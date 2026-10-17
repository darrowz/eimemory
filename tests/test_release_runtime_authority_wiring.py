"""The standard closure owners carry installed authority, never RPC secrets."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

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
