"""Protected Unix operator requests for exact identity conflict manifests.

The root-owned policy and append-only, hash-chained journal are an authority
boundary separate from the service-writable memory database. A shared flock is
held through decision commit/current-view validation; revocations take the
exclusive lock. Root and the host filesystem are explicitly trusted.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import sys
import uuid

from eimemory.core.strict_json import loads
from eimemory.contracts.user_control_plane import user_mode, coordinates, account, trusted_directory

DEFAULT_DIRECTORY = "/etc/eimemory-identity-governance"
MAX_JOURNAL_BYTES = 16 * 1024 * 1024
SCOPE_KEYS = {"tenant_id", "agent_id", "workspace_id", "user_id"}


class IdentityAuthorityError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def authority_directory(value=None):
    if user_mode():
        fixed=coordinates()['authority']/'identity'
        if value is not None and Path(value)!=fixed:
            raise IdentityAuthorityError('identity_user_authority_path_fixed')
        return fixed
    return Path(value or os.environ.get("EIMEMORY_IDENTITY_AUTHORITY_DIR") or DEFAULT_DIRECTORY)


def _require_root():
    if os.name == "posix" and os.getuid() != os.geteuid():
        raise IdentityAuthorityError("identity_authenticated_root_operator_required")
    if user_mode():
        account()
        return
    if os.name != "posix" or os.geteuid() != 0:
        raise IdentityAuthorityError("identity_authenticated_root_operator_required")


def _namespace_owner():
    """Host root may be an overflow UID in a systemd PrivateUsers namespace."""
    if not sys.platform.startswith("linux"):
        return 0, False
    try:
        mappings = [tuple(map(int, line.split())) for line in Path('/proc/self/uid_map').read_text().splitlines()]
        if mappings == [(0, 0, 4294967295)]:
            return 0, False
        for inside, outside, length in mappings:
            if outside == 0 and length:
                return inside, True
        return int(Path('/proc/sys/kernel/overflowuid').read_text()), True
    except (OSError, ValueError) as exc:
        raise IdentityAuthorityError('identity_authority_namespace_unavailable') from exc


def _inode_flags(fd):
    """Only host ext filesystems can supply this kernel-backed root seal.

    FUSE/tmpfs/user-namespace filesystems must never emulate the authority.
    Linux FS_IOC_SETFLAGS requires CAP_LINUX_IMMUTABLE for i/a flags.
    """
    import array
    import ctypes
    import fcntl

    if not sys.platform.startswith('linux') or ctypes.sizeof(ctypes.c_long) != 8:
        raise IdentityAuthorityError('identity_authority_seal_platform_unsupported')
    buffer = ctypes.create_string_buffer(256)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
    libc.fstatfs.restype = ctypes.c_int
    if libc.fstatfs(fd, ctypes.byref(buffer)) != 0:
        raise OSError(ctypes.get_errno(), 'identity_authority_statfs_failed')
    if ctypes.c_long.from_buffer(buffer).value != 0xEF53:
        raise IdentityAuthorityError('identity_authority_seal_requires_ext_filesystem')
    flags = array.array('l', [0])
    fcntl.ioctl(fd, 0x80086601, flags, True)
    return flags[0]


def _seal_required(fd, flag):
    if not _inode_flags(fd) & flag:
        raise IdentityAuthorityError('identity_authority_host_seal_missing')


def _directory(path):
    if user_mode():
        try:trusted_directory(path)
        except (ValueError,OSError) as exc:raise IdentityAuthorityError('identity_user_authority_directory_untrusted') from exc
        return
    if os.name != "posix" or not path.is_absolute() or ".." in path.parts:
        raise IdentityAuthorityError("identity_authority_path_invalid")
    owner, private = _namespace_owner()
    for parent in [*reversed(path.parents), path]:
        meta = parent.lstat()
        # A root-owned sticky /tmp cannot rename another owner's directory.
        sticky = bool(meta.st_mode & stat.S_ISVTX) and parent != path
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid != owner
                or (meta.st_mode & 0o022 and not sticky)):
            raise IdentityAuthorityError("identity_authority_directory_untrusted")
    if private:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            _seal_required(fd, 0x10)
        finally:
            os.close(fd)


def _open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        meta = os.fstat(fd)
        owner, private = (account().pw_uid,False) if user_mode() else _namespace_owner()
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != owner
                or meta.st_mode & 0o022 or meta.st_nlink != 1):
            raise IdentityAuthorityError("identity_authority_file_untrusted")
        if private:
            _seal_required(fd, 0x10 if path.name == 'policy.json' else 0x20)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd, maximum):
    os.lseek(fd, 0, os.SEEK_SET)
    parts, size = [], 0
    while chunk := os.read(fd, min(65536, maximum + 1 - size)):
        parts.append(chunk)
        size += len(chunk)
        if size > maximum:
            raise IdentityAuthorityError("identity_authority_size_limit")
    return b"".join(parts)


def _scope(value):
    return (isinstance(value, dict) and set(value) == SCOPE_KEYS
            and all(isinstance(v, str) and v.strip() == v and v and "*" not in v
                    for v in value.values()))


def _policy(path):
    fd = _open(path, os.O_RDONLY)
    try:
        policy = loads(_read(fd, 65536), max_bytes=65536, max_depth=12)
    finally:
        os.close(fd)
    current_user=user_mode() and isinstance(policy,dict) and policy.get('schema')=='identity_operator_policy.v2'
    expected_uid=account().pw_uid if current_user else 0
    if (not isinstance(policy, dict) or policy.get("schema") != ('identity_operator_policy.v2' if current_user else 'identity_operator_policy.v1')
            or type(policy.get("requester_uid")) is not int or policy["requester_uid"] != expected_uid
            or not _scope(policy.get("owner_scope"))
            or not isinstance(policy.get("source_ids"), list) or not policy["source_ids"]
            or any(not isinstance(s, str) or not s or "*" in s for s in policy["source_ids"])):
        raise IdentityAuthorityError("identity_operator_policy_invalid")
    if current_user:
        retained=policy.get('retained_policy_digests')
        if (not isinstance(retained,list) or len(retained)>32
                or any(not isinstance(item,str) or len(item)!=64 or any(c not in '0123456789abcdef' for c in item) for item in retained)
                or len(set(retained))!=len(retained)):
            raise IdentityAuthorityError('identity_retained_policy_inventory_invalid')
    return policy


def initialize(directory, *, owner_scope, source_ids, service_group):
    _require_root()
    import grp

    path = authority_directory(directory)
    _directory(path.parent)
    if not _scope(owner_scope) or not source_ids or any(not s or "*" in s for s in source_ids):
        raise IdentityAuthorityError("identity_operator_policy_invalid")
    group = grp.getgrnam(service_group).gr_gid
    path.mkdir(mode=0o750)
    owner_uid=account().pw_uid if user_mode() else 0
    os.chown(path, owner_uid, group)
    os.chmod(path, 0o750)
    policy = {"schema": "identity_operator_policy.v2" if user_mode() else "identity_operator_policy.v1", "requester_uid": os.geteuid(),
              "owner_scope": owner_scope, "source_ids": sorted(set(source_ids))}
    if user_mode():policy['retained_policy_digests']=[]
    for name, data in [("policy.json", canonical(policy) + "\n"), ("requests.jsonl", "")]:
        fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
        try:
            os.fchown(fd, owner_uid, group)
            os.fchmod(fd, 0o640)
            os.write(fd, data.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
    dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return {"ok": True, "policy_digest": digest(policy)}


def seal(directory=None, *, enabled=True):
    """Initial-namespace root seals policy/directory and append-only requests."""
    if user_mode():
        raise IdentityAuthorityError('identity_user_authority_uses_namespace_seal')
    _require_root()
    if _namespace_owner()[1]:
        raise IdentityAuthorityError('identity_authority_seal_requires_host_root')
    import array
    import fcntl

    path = authority_directory(directory)
    with snapshot(directory, exclusive=True):
        entries = [(path / 'requests.jsonl', 0x20), (path / 'policy.json', 0x10), (path, 0x10)]
        if not enabled:
            entries.reverse()
        for entry, bit in entries:
            fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                before = _inode_flags(fd)
                flags = array.array('l', [(before | bit) if enabled else (before & ~bit)])
                fcntl.ioctl(fd, 0x40086602, flags)
                if bool(_inode_flags(fd) & bit) != enabled:
                    raise IdentityAuthorityError('identity_authority_seal_not_applied')
                os.fsync(fd)
            finally:
                os.close(fd)
    return {'ok': True, 'host_kernel_seal': enabled, 'filesystem_contract': 'linux_ext_root_inode_flags.v1'}


class AuthoritySnapshot:
    def __init__(self, policy, events, retained_policies=()):
        self.policy = policy
        self.policy_digest = digest(policy)
        self.policies={digest(item):item for item in (*retained_policies,policy)}
        self.events = events
        self.grants = {}
        self.revoked = set()
        for event in events:
            if event.get("operation") == "authorize":
                self.grants[event["event_id"]] = event
            elif event.get("operation") == "revoke":
                if event.get("grant_id") not in self.grants:
                    raise IdentityAuthorityError("identity_revocation_unknown_grant")
                self.revoked.add(event["grant_id"])
            else:
                raise IdentityAuthorityError("identity_authority_event_invalid")

    def active(self, grant_id):
        grant = self.grants.get(grant_id)
        policy=self.policies.get(grant.get('policy_digest')) if grant is not None else None
        if (grant is None or grant_id in self.revoked
                or policy is None or grant.get("requester_uid") != policy["requester_uid"]):
            return None
        manifest = grant.get("manifest")
        if (not isinstance(manifest, dict) or digest(manifest) != grant.get("manifest_digest")
                or manifest.get("owner_scope") != self.policy["owner_scope"]
                or manifest.get("source_id") not in self.policy["source_ids"]
                or manifest.get('owner_scope')!=policy['owner_scope']
                or manifest.get('source_id') not in policy['source_ids']):
            return None
        return grant


@contextmanager
def snapshot(directory=None, *, exclusive=False, blocking=True):
    if exclusive:
        _require_root()
    path = authority_directory(directory)
    _directory(path)
    import fcntl

    fd = _open(path / "requests.jsonl", (os.O_RDWR | os.O_APPEND) if exclusive else os.O_RDONLY)
    try:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fd, mode if blocking else mode | fcntl.LOCK_NB)
        policy = _policy(path / "policy.json")
        retained=[]
        if user_mode():
            for reference in policy.get('retained_policy_digests',[]):
                archived=_policy(path/'retained-policies'/(reference+'.json'))
                if (digest(archived)!=reference or archived.get('schema')!='identity_operator_policy.v1'
                        or archived['owner_scope']!=policy['owner_scope']):
                    raise IdentityAuthorityError('identity_retained_policy_unbound')
                retained.append(archived)
        requesters={item['requester_uid'] for item in (*retained,policy)}
        raw = _read(fd, MAX_JOURNAL_BYTES)
        if raw and not raw.endswith(b"\n"):
            raise IdentityAuthorityError("identity_authority_journal_incomplete")
        events, previous, seen = [], "", set()
        for line in raw.splitlines():
            event = loads(line, max_bytes=1_100_000, max_depth=32)
            if not isinstance(event, dict):
                raise IdentityAuthorityError("identity_authority_event_invalid")
            body = {k: v for k, v in event.items() if k != "event_hash"}
            event_id = event.get("event_id")
            if (not isinstance(event_id, str) or not event_id or event_id in seen
                    or event.get("previous_hash") != previous
                    or digest(body) != event.get("event_hash")
                    or type(event.get("requester_uid")) is not int or event["requester_uid"] not in requesters):
                raise IdentityAuthorityError("identity_authority_chain_invalid")
            seen.add(event_id)
            previous = event["event_hash"]
            events.append(event)
        yield AuthoritySnapshot(policy, events,retained), fd
    finally:
        os.close(fd)


def _append(authority, fd, payload):
    if user_mode() and authority.policy['requester_uid']!=account().pw_uid:
        raise IdentityAuthorityError('identity_user_policy_upgrade_required')
    event = {**payload, "event_id": uuid.uuid4().hex, "requester_uid": os.geteuid(),
             "requested_at": datetime.now(timezone.utc).isoformat(),
             "previous_hash": authority.events[-1]["event_hash"] if authority.events else ""}
    event["event_hash"] = digest(event)
    data = (canonical(event) + "\n").encode()
    if os.fstat(fd).st_size + len(data) > MAX_JOURNAL_BYTES:
        raise IdentityAuthorityError("identity_authority_size_limit")
    os.lseek(fd, 0, os.SEEK_END)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("identity_authority_short_write")
        view = view[written:]
    os.fsync(fd)
    return event


def migrate_user_policy():
    """Retain every original grant/hash while changing only future operator identity."""
    if not user_mode():raise IdentityAuthorityError('identity_user_control_plane_required')
    _require_root()
    path=authority_directory()
    with snapshot(exclusive=True) as (authority,_fd):
        if authority.policy['schema']=='identity_operator_policy.v2':
            return {'ok':True,'status':'existing','policy_digest':authority.policy_digest}
        legacy=authority.policy
        history=path/'retained-policies'
        history.mkdir(mode=0o750,exist_ok=True);_directory(history)
        archive=history/(authority.policy_digest+'.json')
        raw=(canonical(legacy)+'\n').encode()
        if archive.exists():
            if _policy(archive)!=legacy:raise IdentityAuthorityError('identity_retained_policy_changed')
        else:
            fd=os.open(archive,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o640)
            with os.fdopen(fd,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
        current={**legacy,'schema':'identity_operator_policy.v2','requester_uid':account().pw_uid,
                 'retained_policy_digests':[authority.policy_digest]}
        temporary=path/('.user-policy-'+uuid.uuid4().hex)
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o640)
        with os.fdopen(fd,'wb') as stream:
            stream.write((canonical(current)+'\n').encode());stream.flush();os.fsync(stream.fileno())
        os.replace(temporary,path/'policy.json')
        for directory in (history,path):
            fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(fd)
            finally:os.close(fd)
        return {'ok':True,'status':'migrated','previous_policy_digest':authority.policy_digest,
                'policy_digest':digest(current),'retained_events':len(authority.events),
                'historical_requester_ids_rewritten':False}


def authorize(directory, *, database, manifest, request_text):
    _require_root()
    from eimemory.identity_ops import preview_identity_manifest

    if not isinstance(request_text, str) or not request_text.strip() or len(request_text) > 4000:
        raise IdentityAuthorityError("identity_human_request_required")
    if not isinstance(manifest, dict):
        raise IdentityAuthorityError("identity_manifest_invalid")
    with snapshot(directory, exclusive=True) as (authority, fd):
        if (manifest.get("owner_scope") != authority.policy["owner_scope"]
                or manifest.get("source_id") not in authority.policy["source_ids"]):
            raise IdentityAuthorityError("identity_owner_source_not_authorized")
        report = preview_identity_manifest(database, manifest)
        if (report.get("validation_ok") is not True
                or any(item.get("decision") != "retain_both" for item in manifest["items"])):
            raise IdentityAuthorityError("identity_manifest_validation_failed")
        refs = {tuple(item[side + "_ref"]) for item in manifest["items"] for side in ("source", "target")}
        for grant_id in authority.grants:
            active = authority.active(grant_id)
            if active is None:
                continue
            if active["manifest_digest"] == digest(manifest):
                return {"ok": True, "grant_id": grant_id, "manifest_digest": digest(manifest), "idempotent": True}
            if any(tuple(item[side + "_ref"]) in refs
                   for item in active["manifest"]["items"] for side in ("source", "target")):
                raise IdentityAuthorityError("identity_authority_overlapping_request")
        event = _append(authority, fd, {"operation": "authorize", "policy_digest": authority.policy_digest,
                         "manifest_digest": digest(manifest), "manifest": manifest, "request_text": request_text})
        return {"ok": True, "grant_id": event["event_id"], "manifest_digest": event["manifest_digest"],
                "requester_uid": event["requester_uid"], "idempotent": False}


def revoke(directory, *, grant_id, reason):
    _require_root()
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
        raise IdentityAuthorityError("identity_revocation_reason_required")
    with snapshot(directory, exclusive=True) as (authority, fd):
        if grant_id not in authority.grants:
            raise IdentityAuthorityError("identity_authority_grant_missing")
        event = _append(authority, fd, {"operation": "revoke", "grant_id": grant_id, "reason": reason})
        return {"ok": True, "revocation_id": event["event_id"], "grant_id": grant_id}
