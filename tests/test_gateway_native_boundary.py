"""Memory integration leaves native business gateway execution under its owner."""
from hashlib import sha256
import importlib.util
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.contracts import user_control_plane as control

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('gateway_policy_under_test', ROOT / 'deploy/runtime_manager_user_policy.py')
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)
COMMIT = 'a' * 40


@pytest.mark.parametrize('unit', ['hermes-gateway.service', 'hermes-gateway-hongtai.service',
    'hermes-gateway-hongxin.service', 'hermes-gateway-xiaomage.service',
    'openclaw-gateway.service', 'colleague-gateway.service', 'custom.team@one-gateway.service'])
def test_gateways_receive_only_memory_environment(unit):
    text = POLICY.render_gateway_integration(unit, COMMIT).decode()
    directives = [line for line in text.splitlines() if line and not line.startswith(('#', '['))]
    assert all(line.startswith(('Environment=EIMEMORY_', 'Environment="EIMEMORY_',
                                'EnvironmentFile=')) for line in directives)
    assert 'EIMEMORY_RUNTIME_COMMIT=' + COMMIT in text
    assert not any(word in text for word in ('ExecStart', 'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE',
        'UMask', 'NoNewPrivileges', 'PrivateUsers', 'PrivatePIDs', 'PrivateTmp', 'Protect',
        'Capability', 'InaccessiblePaths', 'ReadOnlyPaths', 'ReadWritePaths',
        'MemoryMax', 'CPUWeight', 'TasksMax', 'Restart=', 'Timeout', 'Requires=', 'After=', 'Wants='))
    if unit.startswith('hermes-gateway'):
        assert 'EIMEMORY_ATTESTATION_HOST_PROFILE=operator-separated-v1' in text
        assert ('EIMEMORY_HERMES_GATEWAY_PROCESS=1' in text)==(unit=='hermes-gateway.service')
        assert ('EIMEMORY_HERMES_CODE_IMPLEMENTATION_SOCKET=' in text)==(unit=='hermes-gateway.service')
    elif unit == 'openclaw-gateway.service':
        assert 'EIMEMORY_HOOK_COMMAND=' in text


@pytest.mark.parametrize('unit', ['openclaw-loop-watch.service', 'openclaw-loop-compact.service',
    'eimemory-rpc.service', '../foo-gateway.service', 'bad gateway.service',
    'x' * 65 + '-gateway.service', 'hermes-gateway-.service'])
def test_product_sidecars_and_invalid_units_are_not_gateways(unit):
    assert not POLICY.is_gateway_unit(unit)
    with pytest.raises(ValueError): POLICY.render_gateway_integration(unit, COMMIT)


def test_pure_gateway_helpers_import_without_an_installed_user_controller(monkeypatch):
    monkeypatch.setenv('EIMEMORY_CONTROL_PLANE', 'user')
    specification = importlib.util.spec_from_file_location('staged_gateway_policy', Path(POLICY.__file__))
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    assert b'Environment=EIMEMORY_RUNTIME_COMMIT=' in module.render_gateway_integration('hermes-gateway.service', COMMIT)
    assert 'user_control' not in vars(module)


def test_legacy_policy_reconstruction_is_exact_and_never_the_new_policy():
    account = SimpleNamespace(pw_name='operator', pw_gid=1001)
    old = b'[Service]\nUser=operator\nGroup=1001\nPrivateUsers=self\nReadOnlyPaths=/opt/eimemory/releases/@COMMIT@ /etc\n'
    actual = POLICY.legacy_gateway_policy(old, COMMIT, account, 'hermes-gateway.service', user_mode=False)
    assert actual == b'[Service]\nPrivateUsers=yes\nReadOnlyPaths=/opt/eimemory/releases /etc\n'
    assert sha256(actual).digest() != sha256(POLICY.render_gateway_integration('hermes-gateway.service', COMMIT)).digest()


def test_native_consumer_requires_exact_owned_unit_and_native_cgroup():
    contract = SimpleNamespace(PRODUCT_SERVICES=('eimemory-rpc.service',),
        is_dynamic_unit=lambda unit: unit == 'eimemory-job@' + 'a' * 64 + '.service',
        fixed_system_unit_cgroup=lambda group, unit: group == '/system.slice/' + unit)
    assert control.native_control_consumer(['0::/system.slice/eimemory-rpc.service'], contract)
    assert control.native_control_consumer(['0::/system.slice/eimemory-job@' + 'a' * 64 + '.service'], contract)
    assert control.native_control_consumer(['0::/system.slice/openclaw-loop-watch.service'], contract)
    for unit in ('hermes-gateway.service', 'hermes-gateway-hongtai.service',
                 'openclaw-gateway.service', 'custom-gateway.service', 'eimemory-custom.service',
                 'eimemory-runtime-manager.service'):
        assert not control.native_control_consumer(['0::/system.slice/' + unit], contract)
    with pytest.raises(ValueError, match='native_consumer_unverified'):
        control.native_control_consumer(['0::/untrusted.slice/eimemory-rpc.service'], contract)


@pytest.mark.skipif(os.name != 'posix', reason='native Unix directory ownership')
@pytest.mark.parametrize('unit,nnp,readonly,accepted', [
    ('hermes-gateway.service', '1', False, True),
    ('hermes-gateway-hongtai.service', '1', False, True),
    ('openclaw-gateway.service', '0', False, True),
    ('eimemory-custom.service', '1', False, True),
    ('eimemory-rpc.service', '1', False, False),
    ('eimemory-rpc.service', '1', True, True)])
def test_only_owned_native_consumers_require_readonly_mounts(tmp_path, monkeypatch,
        trusted_dataset_path_ancestors, unit, nnp, readonly, accepted):
    protected = tmp_path / 'private'; protected.mkdir(mode=0o700)
    monkeypatch.setattr(control, 'user_mode', lambda: True)
    import pwd
    native_account=pwd.getpwuid(os.geteuid())
    monkeypatch.setattr(control, 'account', lambda: native_account)
    monkeypatch.setattr(control, 'coordinates', lambda: dict(authority=protected,
        state=protected, code=protected, socket=protected/'bridge.sock'))
    original = Path.read_text
    def proc(path, *args, **kwargs):
        if str(path) == '/proc/self/cgroup':return '0::/system.slice/' + unit + '\n'
        if str(path) == '/proc/self/status':return 'NoNewPrivs: ' + nnp + '\n'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(control.Path, 'read_text', proc)
    from eimemory.contracts import runtime_manager as native_contract
    monkeypatch.setattr(native_contract, 'fixed_system_unit_cgroup', lambda group, candidate: group == '/system.slice/' + candidate)
    native = control.os
    monkeypatch.setattr(control, 'os', SimpleNamespace(**{**vars(native),
        'statvfs': lambda path: SimpleNamespace(f_flag=native.ST_RDONLY if readonly else 0)}))
    if accepted:control.trusted_directory(protected)
    else:
        with pytest.raises(ValueError, match='namespace_not_readonly'):control.trusted_directory(protected)


@pytest.mark.skipif(os.name != 'posix', reason='native Unix metadata publication')
def test_gateway_writer_preserves_native_workspace_modes_and_commands(tmp_path, monkeypatch):
    uid = os.geteuid()
    import pwd
    native = pwd.getpwuid(uid)
    home = tmp_path / 'operator'; systemd = home / '.config/systemd/user'
    systemd.mkdir(parents=True)
    unit = 'hermes-gateway-hongtai.service'; directory = systemd / (unit + '.d')
    directory.mkdir(); directory.chmod(0o775)
    native_fragment = systemd / unit
    native_fragment.write_bytes(b'[Service]\nExecStart=/native/hermes --profile hongtai gateway run\n')
    native_dropin = directory / '90-native.conf'; native_dropin.write_bytes(b'[Service]\nUMask=0002\n')
    authority = home / '.config/eimemory/control-plane'; authority.mkdir(parents=True)
    code = home / '.local/lib/eimemory/control-plane'; code.mkdir(parents=True)
    policy = code / 'runtime-policy.conf'; policy.write_bytes(b'[Service]\nPrivateUsers=yes\n'); policy.chmod(0o640)
    account = SimpleNamespace(pw_name=native.pw_name,pw_gid=native.pw_gid,pw_uid=uid,pw_dir=str(home))
    monkeypatch.setattr(pwd, 'getpwuid', lambda ident: account)
    monkeypatch.setattr(POLICY, 'USER_MODE', True); monkeypatch.setattr(POLICY, 'CONTROL_UID', uid)
    monkeypatch.setattr(POLICY, 'load_user_control', lambda: None)
    monkeypatch.setattr(POLICY, 'USER_COORDINATES', dict(authority=authority, code=code, registry=authority/'registry.json'), raising=False)
    monkeypatch.setattr(POLICY, 'read_root', lambda path: dict(runtime_uid=uid, runtime_user=native.pw_name,
        runtime_gid=native.pw_gid,runtime_commit='b'*40,units={unit:dict(manager='user',installed=True)}))
    monkeypatch.setattr(POLICY.os, 'getuid', lambda: uid or 1001)
    if uid == 0:
        pytest.skip('nonroot product publication')
    monkeypatch.setattr(POLICY.subprocess, 'run', lambda *args, **kwargs: None)
    original = {path:path.read_bytes() for path in (native_fragment, native_dropin)}
    POLICY.apply(COMMIT)
    assert directory.stat().st_mode & 0o777 == 0o775
    assert all(path.read_bytes() == raw for path,raw in original.items())
    integration = directory/'zzzzzz-eimemory-user-control-plane.conf'
    assert integration.read_bytes() == POLICY.render_gateway_integration(unit, COMMIT, authority=authority)
    assert all(line.startswith('EnvironmentFile=-') for line in integration.read_text().splitlines()
        if line.startswith('EnvironmentFile='))
    assert integration.stat().st_mode & 0o777 == 0o600
    fsync=POLICY.os.fsync;changed=False
    with monkeypatch.context() as race:
        def concurrent_edit(descriptor):
            nonlocal changed
            fsync(descriptor)
            if not changed:
                changed=True;integration.write_bytes(b'[Service]\nEnvironment=EDITOR=1\n')
        race.setattr(POLICY.os,'fsync',concurrent_edit)
        with pytest.raises(ValueError,match='integration_changed'):POLICY.apply(COMMIT)
    assert integration.read_bytes()==b'[Service]\nEnvironment=EDITOR=1\n'
    integration.write_bytes(b'[Service]\nEnvironment=CUSTOM=1\n')
    with pytest.raises(ValueError, match='customized_preserved'): POLICY.apply('c' * 40)
    assert integration.read_bytes() == b'[Service]\nEnvironment=CUSTOM=1\n'


@pytest.mark.skipif(os.name!='posix',reason='native reviewed helper authentication')
@pytest.mark.parametrize('fault',['valid','symlink','hardlink','writable'])
def test_standalone_consumer_authenticates_the_exact_adjacent_contract(tmp_path,monkeypatch,fault):
    module=tmp_path/'user_control_plane.py'
    source=tmp_path/'runtime_manager_contract.py'
    source.write_text("PRODUCT_SERVICES=('eimemory-rpc.service',)\n"
        "def is_dynamic_unit(unit):return False\n"
        "def fixed_system_unit_cgroup(group,unit):return group=='/system.slice/'+unit\n")
    source.chmod(0o640)
    if fault=='symlink':
        original=source.with_name('original.py');source.rename(original);source.symlink_to(original)
    elif fault=='hardlink':os.link(source,tmp_path/'alias.py')
    elif fault=='writable':source.chmod(0o660)
    monkeypatch.setattr(control,'__package__','')
    monkeypatch.setattr(control,'__file__',str(module))
    if fault=='valid':assert control.native_control_consumer(['0::/system.slice/eimemory-rpc.service'])
    else:
        with pytest.raises((ValueError,OSError)):
            control.native_control_consumer(['0::/system.slice/eimemory-rpc.service'])


@pytest.mark.skipif(os.name!='posix',reason='native source contract authentication')
def test_standalone_source_fallback_preserves_exact_native_classifier(tmp_path,monkeypatch,
        trusted_dataset_path_ancestors):
    source=tmp_path/'repo/eimemory/contracts';source.mkdir(parents=True)
    helper=source/'user_control_plane.py';helper.write_bytes(Path(control.__file__).read_bytes())
    contract=source/'runtime_manager.py'
    contract.write_text("PRODUCT_SERVICES=('eimemory-rpc.service',)\n"
        "def is_dynamic_unit(unit):return False\n"
        "def fixed_system_unit_cgroup(group,unit):return group=='/system.slice/'+unit\n")
    contract.chmod(0o644)
    monkeypatch.setattr(control,'__package__','')
    monkeypatch.setattr(control,'__file__',str(helper))
    assert control.native_control_consumer(['0::/system.slice/eimemory-rpc.service'])
    assert not control.native_control_consumer(['0::/system.slice/eimemory-unified-v26-shard-1.service'])
    contract.unlink()
    assert not control.native_control_consumer(['0::/system.slice/eimemory-runtime-manager.service'])
    with pytest.raises(OSError):control.native_control_consumer(['0::/system.slice/eimemory-rpc.service'])


@pytest.mark.skipif(os.name!='posix',reason='native readonly mount classification')
@pytest.mark.parametrize('readonly',[False,True])
def test_source_fallback_group_writable_ancestor_requires_actual_readonly_mount(tmp_path,monkeypatch,
        trusted_dataset_path_ancestors,readonly):
    source=tmp_path/'repo/eimemory/contracts';source.mkdir(parents=True)
    source.chmod(0o775)
    helper=source/'user_control_plane.py';helper.write_bytes(Path(control.__file__).read_bytes())
    contract=source/'runtime_manager.py'
    contract.write_text("PRODUCT_SERVICES=('eimemory-rpc.service',)\n"
        "def is_dynamic_unit(unit):return False\n"
        "def fixed_system_unit_cgroup(group,unit):return group=='/system.slice/'+unit\n")
    contract.chmod(0o644)
    monkeypatch.setattr(control,'__package__','')
    monkeypatch.setattr(control,'__file__',str(helper))
    native=control.os
    monkeypatch.setattr(control,'os',SimpleNamespace(**{**vars(native),
        'statvfs':lambda path:SimpleNamespace(f_flag=native.ST_RDONLY if readonly else 0)}))
    if readonly:assert control.native_control_consumer(['0::/system.slice/eimemory-rpc.service'])
    else:
        with pytest.raises(ValueError,match='contract_parent_untrusted'):
            control.native_control_consumer(['0::/system.slice/eimemory-rpc.service'])
