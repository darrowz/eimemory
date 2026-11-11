"""Real systemd Environment= parsing; no manager calls or production reads."""
import ctypes
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from test_storage_writer_restart_order import function

ROOT = Path(__file__).resolve().parents[1]
KEY = 'EIMEMORY_CAPTURE_QUERY_SCOPES'
SCOPES = [{'tenant_id': 'fixture', 'agent_id': 'fixture-agent',
           'workspace_id': 'fixture::channel::hermes', 'user_id': 'fixture-user',
           'channel': 'hermes', 'source_id': 'hermes'}]


@pytest.fixture(scope='module')
def systemd_environment_parser():
    # Use the installed systemd parser itself, including Environment= word
    # splitting and C unescaping. Never load a service or connect to a bus.
    candidates = list(Path('/usr/lib').glob('*/systemd/libsystemd-core-*.so'))
    if not candidates:
        pytest.skip('installed libsystemd-core parser unavailable')
    core_path = candidates[0]
    shared_path = core_path.with_name(core_path.name.replace('core', 'shared'))
    shared = ctypes.CDLL(str(shared_path), mode=ctypes.RTLD_GLOBAL)
    core = ctypes.CDLL(str(core_path))
    parse = core.config_parse_environ
    parse.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p,
        ctypes.c_uint, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p]
    parse.restype = ctypes.c_int
    shared.strv_free.argtypes = [ctypes.POINTER(ctypes.c_char_p)]

    def environment(text):
        vector = ctypes.POINTER(ctypes.c_char_p)()
        try:
            for line in text.splitlines():
                if line.startswith('Environment='):
                    rc = parse(b'fixture.service', b'fixture.conf', 1, b'Service', 1,
                        b'Environment', 0, line.partition('=')[2].encode(), ctypes.byref(vector), None)
                    assert rc == 0
            result, i = {}, 0
            while vector and vector[i]:
                key, _, value = vector[i].decode().partition('=')
                result[key] = value
                i += 1
            return result
        finally:
            shared.strv_free(vector)
    return environment


def legacy_dropin(scopes=SCOPES):
    return ('# Existing exact operator authorization\n[Service]\n'
            'Environment=EIMEMORY_CAPTURE_ORIGINAL_QUERY=0\n'
            'Environment=UNRELATED=preserve\nEnvironment=' + KEY + '=' + json.dumps(scopes) + '\n')


def test_existing_generator_shape_loses_json_quotes(systemd_environment_parser):
    value = systemd_environment_parser(legacy_dropin())[KEY]
    assert '"' not in value
    with pytest.raises(json.JSONDecodeError):
        json.loads(value)


def install_metadata(tmp_path, enabled='0'):
    units = tmp_path / 'units'
    target = units / 'eimemory-rpc.service.d/95-release-scope-binding.conf'
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(legacy_dropin().replace('ORIGINAL_QUERY=0', 'ORIGINAL_QUERY=' + enabled))
    script = f'''
USER_SYSTEMD_ENABLE_SERVICE=1
USER_SYSTEMD_DIR={shlex.quote(str(units))}
PYTHON_BIN=/dev-project/eimemory/.venv/bin/python
CONTROL_PYCACHE_PREFIX={shlex.quote(str(tmp_path / 'pycache'))}
REPO_DIR={shlex.quote(str(ROOT))}
RELEASE_DIR="$REPO_DIR"
SERVICE_USER={shlex.quote(getpass.getuser())}
INSTALL_ROOT=/opt/eimemory
EVIDENCE_RECEIPT_ENV_FILE=/etc/eimemory/evidence-receipt.env
_runtime_manager_enabled() {{ return 1; }}
_run_as_service_user() {{ "$@"; }}
_install_as_service_user() {{ :; }}
_is_unselected_openclaw_unit() {{ return 1; }}
_refresh_user_control_plane_commit() {{ :; }}
_install_learning_runtime_policy() {{ :; }}
_install_code_implementation_owner_policy() {{ :; }}
_install_hermes_integration() {{ :; }}
_user_systemctl() {{ :; }}
'''
    result = subprocess.run(['bash', '-c', script + function('_install_current_runtime_metadata') +
        '_install_current_runtime_metadata "$REPO_DIR" ' + 'a' * 40 + ' "$REPO_DIR"'],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return target


@pytest.mark.parametrize('enabled', ['0', '1'])
def test_formal_metadata_install_preserves_exact_optin_through_systemd(tmp_path, systemd_environment_parser, monkeypatch, enabled):
    from eimemory.evaluation.query_input_vault import configured_capture_scopes
    target = install_metadata(tmp_path, enabled)
    env = systemd_environment_parser(target.read_text())
    assert env['EIMEMORY_CAPTURE_ORIGINAL_QUERY'] == enabled
    assert env['UNRELATED'] == 'preserve'
    monkeypatch.setenv(KEY, env[KEY])
    assert configured_capture_scopes() == SCOPES


@pytest.mark.parametrize('user', ['ordinary', '引号"与反斜杠\\', 'literal-%n-%%-$USER', 'with space'])
def test_renderer_roundtrips_systemd_special_characters_and_is_idempotent(systemd_environment_parser, user):
    from deploy.repair_capture_scope_dropin import render_capture_dropin
    scopes = [{**SCOPES[0], 'user_id': user}]
    source = legacy_dropin(scopes).encode()
    rendered = render_capture_dropin(source)
    assert json.loads(systemd_environment_parser(rendered.decode())[KEY]) == scopes
    assert render_capture_dropin(rendered) == rendered
    assert source.splitlines()[:4] == rendered.splitlines()[:4]


@pytest.mark.parametrize('damage', ['malformed', 'wildcard', 'duplicate', 'quoted_ambiguous', 'wrong_section'])
def test_invalid_existing_policy_is_not_rewritten(tmp_path, damage):
    from deploy.repair_capture_scope_dropin import repair_capture_dropin
    target = tmp_path / 'eimemory-rpc.service.d/95-release-scope-binding.conf'
    target.parent.mkdir()
    text = legacy_dropin()
    if damage == 'malformed':
        text = text.replace(json.dumps(SCOPES), '[{bad}]')
    elif damage == 'wildcard':
        text = text.replace('fixture-user', '*')
    elif damage == 'duplicate':
        text += text.splitlines()[-1] + '\n'
    elif damage == 'quoted_ambiguous':
        text = text.replace('Environment=' + KEY, 'Environment="OTHER=value" ' + KEY)
    else:
        text = text.replace('[Service]', '[Unit]')
    target.write_text(text)
    before = target.read_bytes()
    with pytest.raises(ValueError, match='capture_scope_dropin_invalid'):
        repair_capture_dropin(tmp_path)
    assert target.read_bytes() == before


@pytest.mark.parametrize('damage', ['symlink', 'parent_link', 'hardlink', 'writable', 'parent_writable'])
def test_untrusted_capture_dropin_is_rejected(tmp_path, damage):
    from deploy.repair_capture_scope_dropin import repair_capture_dropin
    target = tmp_path / 'eimemory-rpc.service.d/95-release-scope-binding.conf'
    target.parent.mkdir()
    original = tmp_path / 'original'
    original.write_text(legacy_dropin())
    if damage == 'symlink':
        target.symlink_to(original)
    elif damage == 'parent_link':
        target.parent.rmdir()
        target.parent.symlink_to(tmp_path / 'real-directory', target_is_directory=True)
        (tmp_path / 'real-directory').mkdir()
        target.write_text(legacy_dropin())
    elif damage == 'hardlink':
        os.link(original, target)
    else:
        target.write_text(legacy_dropin())
        (target if damage == 'writable' else target.parent).chmod(0o777)
    with pytest.raises((RuntimeError, OSError)):
        repair_capture_dropin(tmp_path)
    assert original.read_text() == legacy_dropin()


def test_capture_preflight_and_absence_never_create_authority(tmp_path):
    from deploy.repair_capture_scope_dropin import repair_capture_dropin
    assert repair_capture_dropin(tmp_path) == {'capture_scope_dropin': 'not_configured'}
    assert list(tmp_path.iterdir()) == []
    target = tmp_path / 'eimemory-rpc.service.d/95-release-scope-binding.conf'
    target.parent.mkdir()
    target.write_text(legacy_dropin())
    target.chmod(0o600)
    before = target.stat()
    assert repair_capture_dropin(tmp_path, check_only=True) == {'capture_scope_dropin': 'validated'}
    assert target.read_text() == legacy_dropin()
    assert target.stat().st_ino == before.st_ino
    repair_capture_dropin(tmp_path)
    after = target.stat()
    assert after.st_mode & 0o777 == 0o600
    repair_capture_dropin(tmp_path)
    assert target.stat().st_ino == after.st_ino


def test_existing_binding_without_capture_policy_stays_unconfigured(tmp_path):
    from deploy.repair_capture_scope_dropin import repair_capture_dropin
    target = tmp_path / 'eimemory-rpc.service.d/95-release-scope-binding.conf'
    target.parent.mkdir()
    target.write_bytes(b'[Service]\nEnvironment=EIMEMORY_CAPTURE_ORIGINAL_QUERY=0\n')
    before = target.stat()
    repair_capture_dropin(tmp_path)
    assert target.stat().st_ino == before.st_ino
    assert KEY not in target.read_text()


@pytest.mark.parametrize('phase', ['parent_metadata', 'after_stage', 'replace'])
def test_update_enoent_is_not_unconfigured_and_staging_is_cleaned(tmp_path, monkeypatch, phase):
    import errno
    from deploy.repair_capture_scope_dropin import repair_capture_dropin
    target = tmp_path / 'eimemory-rpc.service.d/95-release-scope-binding.conf'
    target.parent.mkdir()
    target.write_text(legacy_dropin())
    before = target.read_bytes()
    observed = []
    if phase == 'parent_metadata':
        original = os.fstat
        def missing_parent_metadata(fd):
            if not observed:
                observed.append(phase)
                raise FileNotFoundError(errno.ENOENT, 'fixture metadata unavailable')
            return original(fd)
        monkeypatch.setattr(os, 'fstat', missing_parent_metadata)
    elif phase == 'after_stage':
        original = os.fsync
        def remove_after_staging(fd):
            original(fd)
            if not observed:
                assert list(target.parent.glob('.eimemory-control-plane-*'))
                target.unlink()
                observed.append(phase)
        monkeypatch.setattr(os, 'fsync', remove_after_staging)
    else:
        def missing_during_replace(*args, **kwargs):
            observed.append(phase)
            raise FileNotFoundError(errno.ENOENT, 'fixture publication disappeared')
        monkeypatch.setattr(os, 'replace', missing_during_replace)
    with pytest.raises(FileNotFoundError):
        repair_capture_dropin(tmp_path)
    assert observed == [phase]
    assert not list(target.parent.glob('.eimemory-control-plane-*'))
    if phase in {'parent_metadata', 'replace'}:
        assert target.read_bytes() == before


@pytest.mark.parametrize('unchanged', [False, True])
@pytest.mark.parametrize('check_only', [False, True])
def test_capture_cli_fails_if_target_disappears_after_read(tmp_path, monkeypatch, capsys, check_only, unchanged):
    from deploy import repair_capture_scope_dropin as repair
    target = tmp_path / repair.DROPIN
    target.parent.mkdir()
    target.write_text(legacy_dropin())
    original = repair.render_capture_dropin
    if unchanged:
        target.write_bytes(original(target.read_bytes()))
    def remove_after_read(payload):
        rendered = original(payload)  # Run the real exact-policy validator.
        target.unlink()
        return rendered
    monkeypatch.setattr(repair, 'render_capture_dropin', remove_after_read)
    args = ['--systemd-root', str(tmp_path)] + (['--check-only'] if check_only else [])
    with pytest.raises(SystemExit) as exc:
        repair.main(args)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == 'capture_scope_dropin_refresh_failed\n'
    assert captured.out == ''
    assert not list(target.parent.glob('.eimemory-control-plane-*'))


def test_reviewed_manager_settings_preserve_the_rendered_exact_policy():
    from deploy.repair_capture_scope_dropin import render_capture_dropin
    from deploy.runtime_manager_settings import build
    scopes = [{**SCOPES[0], 'user_id': 'fixture "quoted" \\ %n'}]
    raw = render_capture_dropin(legacy_dropin(scopes).encode())
    unit = 'eimemory-rpc.service'
    snapshot = {'schema': 'eimemory.runtime_owner_user_files.v1', 'units': [unit],
        'files': {unit: {'kind': 'file', 'raw_hex': raw.hex(),
                         'sha256': sha256(raw).hexdigest(), 'mode': 0o644}}}
    settings = build(snapshot, [unit])
    env = settings['units'][unit]['environment']
    assert json.loads(env[KEY]) == scopes
    assert env['EIMEMORY_CAPTURE_ORIGINAL_QUERY'] == '0'
