"""Exercise discovery against installed unit fixtures without real systemd."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONTROL_UNITS = (
    'eimemory-runtime-manager.service',
    'eimemory-code-deployment-job@fixture.service',
    'eimemory-code-validation-receipt@fixture.service',
    'eimemory-code-validation-pre-observation@fixture.service',
    'eimemory-code-validation-post-resume@fixture.service',
)


def discover(units):
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('bash is required for the deployment discovery script')
    env = dict(os.environ)
    env.pop('EIMEMORY_RUNTIME_TRANSITION_JOB', None)
    command = '''systemctl() { printf 'loaded\\n'; }; export -f systemctl
bash "$1" "$2"'''
    result = subprocess.run(
        [bash, '-c', command, 'bash',
         (ROOT / 'deploy/discover_python_runtime_units.sh').as_posix(),
         units.as_posix()],
        env=env, capture_output=True, text=True, timeout=20, check=True,
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize('unit', CONTROL_UNITS)
def test_controller_units_with_installation_guards_are_not_product_runtime(tmp_path, unit):
    units = tmp_path / 'systemd'
    dropins = units / (unit + '.d')
    dropins.mkdir(parents=True)
    (units / unit).write_text('[Service]\nExecStart=/usr/bin/python3 /control/entry.py\n')
    (dropins / '05-eimemory-installation-guard.conf').write_text(
        '[Unit]\nConditionPathExists=!/control/controller-installation.json\n'
    )
    assert unit not in discover(units)


@pytest.mark.parametrize('unit', CONTROL_UNITS)
def test_controller_identity_pollution_and_current_paths_do_not_reclassify_units(tmp_path, unit):
    units = tmp_path / 'systemd'
    dropins = units / (unit + '.d')
    dropins.mkdir(parents=True)
    (units / unit).write_text('[Service]\nExecStart=/opt/eimemory/current/control.py\n')
    (dropins / 'zzzz-eimemory-python-runtime.conf').write_text(
        '[Service]\nEnvironment=EIMEMORY_RUNTIME_COMMIT=' + 'a' * 40 + '\n'
    )
    assert unit not in discover(units)


def test_product_gateways_and_maintenance_remain_discovered(tmp_path):
    units = tmp_path / 'systemd'
    units.mkdir()
    (units / 'hermes-gateway-research.service').write_text(
        '[Service]\nExecStart=/native/hermes\n'
    )
    (units / 'eimemory-local-maintenance.service').write_text(
        '[Service]\nExecStart=/opt/eimemory/current/.venv/bin/python maintenance.py\n'
    )
    vendor = units / 'vendor-worker.service.d'
    vendor.mkdir()
    (vendor / 'zzzz-eimemory-python-runtime.conf').write_text('[Service]\n')
    discovered = discover(units)
    assert {
        'eimemory-rpc.service', 'hermes-gateway.service',
        'hermes-gateway-research.service', 'eimemory-backup.service',
        'eimemory-code-implementation-refresh.service',
        'eimemory-local-maintenance.service', 'vendor-worker.service',
    } <= set(discovered)
    assert len(discovered) == len(set(discovered))
