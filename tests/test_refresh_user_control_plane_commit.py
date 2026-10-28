from deploy import refresh_user_control_plane_commit as refresh_module
from pathlib import Path

from deploy.refresh_user_control_plane_commit import RefreshError, refresh_commit
import pytest


SAMPLE = """
[Service]
Environment=EIMEMORY_RUNTIME_COMMIT=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
Environment=PYTHONPYCACHEPREFIX=/var/lib/eimemory/.pycache/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
ExecStartPre=/opt/eimemory/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/.venv/bin/python -c true /opt/eimemory/releases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
PrivateUsers=yes
EnvironmentFile=/etc/eimemory/rpc.env

"""


def test_refresh_replaces_only_exact_commit_tokens():
    rendered = refresh_commit(SAMPLE.encode(), "b" * 40).decode()
    assert rendered.count("b" * 40) == 4
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" not in rendered
    assert "PrivateUsers=yes" in rendered
    assert "EnvironmentFile=/etc/eimemory/rpc.env" in rendered


def test_refresh_rejects_missing_or_customized_commit_assignment():
    with pytest.raises(RefreshError):
        refresh_commit(b"[Service]\nPrivateUsers=yes\n", "b" * 40)
    customized = SAMPLE.replace(
        "Environment=EIMEMORY_RUNTIME_COMMIT=",
        "Environment=EIMEMORY_RUNTIME_COMMIT=custom-",
    )
    with pytest.raises(RefreshError):
        refresh_commit(customized.encode(), "b" * 40)


def test_refresh_is_idempotent_for_same_commit(tmp_path: Path):
    target = tmp_path / "zzzzzz-eimemory-user-control-plane.conf"
    target.write_bytes(SAMPLE.encode())
    before = target.read_bytes()
    from deploy.refresh_user_control_plane_commit import write_refresh
    write_refresh(target, "a" * 40)
    assert target.read_bytes() == before


def test_check_only_validates_without_creating_or_replacing_files(tmp_path, monkeypatch):
    import os
    from deploy.refresh_user_control_plane_commit import write_refresh

    target = tmp_path / 'policy'
    target.write_bytes(SAMPLE.encode())
    before = target.stat()
    original_open = os.open

    def no_creating_open(path, flags, *args, **kwargs):
        assert not flags & os.O_CREAT, 'preflight must never stage a replacement'
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(refresh_module.os, 'open', no_creating_open)
    write_refresh(target, 'b' * 40, check_only=True)

    after = target.stat()
    assert target.read_bytes() == SAMPLE.encode()
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (
        before.st_ino, before.st_mtime_ns, before.st_mode,
    )
    assert list(tmp_path.iterdir()) == [target]


def test_check_only_cli_preserves_the_current_commit(tmp_path):
    from deploy.refresh_user_control_plane_commit import main

    target = tmp_path / 'policy'
    target.write_bytes(SAMPLE.encode())
    before = target.stat()
    assert main(['--target', str(target), '--commit', 'b' * 40, '--check-only']) == 0
    assert target.read_bytes() == SAMPLE.encode()
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('damage', ['missing_assignment', 'writable', 'symlink'])
def test_check_only_still_rejects_invalid_or_untrusted_policy(tmp_path, damage):
    from deploy.refresh_user_control_plane_commit import write_refresh

    target = tmp_path / 'policy'
    target.write_bytes(SAMPLE.encode())
    if damage == 'missing_assignment':
        target.write_bytes(b'[Service]\nPrivateUsers=yes\n')
    elif damage == 'writable':
        target.chmod(0o666)
    else:
        original = tmp_path / 'original'
        target.rename(original)
        target.symlink_to(original)
    before = target.read_bytes()
    with pytest.raises((RefreshError, OSError)):
        write_refresh(target, 'b' * 40, check_only=True)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob('.eimemory-control-plane-*'))


def run_refresh(tmp_path, unit, mode='user', metadata=None):
    import subprocess, sys
    source = Path('deploy/install_immutable_release.sh').read_text()
    body = source.split('_refresh_user_control_plane_commit() {', 1)[1].split('\n}\n', 1)[0]
    repo = Path.cwd()
    script = '_refresh_user_control_plane_commit() {' + body + '\n}\n' + '\n_refresh_user_control_plane_commit "$UNIT" "$SHA" "$METADATA"\n'
    import os
    env = dict(os.environ, USER_SYSTEMD_DIR=str(tmp_path), PYTHON_BIN=sys.executable,
               CONTROL_PYCACHE_PREFIX=str(tmp_path), REPO_DIR=str(repo), RELEASE_DIR=str(repo),
               EIMEMORY_CONTROL_PLANE=mode, UNIT=unit, SHA='b'*40,
               METADATA=str(metadata or repo))
    return subprocess.run(['bash', '-eu', '-c', script], env=env, capture_output=True)


def run_preflight(tmp_path, damage=None):
    import os
    import subprocess
    import sys

    repo = Path.cwd()
    unit_dir = tmp_path / 'units'
    unit_dir.mkdir()
    env = dict(os.environ, USER_SYSTEMD_DIR=str(unit_dir), PYTHON_BIN=sys.executable,
               CONTROL_PYCACHE_PREFIX=str(tmp_path / 'cache'), REPO_DIR=str(repo),
               RELEASE_DIR=str(repo), EIMEMORY_CONTROL_PLANE='user',
               USER_SYSTEMD_ENABLE_SERVICE='1', COMMIT='b' * 40)
    env.pop('EIMEMORY_RUNTIME_TRANSITION_JOB', None)
    discovery = subprocess.run(
        ['bash', str(repo / 'deploy/discover_python_runtime_units.sh'), str(unit_dir)],
        env=env, check=True, capture_output=True, text=True,
    )
    for unit in discovery.stdout.splitlines():
        # Existing unit files avoid any dependency on a live systemd manager.
        (unit_dir / unit).write_text('[Service]\nExecStart=/usr/bin/true\n')
        directory = unit_dir / (unit + '.d')
        directory.mkdir()
        (directory / 'zzzzzz-eimemory-user-control-plane.conf').write_bytes(SAMPLE.encode())
    target = unit_dir / 'eimemory-rpc.service.d/zzzzzz-eimemory-user-control-plane.conf'
    if damage == 'missing':
        target.unlink()
    elif damage == 'malformed':
        target.write_bytes(b'[Service]\nPrivateUsers=yes\n')
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
              for path in unit_dir.rglob('*.conf')}

    source = (repo / 'deploy/install_immutable_release.sh').read_text()
    functions = []
    for name in ('_refresh_user_control_plane_commit', '_preflight_user_control_plane_commits'):
        body = source.split(name + '() {', 1)[1].split('\n}\n', 1)[0]
        functions.append(name + '() {' + body + '\n}\n')
    marker = tmp_path / 'service-actions'
    env['ACTION_MARKER'] = str(marker)
    script = '''
_runtime_manager_enabled() { return 1; }
_run_as_service_user() { "$@"; }
_is_unselected_openclaw_unit() { return 1; }
systemctl() { echo systemctl >> "$ACTION_MARKER"; return 99; }
_prepare_storage_for_release() { echo storage >> "$ACTION_MARKER"; }
_stop_storage_writers() { echo stop >> "$ACTION_MARKER"; }
'''
    script += '\n'.join(functions)
    script += '\n_preflight_user_control_plane_commits\n_prepare_storage_for_release\n_stop_storage_writers\n'
    result = subprocess.run(['bash', '-eu', '-c', script], env=env, capture_output=True)
    return result, marker, before


def test_preflight_checks_discovered_units_without_refreshing_identity(tmp_path):
    result, marker, before = run_preflight(tmp_path)
    assert result.returncode == 0, result.stderr
    assert b'user_control_plane_commit_preflight=passed' in result.stdout
    assert marker.read_text().splitlines() == ['storage', 'stop']
    assert before
    for path, (payload, modified) in before.items():
        assert path.read_bytes() == payload
        assert path.stat().st_mtime_ns == modified
    assert not list(tmp_path.rglob('.eimemory-control-plane-*'))


@pytest.mark.parametrize('damage', ['missing', 'malformed'])
def test_preflight_failure_leaves_services_and_storage_untouched(tmp_path, damage):
    result, marker, before = run_preflight(tmp_path, damage)
    assert result.returncode == 2, result.stderr
    assert not marker.exists()
    for path, (payload, modified) in before.items():
        assert path.read_bytes() == payload
        assert path.stat().st_mtime_ns == modified
    assert not list(tmp_path.rglob('.eimemory-control-plane-*'))


def test_installer_runs_identity_preflight_before_live_storage_preparation():
    source = Path('deploy/install_immutable_release.sh').read_text()
    preflight = source.index('\n_preflight_user_control_plane_commits\n')
    observe = source.index('\n_observe_pre_switch_l5\n')
    storage = source.index('\n_prepare_storage_for_release\n')
    assert preflight < observe < storage


def test_unmanaged_unit_without_control_plane_is_optional(tmp_path):
    assert run_refresh(tmp_path, 'hongguang-voice-shim.service').returncode == 0


def test_required_unit_without_control_plane_fails(tmp_path):
    assert run_refresh(tmp_path, 'eimemory-rpc.service').returncode == 2


def test_legacy_install_without_control_plane(tmp_path):
    assert run_refresh(tmp_path, 'eimemory-rpc.service', mode='').returncode == 0


def test_rollback_uses_controller_helper(tmp_path):
    directory = tmp_path / 'hermes-gateway.service.d'
    directory.mkdir()
    target = directory / 'zzzzzz-eimemory-user-control-plane.conf'
    target.write_text(SAMPLE)
    old = tmp_path / 'old-release'
    old.mkdir()
    result = run_refresh(tmp_path, 'hermes-gateway.service', metadata=old)
    assert result.returncode == 0, result.stderr
    assert 'b'*40 in target.read_text()


def test_refresh_rejects_parent_symlink(tmp_path):
    from deploy.refresh_user_control_plane_commit import write_refresh
    real = tmp_path / 'real'
    real.mkdir()
    (real / 'policy').write_text(SAMPLE)
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises((RefreshError, OSError)):
        write_refresh(link / 'policy', 'b'*40)


def test_refresh_rejects_writable_policy(tmp_path):
    from deploy.refresh_user_control_plane_commit import write_refresh
    target = tmp_path / 'policy'
    target.write_text(SAMPLE)
    target.chmod(0o666)
    with pytest.raises(RefreshError):
        write_refresh(target, 'b'*40)


@pytest.mark.parametrize('unit', ['hermes-gateway.service', 'openclaw-gateway.service', 'hongguang-gateway.service', 'hermes-gateway-work.service'])
def test_gateway_final_identity_is_required_and_refreshed(tmp_path, unit):
    from deploy.runtime_manager_user_policy import render_gateway_integration
    assert run_refresh(tmp_path, unit).returncode == 2
    directory = tmp_path / (unit + '.d')
    directory.mkdir()
    target = directory / 'zzzzzz-eimemory-user-control-plane.conf'
    original = render_gateway_integration(unit, 'a'*40)
    target.write_bytes(original)
    assert run_refresh(tmp_path, unit).returncode == 0
    assert target.read_bytes() == render_gateway_integration(unit, 'b'*40)


def test_refresh_rejects_concurrent_replacement(tmp_path, monkeypatch):
    import os
    from deploy.refresh_user_control_plane_commit import write_refresh
    target = tmp_path / 'policy'
    target.write_text(SAMPLE)
    original_fsync = os.fsync
    replacement = b'concurrent editor\n'
    def racing_fsync(fd):
        original_fsync(fd)
        other = tmp_path / 'other'
        other.write_bytes(replacement)
        other.replace(target)
    monkeypatch.setattr(refresh_module.os, 'fsync', racing_fsync)
    with pytest.raises(RefreshError, match='changed'):
        write_refresh(target, 'b'*40)
    assert target.read_bytes() == replacement
    assert not list(tmp_path.glob('.eimemory-control-plane-*'))


@pytest.mark.parametrize('kind', ['symlink', 'hardlink'])
def test_refresh_rejects_linked_target(tmp_path, kind):
    from deploy.refresh_user_control_plane_commit import write_refresh
    original = tmp_path / 'original'
    original.write_text(SAMPLE)
    target = tmp_path / 'policy'
    if kind == 'symlink':
        target.symlink_to(original)
    else:
        target.hardlink_to(original)
    with pytest.raises((RefreshError, OSError)):
        write_refresh(target, 'b'*40)
    assert original.read_text() == SAMPLE
