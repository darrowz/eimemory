"""The user controller sees host facts while product namespaces stay sealed."""
from pathlib import Path
from contextlib import contextmanager
import os
import sys
from types import SimpleNamespace

import pytest

from eimemory.contracts import user_control_plane as authority


@pytest.fixture
def host_proc(tmp_path, monkeypatch):
    proc = tmp_path/'proc'
    for owner in ('self', '1'):
        (proc/owner).mkdir(parents=True)
        for name in ('uid_map', 'gid_map'):
            (proc/owner/name).write_text('0 0 4294967295\n')
    (proc/'self/status').write_text('Uid:\t1001 1001 1001 1001\nGid:\t1001 1001 1001 1001\n'
        'NoNewPrivs:\t1\nCapEff:\t0000000000000000\nCapPrm:\t0000000000000000\n'
        'CapAmb:\t0000000000000000\nCapBnd:\t000001ffffffffff\n')
    monkeypatch.setattr(authority, 'account', lambda:SimpleNamespace(pw_uid=1001, pw_gid=1001))
    monkeypatch.setattr(authority.os, 'getgid', lambda:1001, raising=False)
    return proc


def test_controller_accepts_inherited_bounding_without_granted_capabilities(host_proc):
    authority.verify_controller_host_namespace(host_proc)


@pytest.mark.parametrize('fault,reason', [
    ('real_gid', 'identity_invalid'), ('uid', 'identity_invalid'), ('gid', 'identity_invalid'),
    ('nnp', 'nnp_required'), ('cap_eff', 'capabilities_forbidden'),
    ('cap_prm', 'capabilities_forbidden'), ('cap_amb', 'capabilities_forbidden'),
    ('cap_missing', 'capabilities_forbidden'), ('cap_malformed', 'capabilities_forbidden'),
    ('private_uid', 'host_namespace_required'), ('private_gid', 'host_namespace_required'),
    ('substituted_pid1', 'host_namespace_required'), ('host_uid', 'host_namespace_required'),
    ('host_gid', 'host_namespace_required'), ('map_extra', 'host_namespace_required'),
    ('map_empty', 'host_namespace_required'), ('map_malformed', 'namespace_unverified'),
    ('map_unreadable', 'namespace_unverified'), ('status_unreadable', 'namespace_unverified'),
])
def test_controller_rejects_partial_namespace_or_capability_gain(host_proc, monkeypatch, fault, reason):
    status = host_proc/'self/status'
    if fault=='real_gid':
        monkeypatch.setattr(authority.os, 'getgid', lambda:7, raising=False)
    elif fault in {'uid', 'gid'}:
        name = fault.title()
        status.write_text(status.read_text().replace(name+':\t1001 1001 1001 1001', name+':\t1001 1001 1001 0'))
    elif fault=='nnp':status.write_text(status.read_text().replace('NoNewPrivs:\t1', 'NoNewPrivs:\t0'))
    elif fault in {'cap_eff', 'cap_prm', 'cap_amb', 'cap_missing', 'cap_malformed'}:
        name = {'cap_eff':'CapEff', 'cap_prm':'CapPrm', 'cap_amb':'CapAmb'}.get(fault, 'CapEff')
        replacement = '1' if fault not in {'cap_missing', 'cap_malformed'} else ('garbage' if fault=='cap_malformed' else '')
        status.write_text(status.read_text().replace(name+':\t0000000000000000', name+':\t'+replacement))
    elif fault in {'private_uid', 'private_gid', 'substituted_pid1', 'host_uid', 'host_gid'}:
        name = 'gid_map' if fault.endswith('gid') else 'uid_map'
        owners = ('self', '1') if fault=='substituted_pid1' else ('1',) if fault.startswith('host_') else ('self',)
        for owner in owners:(host_proc/owner/name).write_text('1001 1001 1\n')
    elif fault.startswith('map_'):
        path = host_proc/'self/uid_map'
        if fault=='map_unreadable':path.unlink()
        else:path.write_text({'map_extra':'0 0 4294967295\n1001 1001 1\n', 'map_empty':'', 'map_malformed':'wrong map\n'}[fault])
    elif fault=='status_unreadable':status.unlink()
    with pytest.raises(ValueError, match='user_control_plane_controller_'+reason):
        authority.verify_controller_host_namespace(host_proc)


@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling_symlink'])
def test_controller_rejects_every_incomplete_installation_marker(tmp_path, monkeypatch, kind):
    marker = tmp_path/'controller-installation.json'
    if kind=='file':marker.write_text('{}')
    elif kind=='directory':marker.mkdir()
    else:
        try:marker.symlink_to(tmp_path/'missing')
        except OSError:pytest.skip('symlink creation unavailable')
    monkeypatch.setattr(authority, 'coordinates', lambda:{'state':tmp_path})
    monkeypatch.setattr(authority, 'verify_controller_host_namespace', lambda:pytest.fail('installation barrier must precede observation'))
    with pytest.raises(ValueError, match='controller_installation_incomplete'):
        authority.controller_startup_guard()


def test_controller_startup_checks_native_namespace_after_completed_installation(tmp_path, monkeypatch):
    monkeypatch.setattr(authority, 'coordinates', lambda:{'state':tmp_path})
    checked=[]
    monkeypatch.setattr(authority, 'verify_controller_host_namespace', lambda:checked.append(True))
    authority.controller_startup_guard()
    assert checked==[True]


@pytest.mark.parametrize('blocked', [False, True])
def test_bridge_locks_and_guards_before_loading_or_serving(monkeypatch, blocked):
    from test_runtime_manager import standalone
    bridge=standalone('runtime_manager_bridge')
    events=[]
    @contextmanager
    def lock(path):
        assert path==bridge.STATE/'bridge.lock'
        events.append('lock')
        try:yield 1
        finally:events.append('unlock')
    def guard():
        events.append('guard')
        if blocked:raise ValueError('installation_incomplete')
    def protect(*args):
        assert args==(4,0,0,0,0)
        events.append('nondumpable')
        return 0
    monkeypatch.setitem(sys.modules, 'ctypes', SimpleNamespace(CDLL=lambda *_,**__:SimpleNamespace(prctl=protect)))
    monkeypatch.setattr(bridge, 'USER_MODE', True)
    monkeypatch.setattr(bridge.os, 'geteuid', lambda:1001, raising=False)
    monkeypatch.setattr(bridge, 'trusted_directory', lambda *_,**__:events.append('directory'))
    monkeypatch.setattr(bridge, 'user_control', SimpleNamespace(verified_lock=lock, controller_startup_guard=guard), raising=False)
    monkeypatch.setattr(bridge, 'load_contract', lambda:events.append('contract'))
    monkeypatch.setattr(bridge, 'Bridge', lambda _:None)
    monkeypatch.setattr(bridge, 'serve', lambda _:events.append('serve'))
    if blocked:
        with pytest.raises(ValueError,match='installation_incomplete'):bridge.main()
        assert events==['nondumpable','directory','lock','guard','unlock']
    else:
        bridge.main()
        assert events==['nondumpable','directory','lock','guard','contract','serve','unlock']


NATIVE = pytest.mark.skipif(os.name!='posix', reason='requires native Linux flock and ownership')


@NATIVE
def test_verified_lock_excludes_a_second_open_and_releases_after_error(tmp_path):
    if os.geteuid()==0:pytest.skip('requires user operator')
    lock = tmp_path/'bridge.lock'
    with pytest.raises(OSError, match='body_failure'):
        with authority.verified_lock(lock) as descriptor:
            assert os.fstat(descriptor).st_mode & 0o777==0o600
            with pytest.raises(ValueError, match='lock_busy'):
                with authority.verified_lock(lock):pytest.fail('duplicate acquired')
            raise OSError('body_failure')
    with authority.verified_lock(lock):pass


@NATIVE
@pytest.mark.parametrize('fault', ['symlink', 'hardlink', 'writable', 'fifo', 'directory', 'foreign'])
def test_verified_lock_rejects_unsafe_existing_names(tmp_path, monkeypatch, fault):
    if os.geteuid()==0:pytest.skip('requires user operator')
    lock = tmp_path/'bridge.lock'
    lock.write_bytes(b'');lock.chmod(0o600)
    if fault=='symlink':
        real=tmp_path/'original';lock.rename(real);lock.symlink_to(real)
    elif fault=='hardlink':os.link(lock,tmp_path/'alias')
    elif fault=='writable':lock.chmod(0o660)
    elif fault=='fifo':lock.unlink();os.mkfifo(lock,0o600)
    elif fault=='directory':lock.unlink();lock.mkdir()
    elif fault=='foreign':
        current=authority.account()
        monkeypatch.setattr(authority, 'account', lambda:SimpleNamespace(pw_uid=current.pw_uid+1, pw_dir=current.pw_dir))
        monkeypatch.setattr(authority, 'trusted_directory', lambda *_:None)
    with pytest.raises(ValueError, match='lock_untrusted'):
        with authority.verified_lock(lock):pytest.fail('unsafe lock acquired')


@NATIVE
def test_verified_lock_binds_the_name_after_flock(tmp_path, monkeypatch):
    if os.geteuid()==0:pytest.skip('requires user operator')
    import fcntl
    lock=tmp_path/'bridge.lock'
    original=fcntl.flock
    def replace_after_acquiring(descriptor,operation):
        original(descriptor,operation)
        lock.rename(tmp_path/'previous')
        lock.write_bytes(b'');lock.chmod(0o600)
    monkeypatch.setattr(fcntl,'flock',replace_after_acquiring)
    with pytest.raises(ValueError,match='lock_changed'):
        with authority.verified_lock(lock):pytest.fail('substituted lock acquired')


@NATIVE
def test_verified_lock_binds_ancestors_and_refuses_symlinked_parent(tmp_path):
    if os.geteuid()==0:pytest.skip('requires user operator')
    directory=tmp_path/'real';directory.mkdir(mode=0o700)
    alias=tmp_path/'alias';alias.symlink_to(directory,target_is_directory=True)
    with pytest.raises(ValueError,match='directory_untrusted'):
        with authority.verified_lock(alias/'bridge.lock'):pytest.fail('aliased parent acquired')


@NATIVE
def test_verified_lock_new_file_preserves_exact_mode_under_restrictive_umask(tmp_path):
    if os.geteuid()==0:pytest.skip('requires user operator')
    previous=os.umask(0o777)
    try:
        with authority.verified_lock(tmp_path/'bridge.lock') as descriptor:
            assert os.fstat(descriptor).st_mode & 0o777==0o600
    finally:os.umask(previous)
