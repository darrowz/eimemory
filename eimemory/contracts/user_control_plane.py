"""Fixed user control-plane coordinates, shared with standalone controllers.

The deployment selector chooses a transport, never grants authority. Every
consumer must still authenticate files, peers, native jobs and evidence.
Legacy coordinates are retained for reading pre-migration deployments.
"""
from __future__ import annotations

import os
from pathlib import Path
import stat

MODE_ENV = 'EIMEMORY_CONTROL_PLANE'


def user_mode():
    mode = os.environ.get(MODE_ENV, '')
    if mode not in {'', 'user'}:
        raise ValueError('user_control_plane_mode_invalid')
    if mode == 'user':
        return True
    if os.name == 'posix' and os.geteuid() > 0:
        directory=Path(account().pw_dir)/'.config/eimemory/control-plane'
        # Enrollment survives a missing/corrupt registry: never fall back to
        # a privileged endpoint after a user deployment has been installed.
        return any(os.path.lexists(directory/name) for name in
                   ('installation.v1.json','runtime-manager.v1.json'))
    return False


def account():
    if os.name != 'posix':
        raise ValueError('user_control_plane_posix_identity_required')
    import pwd
    uid = os.geteuid()
    if uid <= 0 or os.getuid() != uid:
        raise ValueError('user_control_plane_nonroot_identity_required')
    value = pwd.getpwuid(uid)
    home = Path(value.pw_dir)
    if (value.pw_uid != uid or value.pw_gid != os.getegid() or not home.is_absolute()
            or '..' in home.parts or home == Path('/') or home.is_symlink()):
        raise ValueError('user_control_plane_account_invalid')
    return value


def coordinates():
    value = account()
    home = Path(value.pw_dir)
    authority = home / '.config/eimemory/control-plane'
    state = home / '.local/state/eimemory/control-plane'
    code = home / '.local/lib/eimemory/control-plane'
    return dict(uid=value.pw_uid, gid=value.pw_gid, home=home, authority=authority,
                state=state, code=code, registry=authority/'runtime-manager.v1.json',
                socket=Path('/run/user')/str(value.pw_uid)/'eimemory-manager/bridge.sock',
                pycache=code/'empty-pycache')


def control_uid():
    return account().pw_uid if user_mode() else 0


def _system_owner(info, path, uid):
    if info.st_uid == 0:
        return True
    # PrivateUsers maps the host's system directories to overflowuid. Admit
    # only fixed system ancestors, never an arbitrary foreign-owned directory.
    home = Path(account().pw_dir)
    known = {*home.parents, Path('/run'), Path('/run/user'), Path('/tmp'), Path('/var'), Path('/var/tmp')}
    if Path(path) not in known:
        return False
    try:
        overflow = int(Path('/proc/sys/kernel/overflowuid').read_text().strip())
        rows = [tuple(map(int, row.split())) for row in Path('/proc/self/uid_map').read_text().splitlines()]
        if not rows or any(len(row) != 3 or row[2] <= 0 for row in rows):
            return False
        mapped_self = any(start <= uid < start+size and host+(uid-start) == uid
                          for start,host,size in rows)
        parent_root_mapped = any(host == 0 for _,host,_ in rows)
        direct = mapped_self and not parent_root_mapped
        # bwrap nests another single-ID user namespace: its parent UID 0 is
        # the intermediate unprivileged namespace owner, not host root. Do
        # not infer that from a mapping alone. The actual system root must
        # remain unmapped and read-only, with NNP and no process capabilities.
        nested = False
        if rows == [(uid, 0, 1)] and os.stat('/').st_uid == overflow:
            fields=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
            nested=(bool(os.statvfs('/').f_flag & os.ST_RDONLY)
                and fields.get('NoNewPrivs','').strip()=='1'
                and all(fields.get(key,'').strip() and set(fields[key].strip())=={'0'}
                        for key in ('CapEff','CapPrm','CapAmb')))
        return info.st_uid == overflow and (direct or nested)
    except (OSError, ValueError):
        return False


def trusted_directory(path, *, private=False):
    """Reject symlinks, foreign owners and writable ancestors in user mode."""
    uid = account().pw_uid
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('user_control_plane_directory_invalid')
    for item in (path, *path.parents):
        info = item.lstat()
        system = _system_owner(info, item, uid)
        sticky = item != path and system and info.st_mode & stat.S_ISVTX
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid and not system
                or stat.S_IMODE(info.st_mode) & 0o022 and not sticky):
            raise ValueError('user_control_plane_directory_untrusted')
    info = path.lstat()
    if info.st_uid != uid or (private and stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('user_control_plane_directory_owner_invalid')
    if user_mode():
        fixed=coordinates()
        protected=(fixed['authority'],fixed['state'],fixed['code'],fixed['socket'].parent)
        if any(path==root or root in path.parents for root in protected):
            try:
                fields=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
                groups=Path('/proc/self/cgroup').read_text().splitlines()
                controller=any(row.endswith('/eimemory-runtime-manager.service') for row in groups)
                if (fields.get('NoNewPrivs','').strip()=='1' and not controller
                        and not os.statvfs(path).f_flag & os.ST_RDONLY):
                    raise ValueError('user_control_plane_namespace_not_readonly')
            except OSError as exc:
                raise ValueError('user_control_plane_namespace_unverified') from exc


def read_file(path, *, maximum=65536, modes=(0o600, 0o640)):
    path = Path(path)
    trusted_directory(path.parent)
    descriptors = []
    try:
        parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(parent)
        names = [Path(path.anchor)]
        current = names[0]
        for part in path.parts[1:-1]:
            parent = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            descriptors.append(parent)
            current /= part
            names.append(current)
        directory_identity = lambda s: (s.st_dev, s.st_ino, s.st_uid, s.st_gid, s.st_mode)
        original = [directory_identity(os.fstat(item)) for item in descriptors]
        uid = account().pw_uid
        for index, descriptor in enumerate(descriptors):
            info = os.fstat(descriptor)
            system = _system_owner(info, names[index], uid)
            sticky = index < len(descriptors)-1 and system and info.st_mode & stat.S_ISVTX
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != uid and not system
                    or info.st_mode & 0o022 and not sticky):
                raise ValueError('user_control_plane_directory_untrusted')
        if os.fstat(parent).st_uid != uid:
            raise ValueError('user_control_plane_directory_owner_invalid')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != account().pw_uid
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) not in modes
                or before.st_size > maximum):
            raise ValueError('user_control_plane_file_untrusted')
        raw = os.read(fd, maximum + 1)
        identity = lambda s: (s.st_dev, s.st_ino, s.st_uid, s.st_gid, s.st_mode,
                              s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if (len(raw) != before.st_size or identity(before) != identity(os.fstat(fd))
                or identity(before) != identity(path.lstat())):
            raise ValueError('user_control_plane_file_changed')
        if any(directory_identity(os.fstat(item)) != prior or directory_identity(name.lstat()) != prior
               for item, name, prior in zip(descriptors[:-1], names, original)):
            raise ValueError('user_control_plane_directory_changed')
        return raw
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
