"""A release gate must not import a checkout through the controller environment."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


@pytest.mark.parametrize('invalid_home', [False, True])
def test_governance_exec_pins_release_imports(tmp_path, invalid_home):
    release = tmp_path / 'immutable-release'
    checkout = tmp_path / 'checkout'
    (release / 'deploy').mkdir(parents=True)
    for root, marker in [(release, 'release'), (checkout, 'checkout')]:
        (root / 'eimemory').mkdir(parents=True)
        (root / 'eimemory' / '__init__.py').write_text(f'ORIGIN = {marker!r}\n')
    wrapper = release / 'deploy' / 'run_with_governance_env.py'
    shutil.copyfile(Path(__file__).parents[1] / 'deploy' / wrapper.name, wrapper)
    env = dict(os.environ, PYTHONPATH=str(checkout), PYTHONDONTWRITEBYTECODE='0')
    if invalid_home:
        env['PYTHONHOME'] = str(tmp_path / 'invalid-home')
    code = 'import eimemory,json,sys;print(json.dumps([eimemory.ORIGIN,sys.dont_write_bytecode]))'
    p = subprocess.run([sys.executable, '-I', '-B', str(wrapper), '--env-file',
                        str(tmp_path / 'absent.env'), '--optional', '--',
                        sys.executable, '-c', code], cwd=checkout, env=env,
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == ['release', True]
    assert not list(release.rglob('*.pyc'))


def test_governance_launcher_has_explicit_release_impact():
    from eimemory.governance.release_impact import _domains_for_change
    domains = _domains_for_change(Path(__file__).parents[1],
        path='deploy/run_with_governance_env.py', ancestor='HEAD', current='HEAD')
    assert domains == {'deployment.runtime', 'memory.governance', 'code.evolution'}
