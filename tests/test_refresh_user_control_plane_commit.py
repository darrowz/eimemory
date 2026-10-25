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
    monkeypatch.setattr(os, 'fsync', racing_fsync)
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
