"""Exercise bytecode suppression independently of release identity checks."""
import os
from pathlib import Path
import shutil
import subprocess
import sys


def test_governance_exec_suppresses_child_and_grandchild_bytecode(tmp_path):
    release = tmp_path / 'release'
    (release / 'deploy').mkdir(parents=True)
    (release / 'bytecode_probe.py').write_text('VALUE = 1\n')
    wrapper = release / 'deploy' / 'run_with_governance_env.py'
    shutil.copyfile(Path(__file__).parents[1] / 'deploy' / wrapper.name, wrapper)
    env = dict(os.environ, PYTHONPATH=str(release))
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPYCACHEPREFIX', None)
    code = (
        'import bytecode_probe, subprocess, sys; '
        'subprocess.run([sys.executable, "-c", "import bytecode_probe"], check=True)'
    )
    result = subprocess.run(
        [sys.executable, '-I', '-B', str(wrapper), '--optional', '--env-file',
         str(tmp_path / 'missing.env'), '--', sys.executable, '-c', code],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not list(release.rglob('*.pyc'))
