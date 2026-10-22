import importlib.util, os, re
from pathlib import Path
from types import SimpleNamespace

import pytest

def _m():
    spec = importlib.util.spec_from_file_location("rot", Path(__file__).parents[1] / "deploy" / "rotate_releases.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

def _mk(root, names):
    root.mkdir(parents=True, exist_ok=True)
    for i, n in enumerate(names):
        d = root / n; d.mkdir(); os.utime(d, (1000 + i, 1000 + i))

C = [f"{i:040x}" for i in range(8)]  # C[7] newest

def test_keeps_newest_plus_current_and_rollback(tmp_path, monkeypatch):
    monkeypatch.delenv("EIMEMORY_KEEP_RELEASES", raising=False)
    m = _m(); r = tmp_path / "releases"; _mk(r, C)
    (r / ".eimemory-stage-x").mkdir(); (r / "notes").mkdir(); (r / f"{'f'*40}").symlink_to(r / C[0])
    rc = m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "none"),
                 "--current-commit", C[0], "--previous-commit", C[1]])
    assert rc == 0
    left = {p.name for p in r.iterdir()}
    assert {C[0], C[1], ".eimemory-stage-x", "notes", "f" * 40} <= left
    # default keep 5 newest non-protected (C[3..7]) plus protected C[0], C[1]
    assert {c for c in C if c in left} == {C[0], C[1], *C[3:]}

def test_keep_n_removes_oldest_only(tmp_path, monkeypatch):
    m = _m(); r = tmp_path / "releases"; _mk(r, C)
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASES", "2")
    m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "s"), "--current-commit", C[0], "--previous-commit", C[3]])
    assert {p.name for p in r.iterdir()} == {C[0], C[3], C[7], C[6]}

def test_snapshots_protect_current_prefix_and_zero_disables(tmp_path, monkeypatch):
    m = _m(); s = tmp_path / "snap"; _mk(s, [f"{c}-a{i}" for i, c in enumerate(C)])
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASE_SNAPSHOTS", "1")
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASES", "0")
    m.main(["--releases-root", str(tmp_path / "r"), "--snapshot-root", str(s), "--current-commit", C[2]])
    assert {p.name.split("-")[0] for p in s.iterdir()} == {C[2], C[7]}

def test_dry_run_and_invalid_current(tmp_path, monkeypatch):
    m = _m(); r = tmp_path / "releases"; _mk(r, C)
    monkeypatch.setenv("EIMEMORY_KEEP_RELEASES", "1")
    m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "s"), "--current-commit", C[0], "--dry-run"])
    assert len(list(r.iterdir())) == 8
    assert m.main(["--releases-root", str(r), "--snapshot-root", str(tmp_path / "s"), "--current-commit", "bad"]) == 2


POSIX_OWNER = pytest.mark.skipif(
    os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() <= 0,
    reason='sealed artifact rotation requires its non-root POSIX owner',
)


@POSIX_OWNER
def test_rotation_removes_owned_sealed_source_and_venv_without_following_links(tmp_path, monkeypatch):
    m = _m()
    root = tmp_path / 'releases'
    _mk(root, C)
    old = root / C[2]
    source = old / 'eimemory/package'
    source.mkdir(parents=True)
    (source / 'module.py').write_text('source')
    venv = old / '.venv/cache'
    venv.mkdir(parents=True)
    (venv / 'retained.pyc').write_bytes(b'dependency')
    outside = tmp_path / 'native-application'
    outside.mkdir()
    (outside / 'cache').write_bytes(b'unchanged')
    (source / 'external').symlink_to(outside, target_is_directory=True)
    for directory in (source, source.parent, old):
        directory.chmod(0o555)
    os.utime(old, (1002, 1002))
    for name in (C[0], C[1]):
        (root / name).chmod(0o555)
    before = (outside.stat().st_mode, (outside / 'cache').read_bytes())
    monkeypatch.delenv('EIMEMORY_KEEP_RELEASES', raising=False)
    assert m.main(['--releases-root', str(root), '--snapshot-root', str(tmp_path / 'none'),
        '--current-commit', C[0], '--previous-commit', C[1]]) == 0
    assert not old.exists()
    assert (outside.stat().st_mode, (outside / 'cache').read_bytes()) == before
    assert all((root / name).stat().st_mode & 0o777 == 0o555 for name in (C[0], C[1]))


@POSIX_OWNER
@pytest.mark.parametrize('kind', ['file', 'directory', 'symlink'])
def test_rotation_rejects_foreign_entries_before_changing_any_mode(tmp_path, monkeypatch, kind):
    m = _m()
    root = tmp_path / 'releases'
    target = root / C[0]
    target.mkdir(parents=True)
    foreign = target / 'foreign'
    if kind == 'directory':
        foreign.mkdir()
    elif kind == 'symlink':
        foreign.symlink_to(tmp_path / 'outside')
    else:
        foreign.write_bytes(b'retained')
    target.chmod(0o555)
    expected = m._identity(target.lstat())
    native = m.os
    def inspect(name, *args, **kwargs):
        info = native.stat(name, *args, **kwargs)
        if name == 'foreign' and kwargs.get('dir_fd') is not None:
            fields = {field: getattr(info, field) for field in dir(info) if field.startswith('st_')}
            fields['st_uid'] = os.geteuid() + 10000
            return SimpleNamespace(**fields)
        return info
    monkeypatch.setattr(m, 'os', SimpleNamespace(**{**vars(native), 'stat': inspect}))
    with pytest.raises(OSError, match='foreign'):
        m.remove_owned_directory(target, root, expected=expected, protected=set())
    assert target.stat().st_mode & 0o777 == 0o555
    assert os.path.lexists(foreign)


@POSIX_OWNER
@pytest.mark.parametrize('kind', ['directory', 'symlink'])
def test_rotation_refuses_a_replaced_planned_directory(tmp_path, kind):
    m = _m()
    root = tmp_path / 'releases'
    target = root / C[0]
    target.mkdir(parents=True)
    (target / 'original').write_bytes(b'retained')
    expected = m._identity(target.lstat())
    saved = tmp_path / 'saved-original'
    target.rename(saved)
    if kind == 'directory':
        target.mkdir()
        (target / 'replacement').write_bytes(b'preserved')
    else:
        target.symlink_to(saved, target_is_directory=True)
    with pytest.raises(OSError):
        m.remove_owned_directory(target, root, expected=expected, protected=set())
    assert (saved / 'original').read_bytes() == b'retained'
    assert os.path.lexists(target)


@POSIX_OWNER
def test_deletion_boundary_rechecks_current_and_previous_protection(tmp_path):
    m = _m()
    root = tmp_path / 'releases'
    _mk(root, C[:2])
    for name in C[:2]:
        target = root / name
        target.chmod(0o555)
        with pytest.raises(OSError, match='protected'):
            m.remove_owned_directory(target, root, expected=m._identity(target.lstat()),
                                     protected=set(C[:2]))
        assert target.stat().st_mode & 0o777 == 0o555
