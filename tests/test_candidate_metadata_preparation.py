"""Candidate preparation exercises real renderers without changing live state."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from deploy import install_hermes_integration as hermes
from deploy import preflight_runtime_metadata as metadata


pytestmark = [pytest.mark.linux_deployment,
              pytest.mark.skipif(os.name != 'posix', reason='POSIX deployment metadata')]
ROOT = Path(__file__).resolve().parents[1]
SHA = 'b' * 40


def tree(path):
    return {str(p.relative_to(path)): (
        p.lstat().st_mode, os.readlink(p) if p.is_symlink() else
        None if p.is_dir() else p.read_bytes()) for p in path.rglob('*')}


def render(units_root, units=None, commit=SHA):
    return metadata.preflight(
        controller=ROOT, release=ROOT, commit=commit,
        releases_root='/opt/eimemory/releases', systemd_root=units_root,
        receipt_env_file='/etc/eimemory/evidence-receipt.env',
        units=units or ['eimemory-rpc.service', 'hermes-gateway.service'],
        owner_uid=os.geteuid(),
    )


def test_private_render_preserves_serving_identity_and_current(tmp_path):
    units = tmp_path / 'systemd'
    existing = units / 'eimemory-rpc.service.d'
    existing.mkdir(parents=True)
    (existing / 'zzzz-eimemory-python-runtime.conf').write_text(
        '# Managed by eimemory immutable release installer\n[Service]\n'
        'Environment=EIMEMORY_RUNTIME_COMMIT=' + 'a' * 40 + '\n')
    prior = tmp_path / 'prior'
    prior.mkdir()
    current = tmp_path / 'current'
    current.symlink_to(prior, target_is_directory=True)
    before = tree(tmp_path)
    assert render(units) == {'ok': True, 'unit_count': 2, 'live_metadata_changed': False}
    assert tree(tmp_path) == before


@pytest.mark.parametrize('retired', [False, True])
def test_custom_live_dropin_is_rejected_during_preparation(tmp_path, retired):
    units = tmp_path / 'systemd'
    existing = units / 'eimemory-rpc.service.d'
    existing.mkdir(parents=True)
    name = '90-eimemory-python-runtime.conf' if retired else 'zzzz-eimemory-python-runtime.conf'
    (existing / name).write_text('[Service]\nEnvironment=LOCAL_POLICY=preserve\n')
    before = tree(tmp_path)
    with pytest.raises(RuntimeError, match='not managed'):
        render(units)
    assert tree(tmp_path) == before


def test_linked_live_dropin_is_rejected_without_following_or_writing(tmp_path):
    units = tmp_path / 'systemd'
    existing = units / 'eimemory-rpc.service.d'
    existing.mkdir(parents=True)
    external = tmp_path / 'external'
    external.write_text('local policy')
    (existing / 'zzzz-eimemory-python-runtime.conf').symlink_to(external)
    before = tree(tmp_path)
    with pytest.raises(RuntimeError, match='symlink'):
        render(units)
    assert tree(tmp_path) == before


def test_invalid_candidate_render_fails_before_live_publication(tmp_path):
    units = tmp_path / 'systemd'
    units.mkdir()
    with pytest.raises(RuntimeError, match='full 40-character'):
        render(units, commit='invalid')
    assert list(units.iterdir()) == []


def test_preparation_does_not_create_a_missing_live_systemd_directory(tmp_path):
    units = tmp_path / 'systemd'
    assert render(units)['ok'] is True
    assert not units.exists()


@pytest.fixture
def integration(tmp_path, monkeypatch):
    release = tmp_path / 'release'
    release.mkdir()
    (release / 'pyproject.toml').write_text('[project]\nversion="1.2.3"\n')
    for source in hermes.PLUGIN_LAYOUT.values():
        plugin = release / source
        plugin.mkdir(parents=True)
        (plugin / '__init__.py').write_text('# plugin\n')
        (plugin / 'plugin.yaml').write_text('{"version":"1.2.3"}')
    home = tmp_path / 'hermes'
    home.mkdir()
    (home / 'config.yaml').write_text('{"memory":{"provider":"prior"},"plugins":{}}')
    monkeypatch.setattr(hermes, '_yaml_module', lambda: SimpleNamespace(
        safe_load=json.loads,
        safe_dump=lambda value, stream, **kw: json.dump(value, stream)))
    monkeypatch.setattr(hermes, 'provider_identity', lambda _: {'binding_id': 'fixture'})
    return release, home


def test_hermes_check_only_validates_without_creating_links_or_rewriting_config(integration):
    release, home = integration
    before = tree(home)
    result = hermes.install_hermes_integration(
        release_root=release, hermes_home=home, check_only=True)
    assert result['ok'] is True and result['check_only'] is True
    assert result['memory_provider'] == 'eimemory'
    assert tree(home) == before


@pytest.mark.parametrize('failure', ['version', 'config', 'backup'])
def test_hermes_preparation_rejects_known_failure_without_partial_publication(integration, failure):
    release, home = integration
    if failure == 'version':
        (release / hermes.PLUGIN_LAYOUT['eimemory_hook'] / 'plugin.yaml').write_text('{"version":"wrong"}')
    elif failure == 'config':
        (home / 'config.yaml').write_text('{"memory":false}')
    else:
        (home / 'plugins' / 'eimemory').mkdir(parents=True)
        (home / '.eimemory-plugin-backups' / 'eimemory.pre-managed').mkdir(parents=True)
    before = tree(home)
    with pytest.raises(RuntimeError):
        hermes.install_hermes_integration(release_root=release, hermes_home=home, check_only=True)
    assert tree(home) == before


def test_live_application_rechecks_config_changed_after_preparation(integration):
    release, home = integration
    hermes.install_hermes_integration(release_root=release, hermes_home=home, check_only=True)
    (home / 'config.yaml').write_text('{"plugins":false}')
    before = tree(home)
    with pytest.raises(RuntimeError, match='plugins config'):
        hermes.install_hermes_integration(release_root=release, hermes_home=home)
    assert tree(home) == before


@pytest.mark.parametrize('failed_helper', [
    'preflight_runtime_metadata.py', 'ensure_hermes_sync_snapshot.py',
    'ensure_hermes_profile_boundary.py', 'install_hermes_integration.py',
])
def test_preparation_failure_stops_before_quiescing_serving_services(tmp_path, failed_helper):
    bash = shutil.which('bash')
    if bash is None:
        pytest.skip('bash required')
    source = (ROOT / 'deploy/install_immutable_release.sh').read_text()
    body = source.split('_preflight_candidate_runtime_metadata() {', 1)[1].split('\n}\n', 1)[0]
    assert source.index('\n_preflight_candidate_runtime_metadata\n') < source.index('\n_prepare_storage_for_release\n')
    events = tmp_path / 'events'
    script = r'''set -euo pipefail
_runtime_manager_enabled() { return 1; }
systemctl() { return 0; }
_is_unselected_openclaw_unit() { return 1; }
_hermes_is_installed() { return 0; }
_run_as_service_user() {
  printf '%s\n' "$*" >> "$EVENTS"
  if [[ "$1" == bash ]]; then printf 'eimemory-rpc.service\n'; return 0; fi
  [[ "$*" != *"$FAIL_HELPER"* ]] || return 29
}
USER_SYSTEMD_ENABLE_SERVICE=1
USER_SYSTEMD_DIR=/fixture/systemd
PYTHON_BIN=/fixture/python
HERMES_PYTHON=/fixture/hermes-python
CONTROL_PYCACHE_PREFIX=/fixture/cache
RELEASE_DIR=/fixture/release
COMMIT=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
INSTALL_ROOT=/opt/eimemory
EVIDENCE_RECEIPT_ENV_FILE=/fixture/evidence.env
SERVICE_USER="$(id -un)"
SERVICE_HOME=/fixture/home
HERMES_HOME_DIR=/fixture/hermes
_preflight_candidate_runtime_metadata() {''' + body + r'''
}
_preflight_candidate_runtime_metadata
printf 'writers_stopped\n' >> "$EVENTS"
'''
    result = subprocess.run([bash, '-c', script], env={**os.environ,
        'REPO_DIR': str(ROOT), 'EVENTS': str(events), 'FAIL_HELPER': failed_helper},
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 29, result.stderr
    recorded = events.read_text()
    assert 'writers_stopped' not in recorded
    if failed_helper == 'install_hermes_integration.py':
        assert '--check-only' in recorded
    assert '--apply' not in recorded
