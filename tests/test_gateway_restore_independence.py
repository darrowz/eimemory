"""Gateway-restore independence from RPC/OpenClaw readiness.

Property under contract: an eimemory rollback or background-writer restart
must still restore the Hermes messaging gateway even when the RPC release
health check or OpenClaw readiness fails, while background timers/monitors
and workers stay fail-closed (never started on a failed path).
Behavioral tests only; no real systemd or production IO.
"""
from pathlib import Path
import shlex
import subprocess

import pytest


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


@pytest.mark.parametrize('failure', ['rpc_health', 'gateway_ready'])
def test_core_restart_still_restores_hermes_when_upstream_unready(tmp_path, failure):
    release = tmp_path / ('a' * 40)
    release.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(release)
    setup = f'''
set -u
USER_SYSTEMD_ENABLE_SERVICE=1
CURRENT_LINK={shlex.quote(str(current))}
FAILURE={failure!r}
_pause_release_closure_reconcile() {{ :; }}
_user_systemctl() {{ printf 'svc:%s\\n' "$*"; }}
_verify_release_health() {{ printf 'rpc-attempt\\n'; [ "$FAILURE" != rpc_health ]; }}
_openclaw_is_enabled() {{ return 0; }}
_wait_openclaw_gateway_ready() {{ printf 'gateway-attempt\\n'; [ "$FAILURE" != gateway_ready ]; }}
_restart_hermes_gateway() {{ printf 'hermes-attempt\\n'; return 0; }}
'''
    result = _run_bash(setup + function('_restart_current_services') + '''
if _restart_current_services; then echo result:0; else echo result:failed; fi
''', tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == 'result:failed'
    assert 'hermes-attempt' in lines
    assert lines.index('hermes-attempt') > lines.index('rpc-attempt')
    assert not any(line.startswith('start ') and '.timer' in line for line in lines)


def test_background_writer_restart_restores_hermes_despite_rpc_health_failure(tmp_path):
    release = tmp_path / ('a' * 40)
    release.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(release)
    setup = f'''
set -u
STORAGE_WRITERS_STOPPED=1
USER_SYSTEMD_ENABLE_SERVICE=1
CURRENT_LINK={shlex.quote(str(current))}
ACTIVE_STORAGE_WRITER_UNITS=(eimemory-rpc.service hermes-gateway.service)
_user_systemctl() {{ printf 'start:%s\\n' "$2"; }}
_verify_release_health() {{ return 9; }}
_resume_release_closure_reconcile() {{ return 0; }}
'''
    result = _run_bash(setup + function('_restart_storage_writers') + '''
if _restart_storage_writers; then rc=0; else rc=$?; fi
printf 'result:%s stopped:%s\\n' "$rc" "$STORAGE_WRITERS_STOPPED"
''', tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[-1] == 'result:2 stopped:1'
    assert 'start:eimemory-rpc.service' in lines
    assert 'start:hermes-gateway.service' in lines
    assert lines.index('start:eimemory-rpc.service') < lines.index('start:hermes-gateway.service')
