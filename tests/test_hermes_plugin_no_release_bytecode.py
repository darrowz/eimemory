"""Our immutable artifact does not change the external host's cache policy."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

REPO = Path(__file__).resolve().parents[1]
POSIX_OWNER = pytest.mark.skipif(
    os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() == 0,
    reason='requires the non-root POSIX artifact owner',
)


def _load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _release(tmp_path):
    repo = tmp_path / 'repository'
    repo.mkdir()
    plugin = repo / 'integrations/hermes/eimemory'
    plugin.mkdir(parents=True)
    shutil.copy(REPO / 'integrations/hermes/eimemory/release_path.py', plugin / 'release_path.py')
    core = repo / 'eimemory/adapters/hermes'
    core.mkdir(parents=True)
    for package in (repo / 'eimemory', repo / 'eimemory/adapters', core):
        (package / '__init__.py').write_text('')
    (core / 'provider_core.py').write_text('VALUE = 1\n')
    for args in (('init', '-q'), ('add', '.'),
                 ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'source')):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    releases = tmp_path / 'releases'
    release = releases / commit
    release.mkdir(parents=True)
    archive = tmp_path / 'source.tar'
    subprocess.run(['git', '-C', str(repo), 'archive', '--format=tar', '--output=' + str(archive), commit], check=True)
    shutil.unpack_archive(archive, release)
    venv = release / '.venv/cache'
    venv.mkdir(parents=True)
    (venv / 'retained').write_bytes(b'dependency cache')
    return dict(repo_root=repo, commit=commit, release_dir=release, releases_root=releases)


@POSIX_OWNER
def test_release_import_preserves_host_bytecode_and_native_module_cache(tmp_path):
    arguments = _release(tmp_path)
    release = arguments['release_dir']
    cleaner = _load(REPO / 'deploy/clean_release_bytecode.py', '_source_seal_test')
    venv_mode = (release / '.venv').stat().st_mode
    cleaner.seal_release_source_permissions(**arguments)
    cleaner.seal_release_source_permissions(**arguments, verify_only=True)
    native = tmp_path / 'native-app'
    native.mkdir()
    (native / 'native_module.py').write_text('VALUE = 2\n')
    helper = release / 'integrations/hermes/eimemory/release_path.py'
    code = (
        'import importlib.util,sys; from pathlib import Path\n'
        'assert sys.dont_write_bytecode is False\n'
        f'spec=importlib.util.spec_from_file_location("rp", {str(helper)!r})\n'
        'm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\n'
        f'm.ensure_release_on_path(Path({str(helper)!r}))\n'
        'import eimemory.adapters.hermes.provider_core as p\n'
        f'sys.path.insert(0, {str(native)!r})\n'
        'import native_module\n'
        'assert p.VALUE == 1 and native_module.VALUE == 2\n'
        'assert sys.dont_write_bytecode is False\n'
    )
    env = {k: v for k, v in os.environ.items() if k not in {'PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX', 'PYTHONPATH'}}
    subprocess.run([sys.executable, '-c', code], cwd=tmp_path, env=env, check=True, timeout=60)
    assert not list((release / 'eimemory').rglob('__pycache__'))
    assert not list((release / 'integrations').rglob('__pycache__'))
    assert list(native.rglob('native_module.*.pyc'))
    assert (release / '.venv').stat().st_mode == venv_mode
    assert (release / '.venv/cache/retained').read_bytes() == b'dependency cache'
    cleaner.validate_release_source(**arguments)
    backup = release.with_name('.eimemory-backup-' + arguments['commit'] + '-test')
    release.rename(backup)
    cleaner.remove_stage_directory(stage_dir=backup, releases_root=arguments['releases_root'])
    assert not backup.exists()


@POSIX_OWNER
def test_source_seal_rejects_drift_and_an_incomplete_seal(tmp_path):
    arguments = _release(tmp_path)
    cleaner = _load(REPO / 'deploy/clean_release_bytecode.py', '_source_seal_test')
    release = arguments['release_dir']
    with pytest.raises(cleaner.CleanupError, match='seal is incomplete'):
        cleaner.seal_release_source_permissions(**arguments, verify_only=True)
    original_mode = stat.S_IMODE(release.stat().st_mode)
    source = release / 'eimemory/adapters/hermes/provider_core.py'
    source.write_text('VALUE = 3\n')
    with pytest.raises(cleaner.CleanupError):
        cleaner.seal_release_source_permissions(**arguments)
    assert stat.S_IMODE(release.stat().st_mode) == original_mode
    assert source.read_text() == 'VALUE = 3\n'


@POSIX_OWNER
def test_prepared_source_seal_is_idempotent_inside_a_readonly_mount(tmp_path):
    if not shutil.which('bwrap'):
        pytest.skip('bwrap is unavailable')
    arguments = _release(tmp_path)
    cleaner_path = REPO / 'deploy/clean_release_bytecode.py'
    cleaner = _load(cleaner_path, '_readonly_source_seal')
    cleaner.seal_release_source_permissions(**arguments)
    command = [str(sys.executable), '-I', '-B', str(cleaner_path), '--seal-source',
        '--release-dir', str(arguments['release_dir']),
        '--releases-root', str(arguments['releases_root']),
        '--repo-root', str(arguments['repo_root']), '--commit', arguments['commit']]
    result = subprocess.run(['bwrap', '--unshare-user', '--uid', str(os.geteuid()),
        '--gid', str(os.getegid()), '--ro-bind', '/', '/', '--dev-bind', '/dev', '/dev',
        '--proc', '/proc', '--', *command], text=True, capture_output=True, timeout=60)
    if result.returncode and any(text in result.stderr for text in (
            'No permissions to create new namespace', 'Operation not permitted',
            'Creating new namespace failed')):
        pytest.skip('the host does not permit nested read-only mount probes')
    assert result.returncode == 0, result.stderr
    cleaner.seal_release_source_permissions(**arguments, verify_only=True)


def test_path_helper_preserves_an_existing_host_cache_policy(monkeypatch):
    module = _load(REPO / 'integrations/hermes/eimemory/release_path.py', '_release_path_policy')
    monkeypatch.setattr(sys, 'path', list(sys.path))
    for value in (False, True):
        monkeypatch.setattr(sys, 'dont_write_bytecode', value)
        module.ensure_release_on_path(REPO / 'integrations/hermes/eimemory/__init__.py')
        assert sys.dont_write_bytecode is value
