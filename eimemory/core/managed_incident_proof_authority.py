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

DOMAIN = 'registered_code_defect_knowledge.v1'
SCHEMA = 'managed_code_incident_proof.v1'
MANIFEST_PATH = '/etc/eimemory-code-automation-authority/managed-code-incident-manifest.v1.json'
DATABASE_PATH = '/var/lib/eimemory/state/eimemory.sqlite'
KEY_PATH = '/etc/eimemory-code-automation-authority/managed-incident-proof.key'
RECEIPT_DIRECTORY = '/var/lib/eimemory-manager/managed-incident-receipts'
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


def _read(path, *, owners=(0,), gid=None, limit=16*1024*1024, mode=None):
    """No-follow every component, with descriptor/name identity revalidation."""
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
        for meta in before_dirs:
            if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid not in owners or meta.st_mode & 0o002
                    or meta.st_mode & 0o020 and not (gid is not None and meta.st_gid == gid and meta.st_uid in owners)):
                raise ValueError('managed_authority_parent_untrusted')
        leaf = path.name
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=descriptors[-1])
        descriptors.append(fd); before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in owners or before.st_nlink != 1
                or before.st_mode & 0o002 or before.st_mode & 0o020 and not (gid is not None and before.st_gid == gid)
                or mode is not None and stat.S_IMODE(before.st_mode) != mode or not 0 < before.st_size <= limit):
            raise ValueError('managed_authority_file_untrusted')
        raw = bytearray()
        while len(raw) <= limit:
            block = os.read(fd, min(65536, limit+1-len(raw)))
            if not block: break
            raw.extend(block)
        if len(raw) != before.st_size or _identity(before) != _identity(os.fstat(fd)) or _identity(before) != _identity(os.stat(leaf, dir_fd=descriptors[-2], follow_symlinks=False)):
            raise ValueError('managed_authority_source_changed')
        for index, old in enumerate(before_dirs):
            if _identity(old) != _identity(os.fstat(descriptors[index])):
                raise ValueError('managed_authority_parent_changed')
            if index and _identity(old) != _identity(os.stat(names[index-1], dir_fd=descriptors[index-1], follow_symlinks=False)):
                raise ValueError('managed_authority_parent_replaced')
        return bytes(raw)
    finally:
        for fd in reversed(descriptors): os.close(fd)


def _configuration(registry):
    if os.geteuid() != 0: raise ValueError('managed_authority_host_root_required')
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
        'ingress_sources', 'historical_domain', 'schema_digest', 'previous_manifest_digest'}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest['schema'] != 'managed_code_incident_manifest.v1' or manifest['qualification_domain'] != DOMAIN
            or manifest['enabled'] is not True or manifest['ingress_sources'] != list(INGRESS)
            or manifest['historical_domain'] != 'retained_typed_history_and_complete_registered_ingress'
            or not isinstance(manifest['scopes'], list) or not 1 <= len(manifest['scopes']) <= 32
            or len({digest(_scope(scope)) for scope in manifest['scopes']}) != len(manifest['scopes'])
            or not isinstance(manifest['producer_files'], dict)
            or not set(PRODUCER_FILES) <= set(manifest['producer_files'])
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


def _producer_guard(manifest, registry):
    root=Path(manifest['source_directory']); inventory=set()
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if name!='.git']
        for name in dirs:
            if (Path(directory)/name).is_symlink():raise ValueError('managed_authority_source_directory_linked')
        for name in files:
            if name.endswith('.py'):
                inventory.add((Path(directory)/name).relative_to(root).as_posix())
                if len(inventory)>5000:raise ValueError('managed_authority_source_inventory_exceeded')
    if inventory!=set(manifest['producer_files']):raise ValueError('managed_authority_source_inventory_changed')
    for name, expected in manifest['producer_files'].items():
        raw = _read(Path(manifest['source_directory']) / name,
            owners=(0, registry['runtime_uid']), gid=registry['runtime_gid'], limit=4*1024*1024)
        if sha256(raw).hexdigest() != expected: raise ValueError('managed_authority_producer_changed')


def _worker(manifest, registry, source_id, source_digest, after):
    _producer_guard(manifest, registry)
    worker_manifest={key:manifest[key] for key in ('scopes','source_directory','schema_digest')}
    request = {'manifest': worker_manifest, 'runtime_uid': registry['runtime_uid'], 'database_path': DATABASE_PATH,
        'source_record_id': source_id, 'source_digest': source_digest, 'after': after}
    completed = subprocess.run(['/usr/sbin/runuser', '-u', registry['runtime_user'], '--',
        '/usr/bin/python3', '-I', '-B', str(Path(__file__).absolute()), '--readonly-worker'],
        input=canonical(request), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        env={'PATH':'/usr/bin:/bin', 'LANG':'C.UTF-8', 'EIMEMORY_CONFIG_DIR':'/etc/eimemory',
             'PYTHONDONTWRITEBYTECODE':'1'}, check=False)
    _producer_guard(manifest, registry)
    if completed.returncode != 0 or len(completed.stdout) > 16*1024*1024:
        raise ValueError('managed_authority_readonly_worker_failed')
    facts = _json(completed.stdout)
    if (not isinstance(facts, dict) or facts.get('schema') != 'managed_code_physical_facts.v1'
            or facts.get('source_record_id') != source_id or facts.get('source_digest') != source_digest
            or facts.get('after') != after or facts.get('schema_digest') != manifest['schema_digest']):
        raise ValueError('managed_authority_worker_identity_mismatch')
    return facts


@contextmanager
def _state(registry, manifest):
    root = Path(RECEIPT_DIRECTORY)
    # Bootstrap owns directory creation. Runtime code cannot create authority.
    for parent in (root, *root.parents):
        meta = parent.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
            raise ValueError('managed_authority_state_directory_untrusted')
    lock_fd = os.open(root / '.authority.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        lock_meta = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_meta.st_mode) or lock_meta.st_uid != 0 or lock_meta.st_nlink != 1 or stat.S_IMODE(lock_meta.st_mode) != 0o600:
            raise ValueError('managed_authority_lock_untrusted')
        import fcntl
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        state_path = root / 'knowledge.sqlite'
        fd = os.open(state_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            meta = os.fstat(fd)
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 0 or meta.st_nlink != 1 or stat.S_IMODE(meta.st_mode) != 0o600:
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
            prior = conn.execute("SELECT value FROM authority_meta WHERE key='manifest'").fetchone()
            token = digest(manifest)
            if prior and prior[0] != token and manifest['previous_manifest_digest'] != prior[0]:
                raise ValueError('managed_authority_manifest_continuity_unverified')
            conn.execute('INSERT OR IGNORE INTO managed_manifest_history(digest) VALUES(?)', (token,))
            conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('manifest',?)", (token,))
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
            known = True; reported = reported or bool(row['human_input'])
    return known, reported, cutoff, ''


def _ingest(conn, facts, manifest):
    after = facts['after']; through = facts['through']; high = facts['high_water']; rows = facts['rows']
    if (any(type(x) is not int for x in (after, through, high)) or not 0 <= after <= through <= high
            or through-after > 5000 or len(rows) != through-after
            or any(row.get('sequence') != after+i+1 for i,row in enumerate(rows))
            or facts.get('complete') is not (through == high)):
        raise ValueError('managed_authority_worker_prefix_incomplete')
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
        add({'scope':owner,'defect_key':key,'record_id':row['record_id'],'source_digest':row['payload_digest'],
            'source_time':row['source_time'],'human_input':bool(row['human_input'])}, row['sequence'])
    conn.execute("INSERT OR REPLACE INTO authority_meta VALUES('high_water',?)", (str(through),))


def _activation_facts(facts, activation, registry, peer):
    """A signed unknown remains unknown; no Unit/TriggeredBy inference here."""
    reference = facts.get('activation_reference')
    if (not isinstance(activation, dict) or activation.get('schema') != 'eimemory.runtime_activation_attestation.v1'
            or activation.get('signature_verified') is not True or activation.get('reference') != reference
            or activation.get('registry_generation') != registry['generation']
            or activation.get('scope') != facts['scope'] or activation.get('peer_uid') != registry['runtime_uid']
            or type(activation.get('peer_pid')) is not int or activation['peer_pid'] < 1
            or peer.get('uid') != registry['runtime_uid']
            or activation.get('manager') != 'system' or activation.get('unit') not in {'eimemory-learn-watch.service','eimemory-nightly.service','eimemory-release-closure.service'}):
        return None, 'original_activation_unverified'
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
        row = conn.execute("SELECT value FROM authority_meta WHERE key='high_water'").fetchone()
        facts = _worker(manifest,registry,source_record_id,source_digest,int(row[0]) if row else 0)
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
            history_watermark=facts['through'],source_sequence=source_sequence,managed_facts=managed,global_facts=globals_,
            external_knowledge=None,external_user_reports=None,status=status,reason=reason or cause_reason)
        receipt = {'schema':'root_managed_code_incident_receipt.v1','snapshot':snapshot,
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


@contextmanager
def _readonly_state(snapshot, manifest):
    """Snapshot only relevant positives into memory; root files remain read-only."""
    root=Path(RECEIPT_DIRECTORY)
    for parent in (root,*root.parents):
        meta=parent.lstat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=0 or meta.st_mode&0o022:
            raise ValueError('managed_authority_state_directory_untrusted')
    path=root/'knowledge.sqlite'
    meta=path.lstat()
    if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0 or meta.st_nlink!=1 or stat.S_IMODE(meta.st_mode)!=0o600:
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
        row=conn.execute("SELECT value FROM authority_meta WHERE key='high_water'").fetchone()
        facts=_worker(manifest,registry,snapshot['source_record_id'],snapshot['source_digest'],int(row[0]))
        if (facts['scope']!=snapshot['scope'] or facts['defect_key']!=snapshot['defect_key']
                or facts['incident']['incident_id']!=snapshot['incident_id']
                or facts['incident']['incident_digest']!=snapshot['incident_digest']
                or facts['global_facts']!=snapshot['global_facts']
                or facts['activation_reference']!=snapshot['activation_reference']):
            raise ValueError('managed_authority_original_source_identity_changed')
        _ingest(conn,facts,manifest)
        if not facts['complete']:raise ValueError('managed_authority_readback_prefix_incomplete')
        known,reported,sequence,reason=_known(conn,facts)
        if facts['global_facts']['known_before_detection'] is True:known=True
        if facts['global_facts']['prior_user_reported'] is True:reported=True
        if (conn.execute("SELECT 1 FROM authority_meta WHERE key='uncertainty'").fetchone()
                or sequence!=snapshot['source_sequence'] or reason
                or known is not snapshot['managed_facts']['known_before_detection']
                or reported is not snapshot['managed_facts']['prior_user_reported']):
            raise ValueError('managed_authority_historical_knowledge_changed')
    return _snapshot(receipt,{'schema':'managed_code_incident_proof_ref.v1','receipt_id':receipt_id,'digest':digest_value})


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
        source_record_id=value['source_record_id'],source_digest=value['source_digest'],after=value['after'])
    sys.stdout.buffer.write(canonical(facts))


if __name__=='__main__':
    if sys.argv[1:]!=['--readonly-worker']:raise SystemExit('fixed readonly worker only')
    _readonly_worker()
