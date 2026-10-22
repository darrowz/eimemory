"""Reviewed host-root authority, installed independently of writable product code.

Only the --readonly-worker branch, after an explicit UID check, imports product
code. Host root holds the signing key and append-only positive registry; it
never opens the runtime SQLite database or imports its Python modules.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
import time

DOMAIN = 'registered_code_defect_knowledge.v1'
KEY_SEMANTICS = 'conservative_native_symptom_signature.v1'
SCHEMA = 'managed_code_incident_proof.v1'
MANIFEST_PATH = '/etc/eimemory-code-automation-authority/managed-code-incident-manifest.v1.json'
DATABASE_PATH = '/var/lib/eimemory/state/eimemory.sqlite'
KEY_PATH = '/etc/eimemory-code-automation-authority/managed-incident-proof.key'
RECEIPT_DIRECTORY = '/var/lib/eimemory-manager/managed-incident-receipts'
PYCACHE_DIRECTORY = RECEIPT_DIRECTORY + '/empty-pycache'
CONTROLLER_HISTORY_DIRECTORY = '/var/lib/eimemory-manager'
RELEASES_DIRECTORY = '/opt/eimemory/releases'
USER_MODE = os.environ.get('EIMEMORY_CONTROL_PLANE', '') == 'user'
CONTROL_UID = os.geteuid() if USER_MODE else 0
if USER_MODE:
    import importlib.util
    helper=Path(__file__).with_name('user_control_plane.py')
    if not helper.is_file():
        helper=Path(__file__).parents[1]/'contracts/user_control_plane.py'
    metadata=helper.lstat()
    if helper.is_symlink() or metadata.st_uid!=CONTROL_UID or metadata.st_mode&0o022:
        raise ValueError('managed_authority_user_helper_untrusted')
    spec=importlib.util.spec_from_file_location('eimemory_user_control_plane',helper)
    user_control=importlib.util.module_from_spec(spec)
    exec(compile(helper.read_bytes(),str(helper),'exec'),user_control.__dict__)
    USER_COORDINATES=user_control.coordinates()
    MANIFEST_PATH=str(USER_COORDINATES['authority']/'managed-code-incident-manifest.v1.json')
    KEY_PATH=str(USER_COORDINATES['authority']/'managed-incident-proof.key')
    RECEIPT_DIRECTORY=str(USER_COORDINATES['state']/'managed-incident-receipts')
    PYCACHE_DIRECTORY=RECEIPT_DIRECTORY+'/empty-pycache'
    CONTROLLER_HISTORY_DIRECTORY=str(USER_COORDINATES['state'])

FIELDS = ('known_before_detection', 'prior_user_reported', 'manual_bootstrap')
SCOPE_FIELDS = ('tenant_id', 'agent_id', 'workspace_id', 'user_id')
INGRESS = ('eimemory.release_closure_failure', 'eimemory.runtime_identity_drift',
           'eimemory.system_code_repair_failure', 'eimemory.code_maintenance')
PRODUCER_FILES = (
    'eimemory/contracts/managed_code_incident_proof.py',
    'eimemory/governance/evolution/managed_code_incident_proof.py',
    'eimemory/governance/evolution/code_automation_policy_incidents.py',
    'eimemory/governance/evolution/code_evolution_incident_coverage.py',
    'eimemory/governance/evolution/code_maintenance.py',
    'eimemory/ops/runtime_identity_drift.py', 'eimemory/ops/release_closure_failure.py',
    'eimemory/ops/system_code_repair_failure.py',
    'eimemory/storage/code_incident_coverage_store.py',
    'eimemory/storage/code_incident_coverage_archive.py',
    'eimemory/storage/migrations/code_incident_coverage.py',
    'eimemory/storage/migrations/code_incident_coverage_archive.py',
    'eimemory/storage/payload_segments.py', 'eimemory/governance/learning/effect_policy.py',
)


# Runtime compatibility pins the immutable acquisition/serialization boundary,
# not repairable business logic or the l5_reader aggregation implementation.
# The independent readonly worker still pins the complete controller inventory.
RUNTIME_PRODUCER_FILES = tuple(dict.fromkeys((*PRODUCER_FILES,
    'eimemory/__init__.py', 'eimemory/contracts/__init__.py',
    'eimemory/governance/__init__.py', 'eimemory/governance/evolution/__init__.py',
    'eimemory/governance/learning/__init__.py', 'eimemory/governance/release/__init__.py',
    'eimemory/ops/__init__.py',
    'eimemory/contracts/code_incident_provenance.py',
    'eimemory/contracts/code_incident_coverage.py',
    'eimemory/models/records.py', 'eimemory/core/clock.py',
    'eimemory/core/ids.py', 'eimemory/core/record_ids.py',
    'eimemory/core/key_components.py',
    'eimemory/models/source_partitions.py', 'eimemory/models/identity_aliases.py',
    'eimemory/metadata.py', 'eimemory/storage/jsonl.py',
    'eimemory/storage/sqlite_store.py', 'eimemory/storage/runtime_store.py',
    'eimemory/storage/migrations/__init__.py',
    'eimemory/governance/release/closure_verdict.py',
    'eimemory/governance/release/closure_contracts.py',
    'eimemory/governance/release/closure_blockers.py',
    'eimemory/governance/release/release_pre_observation.py',
    'eimemory/governance/release_pre_observation.py',
)))


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value): return sha256(canonical(value)).hexdigest()


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('managed_authority_duplicate_json_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('managed_authority_nonfinite_json')))


def _hex(value, size=64):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{'+str(size)+'}', value) is None:
        raise ValueError('managed_authority_digest_invalid')
    return value


def _scope(value):
    if (not isinstance(value, dict) or set(value) != set(SCOPE_FIELDS)
            or any(not isinstance(value[k], str) or not value[k] or value[k] != value[k].strip()
                   or len(value[k]) > 512 or any(ord(c) < 32 for c in value[k]) for k in SCOPE_FIELDS)):
        raise ValueError('managed_authority_scope_invalid')
    return value


def _time(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else None
    if result is None or result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
        raise ValueError('managed_authority_time_invalid')
    return result


def _identity(meta):
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_gid, meta.st_mode,
            meta.st_nlink, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def _directory_identity(meta):
    # Root-only ancestors (including sticky global temporary directories) may
    # receive unrelated entries from other root jobs. Runtime cannot replace an
    # existing root entry there. Keep namespace/permission identity, while all
    # runtime-writable parents retain mutation clocks and every file stays full.
    if meta.st_uid==0 and (not meta.st_mode&0o022 or meta.st_mode&stat.S_ISVTX):
        return (meta.st_dev,meta.st_ino,meta.st_uid,meta.st_gid,meta.st_mode)
    return _identity(meta)


def _read(path, *, owners=(0,), gid=None, limit=16*1024*1024, mode=None,
          allow_empty=False, with_identity=False):
    """No-follow every component, with descriptor/name identity revalidation."""
    if USER_MODE and owners==(0,):owners=(CONTROL_UID,)
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts: raise ValueError('managed_authority_path_invalid')
    descriptors = []; names = []
    try:
        fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(fd)
        for part in path.parts[1:-1]:
            fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            descriptors.append(fd); names.append(part)
        before_dirs = [os.fstat(item) for item in descriptors]
        for index,meta in enumerate(before_dirs):
            sticky=(index<len(before_dirs)-1 and meta.st_uid==0 and bool(meta.st_mode&stat.S_ISVTX))
            if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in ({0,*owners} if USER_MODE else owners) or meta.st_mode & 0o002 and not sticky
                    or meta.st_mode & 0o020 and not sticky and not (gid is not None and meta.st_gid == gid and meta.st_uid in owners)):
                raise ValueError('managed_authority_parent_untrusted')
        leaf = path.name
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptors[-1])
        descriptors.append(fd); before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in owners or before.st_nlink != 1
                or before.st_mode & 0o002 or before.st_mode & 0o020 and not (gid is not None and before.st_gid == gid)
                or mode is not None and stat.S_IMODE(before.st_mode) != mode
                or not (0 if allow_empty else 1) <= before.st_size <= limit):
            raise ValueError('managed_authority_file_untrusted')
        raw = bytearray()
        while len(raw) <= limit:
            block = os.read(fd, min(65536, limit+1-len(raw)))
            if not block: break
            raw.extend(block)
        if len(raw) != before.st_size or _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(os.stat(leaf, dir_fd=descriptors[-2], follow_symlinks=False)):
            raise ValueError('managed_authority_source_changed')
        for index, old in enumerate(before_dirs):
            if _directory_identity(old) != _directory_identity(os.fstat(descriptors[index])):
                raise ValueError('managed_authority_parent_changed')
            if index and _directory_identity(old) != _directory_identity(os.stat(names[index-1], dir_fd=descriptors[index-1], follow_symlinks=False)):
                raise ValueError('managed_authority_parent_replaced')
        identity=(_identity(before),tuple(_directory_identity(meta) for meta in before_dirs))
        return (bytes(raw),identity) if with_identity else bytes(raw)
    finally:
        for fd in reversed(descriptors): os.close(fd)


def _configuration(registry):
    if os.geteuid()!=CONTROL_UID or (USER_MODE and CONTROL_UID<=0):raise ValueError('managed_authority_control_identity_required' if USER_MODE else 'managed_authority_host_root_required')
    value = registry.get('managed_incident_authority')
    expected = {'enabled', 'database_path', 'manifest_path', 'manifest_sha256', 'receipt_directory', 'key_path'}
    if not isinstance(value, dict) or set(value) != expected or value.get('enabled') is not True:
        raise ValueError('managed_authority_not_enabled')
    if (value['database_path'] != DATABASE_PATH or value['manifest_path'] != MANIFEST_PATH
            or value['receipt_directory'] != RECEIPT_DIRECTORY or value['key_path'] != KEY_PATH):
        raise ValueError('managed_authority_configuration_invalid')
    raw = _read(MANIFEST_PATH, limit=1024*1024, mode=0o640)
    if sha256(raw).hexdigest() != _hex(value['manifest_sha256']):
        raise ValueError('managed_authority_manifest_not_reviewed')
    manifest = _json(raw)
    fields = {'schema', 'qualification_domain', 'enabled', 'scopes', 'source_directory', 'producer_files',
        'ingress_sources', 'historical_domain', 'schema_digest', 'previous_manifest_digest', 'release_commit', 'defect_key_semantics'}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest['schema'] != 'managed_code_incident_manifest.v1' or manifest['qualification_domain'] != DOMAIN
            or manifest['defect_key_semantics'] != KEY_SEMANTICS
            or manifest['release_commit'] != registry.get('controller_commit',registry['runtime_commit'])
            or manifest['enabled'] is not True or manifest['ingress_sources'] != list(INGRESS)
            or manifest['historical_domain'] != 'retained_typed_history_and_complete_registered_ingress'
            or not isinstance(manifest['scopes'], list) or not 1 <= len(manifest['scopes']) <= 32
            or len({digest(_scope(scope)) for scope in manifest['scopes']}) != len(manifest['scopes'])
            or not isinstance(manifest['producer_files'], dict)
            or not set(RUNTIME_PRODUCER_FILES) <= set(manifest['producer_files'])
            or not len(PRODUCER_FILES) <= len(manifest['producer_files']) <= 5000
            or any(not isinstance(name,str) or not name.endswith('.py') or name.startswith('/')
                or '\\' in name or '..' in name.split('/') or any(c in name for c in '\r\n\0')
                for name in manifest['producer_files'])):
        raise ValueError('managed_authority_manifest_invalid')
    _hex(manifest['schema_digest'])
    if manifest['previous_manifest_digest'] != '': _hex(manifest['previous_manifest_digest'])
    for value in manifest['producer_files'].values(): _hex(value)
    path = Path(manifest['source_directory'])
    if not path.is_absolute() or '..' in path.parts or str(path) == '/':
        raise ValueError('managed_authority_source_path_invalid')
    key = _read(KEY_PATH, limit=4096, mode=0o600)
    if len(key) < 32: raise ValueError('managed_authority_key_too_short')
    return manifest, key


def validate_configuration(*,registry):
    """Root installer preflight. No proof/Runtime/SQLite/child issuance writes."""
    manifest,_key=_configuration(registry)
    before=_producer_guard(manifest,registry)
    pycache=_empty_pycache()
    if before!=_producer_guard(manifest,registry):raise ValueError('managed_authority_producer_identity_changed')
    return {'qualification_domain':DOMAIN,'manifest_digest':digest(manifest),
        'manifest_sha256':registry['managed_incident_authority']['manifest_sha256'],
        'release_commit':manifest['release_commit'],'scopes':manifest['scopes'],
        'producer_file_count':len(before),'defect_key_semantics':KEY_SEMANTICS,
        'empty_pycache_directory':pycache,'key_material_returned':False}


def python_source_inventory(root):
    """The fixed -I -S worker never searches the candidate virtualenv/cache."""
    root=Path(root); inventory=set()
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if name!='__pycache__'
            and not (Path(directory)==root and name in {'.git','.venv'})]
        for name in dirs:
            if (Path(directory)/name).is_symlink():raise ValueError('managed_authority_source_directory_linked')
        for name in files:
            if name.endswith('.py'):
                inventory.add((Path(directory)/name).relative_to(root).as_posix())
                if len(inventory)>5000:raise ValueError('managed_authority_source_inventory_exceeded')
    return inventory


def _producer_guard(manifest, registry):
    deadline=time.monotonic()+10
    inventory=python_source_inventory(manifest['source_directory'])
    if inventory!=set(manifest['producer_files']):raise ValueError('managed_authority_source_inventory_changed')
    identities={}
    for name, expected in manifest['producer_files'].items():
        if time.monotonic()>deadline:raise ValueError('managed_authority_source_guard_budget_exhausted')
        raw,identity = _read(Path(manifest['source_directory']) / name,
            owners=(0, registry['runtime_uid']), gid=registry['runtime_gid'], limit=4*1024*1024,
            allow_empty=True,with_identity=True)
        if sha256(raw).hexdigest() != expected: raise ValueError('managed_authority_producer_changed')
        identities[name]=identity
    return identities


def _runtime_producer_guard(manifest,registry):
    """No candidate path from the caller and no candidate imports as root.

    A serving candidate may legitimately change repairable .py files. Only the
    protected native acquisition contract must retain reviewed controller bytes.
    Original proof readback intentionally uses _worker, not this new-issue gate.
    """
    runtime=_hex(registry['runtime_commit'],40)
    controller=_hex(registry.get('controller_commit',runtime),40)
    root=Path(manifest['source_directory']) if runtime==controller else Path(RELEASES_DIRECTORY)/runtime
    identities={};deadline=time.monotonic()+10;prefix_modules=set()
    for name in RUNTIME_PRODUCER_FILES:
        shadow=(Path(name[:-3])/'__init__.py').as_posix()
        if not name.endswith('/__init__.py') and shadow not in manifest['producer_files'] and os.path.lexists(root/shadow):
            raise ValueError('managed_authority_runtime_import_shadowed')
        for parent in Path(name).parents:
            if str(parent)=='.':continue
            initializer=(parent/'__init__.py').as_posix()
            if initializer not in manifest['producer_files']:
                if os.path.lexists(root/initializer):
                    raise ValueError('managed_authority_runtime_namespace_injected')
                module=parent.as_posix()+'.py'
                if module in manifest['producer_files']:
                    prefix_modules.add(module)
                elif os.path.lexists(root/module):
                    raise ValueError('managed_authority_runtime_namespace_shadowed')
    for name in (*RUNTIME_PRODUCER_FILES,*sorted(prefix_modules)):
        if name not in manifest['producer_files']:raise ValueError('managed_authority_runtime_contract_incomplete')
        if time.monotonic()>deadline:raise ValueError('managed_authority_runtime_guard_budget_exhausted')
        raw,identity=_read(root/name,owners=(0,registry['runtime_uid']),gid=registry['runtime_gid'],
            allow_empty=True,with_identity=True,limit=4*1024*1024)
        if sha256(raw).hexdigest()!=manifest['producer_files'][name]:
            raise ValueError('managed_authority_runtime_producer_changed')
        identities[name]=identity
    return identities


def _empty_pycache():
    path=Path(PYCACHE_DIRECTORY)
    for index,parent in enumerate((path,*path.parents)):
        meta=parent.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in ({0,CONTROL_UID} if USER_MODE else {0})
                or meta.st_mode&0o022 and not (index>0 and meta.st_mode&stat.S_ISVTX)):
            raise ValueError('managed_authority_pycache_untrusted')
    if any(path.iterdir()):raise ValueError('managed_authority_pycache_not_empty')
    return str(path)


def _worker(manifest, registry, source_id, source_digest, after, cursor_anchor=None):
    before=_producer_guard(manifest, registry)
    pycache=_empty_pycache()
    worker_manifest={key:manifest[key] for key in ('scopes','source_directory','schema_digest')}
    request = {'manifest': worker_manifest, 'runtime_uid': registry['runtime_uid'], 'database_path': DATABASE_PATH,
        'source_record_id': source_id, 'source_digest': source_digest, 'after': after,
        'cursor_anchor':cursor_anchor}
    completed = subprocess.run([* ([] if USER_MODE else ['/usr/sbin/runuser', '-u', registry['runtime_user'], '--']),
        '/usr/bin/python3', '-I', '-S', '-B', '-X', 'pycache_prefix='+pycache,
        str(Path(__file__).absolute()), '--readonly-worker'],
        input=canonical(request), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        env={'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'EIMEMORY_CONFIG_DIR':'/etc/eimemory',
             'PYTHONDONTWRITEBYTECODE':'1', **({'EIMEMORY_CONTROL_PLANE':'user'} if USER_MODE else {})}, check=False)
    if before!=_producer_guard(manifest, registry):raise ValueError('managed_authority_producer_identity_changed')
    _empty_pycache()
    if completed.returncode != 0 or len(completed.stdout) > 16*1024*1024:
        raise ValueError('managed_authority_readonly_worker_failed')
    facts = _json(completed.stdout)
    if (not isinstance(facts, dict) or facts.get('schema') != 'managed_code_physical_facts.v1'
            or facts.get('source_record_id') != source_id or facts.get('source_digest') != source_digest
            or facts.get('after') != after or facts.get('schema_digest') != manifest['schema_digest']
            or facts.get('cursor_anchor')!=cursor_anchor or type(facts.get('cursor_anchor_verified')) is not bool):
        raise ValueError('managed_authority_worker_identity_mismatch')
    return facts


@contextmanager
def _state(registry, manifest):
    root = Path(RECEIPT_DIRECTORY)
    # Bootstrap owns directory creation. Runtime code cannot create authority.
    for index,parent in enumerate((root, *root.parents)):
        meta = parent.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in ({0,CONTROL_UID} if USER_MODE else {0})
                or meta.st_mode & 0o022 and not (index>0 and meta.st_mode&stat.S_ISVTX)):
            raise ValueError('managed_authority_state_directory_untrusted')
    lock_fd = os.open(root / '.authority.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        lock_meta = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != CONTROL_UID or lock_meta.st_nlink != 1 or stat.S_IMODE(lock_meta.st_mode) != 0o600:
            raise ValueError('managed_authority_lock_untrusted')
        import fcntl
        deadline=time.monotonic()+10
        while True:
            try:
                fcntl.flock(lock_fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise ValueError('managed_authority_lock_timeout')
                time.sleep(.025)
        state_path = root / 'knowledge.sqlite'
        fd = os.open(state_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != CONTROL_UID or meta.st_nlink != 1 or stat.S_IMODE(meta.st_mode) != 0o600:
                raise ValueError('managed_authority_registry_untrusted')
        finally: os.close(fd)
        conn = sqlite3.connect(state_path); conn.row_factory = sqlite3.Row
        try:
            conn.executescript('''PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS authority_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS managed_knowledge(owner TEXT NOT NULL,defect_key TEXT NOT NULL,record_id TEXT NOT NULL,
                  source_digest TEXT NOT NULL,source_time TEXT NOT NULL,sequence INTEGER,human_input INTEGER NOT NULL,
                  PRIMARY KEY(owner,record_id,source_digest));
                CREATE INDEX IF NOT EXISTS idx_managed_knowledge_key ON managed_knowledge(owner,defect_key);
                CREATE TABLE IF NOT EXISTS managed_receipts(source_record_id TEXT NOT NULL,source_digest TEXT NOT NULL,
                  receipt_id TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(source_record_id,source_digest));
                CREATE TABLE IF NOT EXISTS managed_manifest_history(digest TEXT PRIMARY KEY);''')
            conn.execute('BEGIN IMMEDIATE')
            token = digest(manifest)
            _prepare_manifest_catchup(conn, manifest)
            conn.execute('INSERT OR IGNORE INTO managed_manifest_history(digest) VALUES(?)', (token,))
            yield conn
            conn.commit()
            _fsync_directory(root)
        except BaseException:
            conn.rollback(); raise
        finally: conn.close()
    finally:
        os.close(lock_fd)


def _fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try: os.fsync(fd)
    finally: os.close(fd)


def _meta_integer(conn, name, *, default=None):
    row=conn.execute('SELECT value FROM authority_meta WHERE key=?',(name,)).fetchone()
    if row is None:
        if default is None:raise ValueError('managed_authority_catchup_state_missing')
        return default
    if not isinstance(row[0],str) or re.fullmatch('0|[1-9][0-9]{0,18}',row[0]) is None:
        raise ValueError('managed_authority_catchup_state_invalid')
    return int(row[0])


def _receive_anchor(conn):
    row=conn.execute("SELECT value FROM authority_meta WHERE key='receive_anchor'").fetchone()
    if row is None:return None
    value=_json(row[0])
    if (not isinstance(value,dict) or set(value)!={'schema','sequence','digest'}
            or value['schema']!='managed_code_receive_anchor.v1'
            or type(value['sequence']) is not int or value['sequence']<1):
        raise ValueError('managed_authority_receive_anchor_invalid')
    _hex(value['digest'])
    if value['sequence']!=_meta_integer(conn,'high_water',default=0):
        raise ValueError('managed_authority_receive_anchor_sequence_changed')
    return value


def _record_anchor_gap(conn, reason):
    # A numeric HWM may catch up after storage rollback; that never repairs the
    # already broken chronology or replaces its original acknowledged anchor.
    conn.execute("INSERT OR IGNORE INTO authority_meta VALUES('uncertainty',?)",(reason,))
    conn.execute("INSERT OR IGNORE INTO authority_meta VALUES('receive_anchor_uncertainty',?)",(reason,))
    conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('catchup_complete','0')")


def _anchor_gap(conn, anchor, high_water):
    row=conn.execute("SELECT value FROM authority_meta WHERE key='receive_anchor_uncertainty'").fetchone()
    if row is not None:
        if row[0] not in {'receive_prefix_anchor_changed','receive_prefix_anchor_unavailable'}:
            raise ValueError('managed_authority_receive_anchor_state_invalid')
        return row[0]
    return 'receive_prefix_anchor_unavailable' if high_water and anchor is None else ''


def _reviewed_manifest_archive():
    """Only completed Root bootstrap histories can bridge skipped checkpoints."""
    archive={}
    try:
        histories=sorted(path for path in Path(CONTROLLER_HISTORY_DIRECTORY).iterdir()
            if re.fullmatch(r'(controller|authority)-[0-9]{8}T[0-9]{6}Z-[0-9]+',path.name))
        if len(histories)>4096:raise ValueError('managed_authority_manifest_archive_limit')
        for history in histories:
            try:complete=_json(_read(history/'complete.json',limit=65536,mode=0o600))
            except FileNotFoundError:continue
            controller=(isinstance(complete,dict) and complete.get('ok') is True
                and re.fullmatch('[0-9a-f]{40}',str(complete.get('controller_commit',''))))
            authority=(isinstance(complete,dict) and
                set(complete)=={'registry_generation','manifest_sha256'})
            if not (controller or authority):
                continue
            _hex(complete.get('registry_generation'));_hex(complete.get('manifest_sha256'))
            for name in ('managed-manifest.snapshot','managed-manifest.original'):
                try:value=_json(_read(history/name,limit=1048576,mode=0o600))
                except FileNotFoundError:continue
                if (not isinstance(value,dict) or value.get('schema')!='managed_code_incident_manifest.v1'
                        or value.get('qualification_domain')!=DOMAIN or value.get('enabled') is not True
                        or value.get('defect_key_semantics')!=KEY_SEMANTICS):
                    continue
                archive[digest(value)]=value
        return archive
    except (OSError,ValueError) as exc:
        raise ValueError('managed_authority_manifest_continuity_unverified') from exc


def _reviewed_manifest_chain(manifest, prior, archive):
    chain=[];seen=set();current=manifest
    for _ in range(128):
        token=digest(current)
        if token in seen:break
        seen.add(token);chain.append(current)
        previous=current.get('previous_manifest_digest')
        if previous==prior:return list(reversed(chain))
        if not isinstance(previous,str) or previous not in archive:break
        current=archive[previous]
        if digest(current)!=previous:break
    raise ValueError('managed_authority_manifest_continuity_unverified')


def _prepare_manifest_catchup(conn, manifest):
    """Re-register a complete history domain without erasing any known fact.

    The positive high water is monotonic. A separate bounded replay cursor is
    reset for every reviewed manifest transition, including newly added scopes.
    Legacy root indexes also replay once instead of assuming their old domain
    was complete under this manifest. All original receipts remain untouched.
    """
    token=digest(manifest)
    prior=conn.execute("SELECT value FROM authority_meta WHERE key='manifest'").fetchone()
    if prior and prior[0]!=token and manifest.get('previous_manifest_digest')!=prior[0]:
        chain=_reviewed_manifest_chain(manifest,prior[0],_reviewed_manifest_archive())
        # The existing transaction and Root authority lock cover the full chain.
        # Register boundaries only: never create facts, receipts or absence proof.
        for intermediate in chain[:-1]:
            _prepare_manifest_catchup(conn,intermediate)
            conn.execute('INSERT OR IGNORE INTO managed_manifest_history(digest) VALUES(?)',(digest(intermediate),))
        prior=conn.execute("SELECT value FROM authority_meta WHERE key='manifest'").fetchone()
    marker=conn.execute("SELECT value FROM authority_meta WHERE key='catchup_manifest'").fetchone()
    if prior and prior[0]==token and marker is not None:
        if marker[0]!=token:raise ValueError('managed_authority_catchup_manifest_mismatch')
        progress=_catchup_progress(conn,manifest)
        if progress['high_water'] and _receive_anchor(conn) is None:
            _record_anchor_gap(conn,'receive_prefix_anchor_unavailable')
        return
    if marker is None and conn.execute("SELECT 1 FROM authority_meta WHERE key IN ('catchup_epoch','catchup_cursor','catchup_complete') LIMIT 1").fetchone():
        raise ValueError('managed_authority_catchup_state_missing')
    if marker is not None and (prior is None or marker[0]!=prior[0]):
        raise ValueError('managed_authority_catchup_manifest_mismatch')
    epoch=_meta_integer(conn,'catchup_epoch',default=0)
    if prior and prior[0]!=token:epoch+=1
    high=_meta_integer(conn,'high_water',default=0)
    if high and _receive_anchor(conn) is None:
        _record_anchor_gap(conn,'receive_prefix_anchor_unavailable')
    for name,value in {'manifest':token,'catchup_manifest':token,'catchup_epoch':str(epoch),
            'catchup_cursor':'0','catchup_complete':'0'}.items():
        conn.execute('INSERT OR REPLACE INTO authority_meta VALUES(?,?)',(name,value))


def _catchup_progress(conn, manifest):
    token=digest(manifest)
    values=dict(conn.execute("SELECT key,value FROM authority_meta WHERE key IN ('manifest','catchup_manifest','catchup_complete')"))
    high=_meta_integer(conn,'high_water',default=0)
    if values.get('manifest')==token and values.get('catchup_manifest') not in {None,token}:
        raise ValueError('managed_authority_catchup_manifest_mismatch')
    if values.get('manifest')!=token or values.get('catchup_manifest')!=token:
        # Verification is read-only and must not initialize a new history domain.
        return {'epoch':None,'cursor':0,'complete':False,'high_water':high}
    epoch=_meta_integer(conn,'catchup_epoch')
    cursor=_meta_integer(conn,'catchup_cursor')
    complete=values.get('catchup_complete')
    if complete not in {'0','1'} or cursor>high:
        raise ValueError('managed_authority_catchup_state_invalid')
    return {'epoch':epoch,'cursor':cursor,'complete':complete=='1','high_water':high}


def _known(conn, facts):
    owner = digest(facts['scope']); source_id = facts['source_record_id']
    rows = conn.execute('SELECT * FROM managed_knowledge WHERE owner=? AND defect_key IN (?,?)', (owner, facts['defect_key'], '*')).fetchall()
    source = [r for r in rows if r['record_id'] == source_id and r['source_digest'] == facts['source_digest'] and r['sequence'] is not None]
    if len(source) != 1: return None, None, None, 'original_receive_sequence_unverified'
    cutoff = source[0]['sequence']; detected = _time(facts['source_created_at'])
    known = False; reported = False
    for row in rows:
        if row['record_id'] == source_id and row['source_digest'] == facts['source_digest']: continue
        if row['sequence'] is None:
            try: prior = _time(row['source_time']) <= detected
            except ValueError: return None, None, cutoff, 'historical_typed_knowledge_time_unknown'
        else:
            prior = row['sequence'] < cutoff
            if row['sequence'] > cutoff:
                try: late = _time(row['source_time']) <= detected
                except ValueError: late = True
                if late: return None, None, cutoff, 'late_prior_typed_knowledge'
        if prior:
            if row['defect_key']=='*':
                return None,None,cutoff,'unclassified_typed_prehistory'
            known = True; reported = reported or bool(row['human_input'])
    return known, reported, cutoff, ''


def _retained_positive_facts(conn, snapshot):
    """Only preserve adverse facts from the independent root positive index.

    A broken runtime chronology cannot erase a previously retained positive.
    It also cannot prove any negative: ambiguous keys, late rows and missing
    original coordinates remain unknown in this projection.
    """
    rows=conn.execute('SELECT * FROM managed_knowledge WHERE owner=? AND defect_key=?',
        (digest(snapshot['scope']),snapshot['defect_key'])).fetchall()
    original=[row for row in rows if row['record_id']==snapshot['source_record_id']
        and row['source_digest']==snapshot['source_digest'] and row['sequence']==snapshot['source_sequence']]
    if len(original)!=1:return None,None
    try:detected=_time(original[0]['source_time'])
    except ValueError:detected=None
    known=reported=None
    for row in rows:
        if row['record_id']==snapshot['source_record_id'] and row['source_digest']==snapshot['source_digest']:continue
        prior=type(row['sequence']) is int and 0<row['sequence']<snapshot['source_sequence']
        if row['sequence'] is None and detected is not None:
            try:prior=_time(row['source_time'])<=detected
            except ValueError:prior=False
        if prior:
            known=True
            if row['human_input']==1:reported=True
    return known,reported


def _ingest(conn, facts, manifest):
    after = facts['after']; through = facts['through']; high = facts['high_water']; rows = facts['rows']
    if (any(type(x) is not int for x in (after, through, high)) or not 0 <= after <= through <= high
            or through-after > 5000 or len(rows) != through-after
            or any(row.get('sequence') != after+i+1 for i,row in enumerate(rows))
            or facts.get('complete') is not (through == high)):
        raise ValueError('managed_authority_worker_prefix_incomplete')
    _prepare_manifest_catchup(conn,manifest)
    progress=_catchup_progress(conn,manifest)
    anchor=_receive_anchor(conn) if progress['high_water'] else None
    if _anchor_gap(conn,anchor,progress['high_water']):
        raise ValueError('managed_authority_receive_anchor_unverified')
    if high<progress['high_water']:
        raise ValueError('managed_authority_receive_high_water_regressed')
    def add(value, seq):
        owner = _scope(value['scope'])
        if owner not in manifest['scopes']: raise ValueError('managed_authority_history_scope_invalid')
        token = value['defect_key']
        if token != '*': _hex(token)
        if type(value['human_input']) is not bool: raise ValueError('managed_authority_human_fact_invalid')
        conn.execute('''INSERT INTO managed_knowledge VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(owner,record_id,source_digest) DO UPDATE SET sequence=coalesce(managed_knowledge.sequence,excluded.sequence)''',
            (digest(owner),token,value['record_id'],value['source_digest'],value['source_time'],seq,int(value['human_input'])))
    for item in facts['history']: add(item, None)
    for row in rows:
        if row['table_name'] == 'coverage_control':
            conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('uncertainty','capture_gap')"); continue
        owner = {k: row[k] for k in SCOPE_FIELDS}
        if owner not in manifest['scopes']: continue
        typed = row['record_kind'] == 'incident' or row['source'] in INGRESS
        if not typed: continue
        if row['table_name'] != 'records' or row['operation'] != 'INSERT' or row['source'] not in INGRESS:
            conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('uncertainty','typed_ingress_mutation_or_unregistered')")
            continue
        try:
            code = _json(row['codes_json'])
            if (not isinstance(code,list) or not code or len(code)>128 or any(not isinstance(x,str)
                    or not x or len(x)>512 or re.fullmatch('[A-Za-z0-9_.:-]+',x) is None
                    or re.search('[0-9a-f]{40}',x) for x in code)): raise ValueError()
            # Same protected cross-release typed key as the application codec.
            if row['incident_class'] not in {'deployment.runtime_commit_drift','release.closure_internal_failure',
                    'release.closure_report_failure','code.system_repair_policy_stale','code.incident_routing_stale'}:
                raise ValueError()
            key = digest({'schema':'managed_code_defect_key.v1','affected_contract':row['incident_class'],
                          'diagnostic_codes':sorted(set(code))})
        except (ValueError, TypeError): key = '*'
        if row['human_input']:key='*'  # no native defect mapping can be inferred from manual prose
        add({'scope':owner,'defect_key':key,'record_id':row['record_id'],'source_digest':row['payload_digest'],
            'source_time':row['source_time'],'human_input':bool(row['human_input'])}, row['sequence'])
    conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('high_water',?)", (str(max(progress['high_water'],through)),))
    conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('catchup_cursor',?)", (str(max(progress['cursor'],through)),))
    conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('catchup_complete',?)", ('1' if facts['complete'] else '0',))
    # Retain the original high-water witness during lower catchup pages. Cold
    # moves preserve this logical row digest exactly; archive physical offsets
    # and current SQL rowids are deliberately absent from the anchor.
    if through>progress['high_water']:
        if not rows or rows[-1]['sequence']!=through:
            raise ValueError('managed_authority_receive_anchor_missing')
        anchor={'schema':'managed_code_receive_anchor.v1','sequence':through,'digest':digest(rows[-1])}
    if anchor is not None:
        conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('receive_anchor',?)",(canonical(anchor).decode(),))


def _activation_facts(facts, activation, registry, peer):
    """A signed unknown remains unknown; no Unit/TriggeredBy inference here."""
    reference = facts.get('activation_reference')
    first=facts.get('first_source_evidence')
    if (not isinstance(first,dict) or set(first)!={'schema','activation_reference','created_at'}
            or first.get('schema')!='managed_native_first_physical_source.v1'
            or first.get('activation_reference')!=reference or first.get('created_at')!=facts.get('source_created_at')
            or not isinstance(first.get('created_at'),str) or re.search(r'T\d\d:\d\d:\d\d\.\d+',first['created_at']) is None):
        return None,'original_first_physical_source_unproven'
    if (not isinstance(activation, dict) or activation.get('schema') != 'eimemory.runtime_activation_attestation.v1'
            or activation.get('signature_verified') is not True or activation.get('reference') != reference
            or activation.get('registry_generation') != registry['generation']
            or activation.get('scope') != facts['scope'] or activation.get('peer_uid') != registry['runtime_uid']
            or type(activation.get('peer_pid')) is not int or activation['peer_pid'] < 1
            or peer.get('uid') != registry['runtime_uid']
            or activation.get('manager') != ('user' if USER_MODE else 'system') or activation.get('unit') not in {'eimemory-learn-watch.service','eimemory-nightly.service','eimemory-release-closure.service'}):
        return None, 'original_activation_unverified'
    if ('runtime_commit' in registry and (activation.get('runtime_commit')!=registry['runtime_commit']
            or activation.get('controller_commit')!=registry.get('controller_commit',registry['runtime_commit']))):
        return None,'original_activation_release_unverified'
    try:
        issued = _time(activation['observed_at']); created = _time(facts['source_created_at']); observed = _time(facts['observed_at'])
        received = _time(facts['source_received_at'])
    except (KeyError, ValueError): return None, 'activation_time_unverified'
    if (not issued <= created <= observed or created > received+timedelta(milliseconds=1)
            or issued > received+timedelta(milliseconds=1) or received > observed
            or (created-issued).total_seconds() > 3600):
        return None, 'activation_source_time_unbound'
    if activation.get('trigger_kind') == 'manual': return True, ''
    if activation.get('trigger_kind') != 'scheduled_timer' or activation.get('job_state') != 'running' or type(activation.get('job_id')) is not int or activation['job_id'] < 1:
        return None, 'activation_cause_unknown'
    return False, ''


def _snapshot(receipt, reference):
    value = {**receipt['snapshot'], 'reference': reference}
    return {**value, 'digest': digest(value)}


def issue(source_record_id, source_digest, *, verified_activation_receipt, registry, peer):
    """No request-controlled path, scope, cause, key, timestamp or negative fact."""
    if not isinstance(source_record_id,str) or re.fullmatch('[A-Za-z0-9_.:-]{1,256}',source_record_id) is None:
        raise ValueError('managed_authority_source_id_invalid')
    _hex(source_digest)
    manifest, key = _configuration(registry)
    with _state(registry,manifest) as conn:
        existing = conn.execute('SELECT * FROM managed_receipts WHERE source_record_id=? AND source_digest=?', (source_record_id,source_digest)).fetchone()
        if existing is not None:
            return {'schema':'managed_code_incident_proof_ref.v1','receipt_id':existing['receipt_id'],'digest':existing['digest']}
        producer_before=_runtime_producer_guard(manifest,registry)
        progress=_catchup_progress(conn,manifest)
        anchor=_receive_anchor(conn)
        anchor_gap=_anchor_gap(conn,anchor,progress['high_water'])
        facts = (_worker(manifest,registry,source_record_id,source_digest,progress['cursor'],anchor)
            if not anchor_gap else {'cursor_anchor_verified':False})
        if producer_before!=_runtime_producer_guard(manifest,registry):
            raise ValueError('managed_authority_runtime_producer_identity_changed')
        if facts.get('cursor_anchor_verified') is False:
            reason=anchor_gap or 'receive_prefix_anchor_changed'
            _record_anchor_gap(conn,reason)
            return {'status':'unproven','reason':reason,'high_water':progress['high_water']}
        _ingest(conn,facts,manifest)
        if not facts['complete']:
            return {'status':'unproven','reason':'managed_history_accumulation_incomplete','high_water':facts['through']}
        known, reported, source_sequence, reason = _known(conn,facts)
        original_activation=verified_activation_receipt
        if callable(original_activation):
            ref=facts.get('activation_reference')
            if (not isinstance(ref,dict) or set(ref)!={'schema','receipt_id','digest'}
                    or ref['schema']!='eimemory.runtime_activation_receipt_ref.v1'):
                original_activation={}
            else:
                original_activation=original_activation(_hex(ref['receipt_id']),_hex(ref['digest']))
        manual, cause_reason = _activation_facts(facts,original_activation,registry,peer)
        uncertain = conn.execute("SELECT value FROM authority_meta WHERE key='uncertainty'").fetchone()
        if uncertain: known=reported=None; reason=uncertain[0]
        if source_sequence is None: return {'status':'unproven','reason':reason}
        globals_ = facts['global_facts']
        if (set(globals_) != set(FIELDS) or any(v is not None and type(v) is not bool for v in globals_.values())
                or any(v is False for v in globals_.values())):
            raise ValueError('managed_authority_global_facts_invalid')
        managed = {'known_before_detection':known,'prior_user_reported':reported,'manual_bootstrap':manual}
        # Positive global declarations are adverse even within the narrow domain.
        managed = {k:True if globals_[k] is True else value for k,value in managed.items()}
        status = 'adverse' if any(v is True for v in managed.values()) else 'proven' if all(v is False for v in managed.values()) else 'unproven'
        snapshot = dict(schema=SCHEMA,qualification_domain=DOMAIN,scope=facts['scope'],source_record_id=source_record_id,
            source_digest=source_digest,incident_id=facts['incident']['incident_id'],incident_digest=facts['incident']['incident_digest'],
            defect_key=facts['defect_key'],activation_reference=facts['activation_reference'],manifest_digest=digest(manifest),
            defect_key_semantics=KEY_SEMANTICS,
            history_watermark=facts['through'],source_sequence=source_sequence,managed_facts=managed,global_facts=globals_,
            external_knowledge=None,external_user_reports=None,status=status,reason=reason or cause_reason)
        receipt = {'schema':'user_managed_code_incident_receipt.v1' if USER_MODE else 'root_managed_code_incident_receipt.v1','snapshot':snapshot,
            'activation':original_activation,'issued_at':datetime.now(timezone.utc).isoformat()}
        signature = hmac.new(key,canonical(receipt),sha256).hexdigest()
        packet = {'payload':receipt,'signature':signature}; raw=canonical(packet)
        receipt_id=digest(receipt); raw_digest=sha256(raw).hexdigest()
        path=Path(RECEIPT_DIRECTORY)/(receipt_id+'.json')
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        except BaseException: raise
        _fsync_directory(path.parent)
        if _read(path,mode=0o600) != raw: raise ValueError('managed_authority_receipt_readback_failed')
        conn.execute('INSERT INTO managed_receipts VALUES(?,?,?,?)',(source_record_id,source_digest,receipt_id,raw_digest))
        return {'schema':'managed_code_incident_proof_ref.v1','receipt_id':receipt_id,'digest':raw_digest}


def checkpoint(*,registry,peer):
    """Explicit positive accumulation, including a healthy no-incident run.

    This creates no incident, causal receipt, negative fact or child authority.
    A readback verifier never invokes this maintenance operation implicitly.
    """
    manifest,_key=_configuration(registry)
    if peer.get('uid')!=registry['runtime_uid']:
        raise ValueError('managed_authority_checkpoint_peer_invalid')
    with _state(registry,manifest) as conn:
        progress=_catchup_progress(conn,manifest)
        producer_before=_runtime_producer_guard(manifest,registry)
        anchor=_receive_anchor(conn)
        anchor_gap=_anchor_gap(conn,anchor,progress['high_water'])
        facts=(_worker(manifest,registry,'','',progress['cursor'],anchor)
            if not anchor_gap else {'cursor_anchor_verified':False,'high_water':None})
        if producer_before!=_runtime_producer_guard(manifest,registry):
            raise ValueError('managed_authority_runtime_producer_identity_changed')
        if facts.get('cursor_anchor_verified') is False:
            reason=anchor_gap or 'receive_prefix_anchor_changed'
            _record_anchor_gap(conn,reason)
            return {'schema':'managed_code_knowledge_checkpoint.v1','qualification_domain':DOMAIN,
                'status':'unproven','reason':reason,'high_water':progress['high_water'],
                'physical_high_water':facts['high_water'],'catchup_epoch':progress['epoch'],
                'catchup_cursor':progress['cursor'],'catchup_complete':False,'catchup_manifest_digest':digest(manifest),
                'qualifies_for_product_completion':False,'incident_created':False,'child_authority_created':False}
        if facts.get('incident') is not None or facts.get('scope') is not None:
            raise ValueError('managed_authority_checkpoint_no_source_expected')
        _ingest(conn,facts,manifest)
        uncertain=conn.execute("SELECT value FROM authority_meta WHERE key='uncertainty'").fetchone()
        progress=_catchup_progress(conn,manifest)
        return {'schema':'managed_code_knowledge_checkpoint.v1','qualification_domain':DOMAIN,
            'status':'unproven' if uncertain else 'ready' if facts['complete'] else 'accumulating',
            'high_water':facts['through'],'physical_high_water':facts['high_water'],
            'catchup_epoch':progress['epoch'],'catchup_cursor':progress['cursor'],
            'catchup_complete':progress['complete'],'catchup_manifest_digest':digest(manifest),
            'reason':uncertain[0] if uncertain else '', 'qualifies_for_product_completion':False,
            'incident_created':False,'child_authority_created':False}


@contextmanager
def _readonly_state(snapshot, manifest):
    """Snapshot only relevant positives into memory; root files remain read-only."""
    root=Path(RECEIPT_DIRECTORY)
    for index,parent in enumerate((root,*root.parents)):
        meta=parent.lstat()
        if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in ({0,CONTROL_UID} if USER_MODE else {0})
                or meta.st_mode&0o022 and not (index>0 and meta.st_mode&stat.S_ISVTX)):
            raise ValueError('managed_authority_state_directory_untrusted')
    path=root/'knowledge.sqlite'
    meta=path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=CONTROL_UID or meta.st_nlink!=1 or stat.S_IMODE(meta.st_mode)!=0o600:
        raise ValueError('managed_authority_registry_untrusted')
    original=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True);original.row_factory=sqlite3.Row
    memory=sqlite3.connect(':memory:');memory.row_factory=sqlite3.Row
    try:
        original.execute('PRAGMA query_only=ON');original.execute('BEGIN')
        original.set_authorizer(lambda action,_a,_b,_db,_trigger:sqlite3.SQLITE_OK if action in {
            sqlite3.SQLITE_SELECT,sqlite3.SQLITE_READ,sqlite3.SQLITE_FUNCTION,sqlite3.SQLITE_TRANSACTION} else sqlite3.SQLITE_DENY)
        for table in ('authority_meta','managed_knowledge','managed_receipts','managed_manifest_history'):
            sql=original.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()
            if sql is None:raise ValueError('managed_authority_registry_schema_missing')
            # Execute only the fixed schema, never SQL fetched from a database.
        memory.executescript('''CREATE TABLE authority_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE managed_knowledge(owner TEXT NOT NULL,defect_key TEXT NOT NULL,record_id TEXT NOT NULL,
              source_digest TEXT NOT NULL,source_time TEXT NOT NULL,sequence INTEGER,human_input INTEGER NOT NULL,
              PRIMARY KEY(owner,record_id,source_digest));
            CREATE TABLE managed_receipts(source_record_id TEXT NOT NULL,source_digest TEXT NOT NULL,
              receipt_id TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(source_record_id,source_digest));
            CREATE TABLE managed_manifest_history(digest TEXT PRIMARY KEY);''')
        for row in original.execute('SELECT key,value FROM authority_meta'):
            memory.execute('INSERT INTO authority_meta VALUES(?,?)',tuple(row))
        rows=original.execute('SELECT * FROM managed_knowledge WHERE owner=? AND defect_key IN (?,?) LIMIT 100001',
            (digest(snapshot['scope']),snapshot['defect_key'],'*')).fetchall()
        if len(rows)>100000:raise ValueError('managed_authority_positive_read_bound_exceeded')
        memory.executemany('INSERT INTO managed_knowledge VALUES(?,?,?,?,?,?,?)',[tuple(row) for row in rows])
        row=original.execute('SELECT * FROM managed_receipts WHERE source_record_id=? AND source_digest=?',
            (snapshot['source_record_id'],snapshot['source_digest'])).fetchone()
        if row is not None:memory.execute('INSERT INTO managed_receipts VALUES(?,?,?,?)',tuple(row))
        for token in (snapshot['manifest_digest'],digest(manifest)):
            row=original.execute('SELECT digest FROM managed_manifest_history WHERE digest=?',(token,)).fetchone()
            if row is not None:memory.execute('INSERT OR IGNORE INTO managed_manifest_history VALUES(?)',tuple(row))
        yield memory
    finally:
        memory.close();original.close()


def verify(receipt_id, digest_value, *, registry):
    _hex(receipt_id);_hex(digest_value)
    manifest,key=_configuration(registry)
    raw=_read(Path(RECEIPT_DIRECTORY)/(receipt_id+'.json'),mode=0o600)
    if sha256(raw).hexdigest()!=digest_value:raise ValueError('managed_authority_receipt_digest_changed')
    packet=_json(raw);receipt=packet.get('payload')
    if (set(packet)!={'payload','signature'} or not isinstance(receipt,dict) or digest(receipt)!=receipt_id
            or receipt.get('schema') not in ({'root_managed_code_incident_receipt.v1','user_managed_code_incident_receipt.v1'} if USER_MODE else {'root_managed_code_incident_receipt.v1'})
            or not hmac.compare_digest(hmac.new(key,canonical(receipt),sha256).hexdigest(),str(packet['signature']))):
        raise ValueError('managed_authority_receipt_signature_invalid')
    snapshot=receipt['snapshot']
    with _readonly_state(snapshot,manifest) as conn:
        original=conn.execute('SELECT * FROM managed_receipts WHERE source_record_id=? AND source_digest=?',
            (snapshot['source_record_id'],snapshot['source_digest'])).fetchone()
        if original is None or original['receipt_id']!=receipt_id or original['digest']!=digest_value:
            raise ValueError('managed_authority_original_registry_mismatch')
        if conn.execute('SELECT 1 FROM managed_manifest_history WHERE digest=?',(snapshot['manifest_digest'],)).fetchone() is None:
            raise ValueError('managed_authority_manifest_history_missing')
        progress=_catchup_progress(conn,manifest)
        anchor=_receive_anchor(conn)
        anchor_gap=_anchor_gap(conn,anchor,progress['high_water'])
        facts=(_worker(manifest,registry,snapshot['source_record_id'],snapshot['source_digest'],progress['cursor'],anchor)
            if not anchor_gap else {'cursor_anchor_verified':False})
        if facts.get('cursor_anchor_verified') is False:
            # The immutable host-root receipt still authenticates its original
            # capture. A read-only verifier cannot heal a replaced receive
            # prefix, and grants no negative current knowledge in this case.
            original=_snapshot(receipt,{'schema':'managed_code_incident_proof_ref.v1','receipt_id':receipt_id,'digest':digest_value})
            retained=dict(zip(('known_before_detection','prior_user_reported'),_retained_positive_facts(conn,snapshot)))
            current={key:True if snapshot['global_facts'][key] is True or snapshot['managed_facts'][key] is True or retained.get(key) is True
                     else snapshot['managed_facts'][key] if key=='manual_bootstrap' else None for key in FIELDS}
            return {'schema':'managed_code_incident_proof_verification.v1','snapshot':original,
                'qualification_current':False,'current_managed_facts':current,
                'current_watermark':max(progress['high_water'],snapshot['history_watermark']),
                'reason':anchor_gap or 'receive_prefix_anchor_changed'}
        if (facts['scope']!=snapshot['scope'] or facts['defect_key']!=snapshot['defect_key']
                or facts['incident']['incident_id']!=snapshot['incident_id']
                or facts['incident']['incident_digest']!=snapshot['incident_digest']
                or facts['global_facts']!=snapshot['global_facts']
                or facts['activation_reference']!=snapshot['activation_reference']):
            raise ValueError('managed_authority_original_source_identity_changed')
        _ingest(conn,facts,manifest)
        known,reported,sequence,reason=_known(conn,facts)
        uncertain=conn.execute("SELECT value FROM authority_meta WHERE key='uncertainty'").fetchone()
        if uncertain:known=reported=None;reason=uncertain[0]
        if facts['global_facts']['known_before_detection'] is True:known=True
        if facts['global_facts']['prior_user_reported'] is True:reported=True
        if sequence!=snapshot['source_sequence']:raise ValueError('managed_authority_original_sequence_changed')
        if not progress['complete']:
            known=True if known is True else None
            reported=True if reported is True else None
            reason='managed_history_manifest_catchup_incomplete'
        elif not facts['complete']:
            known=True if known is True else None
            reported=True if reported is True else None
            reason='managed_history_accumulation_incomplete'
        current_facts={'known_before_detection':known,'prior_user_reported':reported,
            'manual_bootstrap':snapshot['managed_facts']['manual_bootstrap']}
        original=_snapshot(receipt,{'schema':'managed_code_incident_proof_ref.v1','receipt_id':receipt_id,'digest':digest_value})
        return {'schema':'managed_code_incident_proof_verification.v1','snapshot':original,
            'qualification_current':snapshot['status']=='proven' and all(value is False for value in current_facts.values()),
            'current_managed_facts':current_facts,
            'current_watermark':max(progress['high_water'],facts['through']), 'reason':reason}


def _readonly_worker():
    raw=sys.stdin.buffer.read(65537)
    if len(raw)>65536:raise ValueError('managed_authority_worker_request_too_large')
    value=_json(raw)
    if os.geteuid()!=value['runtime_uid'] or value['runtime_uid']<1 or value['database_path']!=DATABASE_PATH:
        raise ValueError('managed_authority_worker_uid_invalid')
    # The root caller verified raw producer bytes before and after this worker.
    sys.path.insert(0,value['manifest']['source_directory'])
    from eimemory.governance.evolution.managed_code_incident_proof import collect_physical_facts
    facts=collect_physical_facts(value['database_path'],manifest=value['manifest'],
        source_record_id=value['source_record_id'],source_digest=value['source_digest'],after=value['after'],
        cursor_anchor=value.get('cursor_anchor'))
    sys.stdout.buffer.write(canonical(facts))


if __name__=='__main__':
    if sys.argv[1:]!=['--readonly-worker']:raise SystemExit('fixed readonly worker only')
    _readonly_worker()
