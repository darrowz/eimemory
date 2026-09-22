"""Optional operator-managed evidence catalog in the existing authority database.

Only the explicit local governance CLI installs/writes this schema. Online
recall opens a separate read-only connection. Ordinary record metadata never
confers approval. Business-partition clocks invalidate even unseen additions;
graph/alias changes invalidate all contracts. Approval is a HUMAN semantic
attestation, not a claim that deterministic code understands arbitrary prose.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from math import isfinite
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from eimemory.retrieval.evidence_query import POLICY as QUERY_POLICY, label

SCHEMA = 'independent-evidence-catalog.v1'
PROPOSAL = 'independent-evidence-proposal.v1'
PROJECTION = 'keyword-16000-caller-768-fragment-v2'
SCOPE_FIELDS = ('tenant_id', 'agent_id', 'workspace_id', 'user_id')
# Normative records/facts, not observation/audit rows written on every recall.
BUSINESS_KINDS = ('memory', 'rule', 'sop', 'knowledge_page', 'claim_card',
                  'entity_record', 'relation_record', 'knowledge_unit', 'intent_pattern')
GUARD_TABLES = ('memory_edges', 'recall_alias_index')
ATTESTATIONS = ('original_supports_slot', 'complete_answer',
                'unconditional_for_grammar', 'partition_conflicts_reviewed')
MAX_CONTRACTS = 4096
MAX_PACKET_BYTES = 16384
MAX_LIFETIME = 7 * 86400


class CatalogError(ValueError):
    """Only constant error codes, no packet, SQL, source text, or exceptions."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def text_digest(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def strict_json(raw: str | bytes):
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise CatalogError('duplicate_key')
            out[key] = value
        return out
    def constant(_):
        raise CatalogError('nonfinite_value')
    def finite_float(token):
        number = float(token)
        if not isfinite(number):
            raise CatalogError('nonfinite_value')
        return number
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant,
                      parse_float=finite_float)


def scope_tuple(scope) -> tuple[str, ...]:
    if not isinstance(scope, dict) or set(scope) != set(SCOPE_FIELDS):
        raise CatalogError('exact_scope_required')
    values = tuple(scope[k] for k in SCOPE_FIELDS)
    if any(not isinstance(v, str) or len(v) > 256 or '\x00' in v for v in values):
        raise CatalogError('scope_invalid')
    if not values[0]:
        raise CatalogError('tenant_required')
    return values


def db_path(root) -> Path:
    # pathlib.Path.root is a filesystem anchor, not RuntimeStore.root.
    if not isinstance(root, (str, os.PathLike)):
        root = root.root
    path = Path(root).resolve() / 'state' / 'eimemory.sqlite'
    if not path.is_file():
        raise CatalogError('authority_missing')
    return path


@contextmanager
def connect(root, *, writable=False, deadline=0.0):
    remaining = max(0.0, deadline - time.perf_counter()) if deadline else .025
    if deadline and remaining <= 0:
        raise CatalogError('local_deadline')
    path = db_path(root)
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                           uri=True, timeout=min(.025, remaining), isolation_level=None)
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        if not writable:
            conn.execute('PRAGMA query_only=ON')
        if deadline:
            conn.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 100)
        yield conn
    finally:
        conn.close()


def _tables(conn) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def definitions(guards):
    scope = ','.join(k + ' TEXT NOT NULL' for k in SCOPE_FIELDS)
    keys = ','.join(SCOPE_FIELDS)
    yield 'ie_v1_state', ('CREATE TABLE ie_v1_state (id INTEGER PRIMARY KEY CHECK(id=1), '
        'schema_id TEXT NOT NULL, epoch INTEGER NOT NULL, graph_epoch INTEGER NOT NULL)')
    yield 'ie_v1_clock', (f'CREATE TABLE ie_v1_clock ({scope}, revision INTEGER NOT NULL, '
        f'PRIMARY KEY({keys}))')
    yield 'ie_v1_contract', (f'CREATE TABLE ie_v1_contract (contract_id TEXT PRIMARY KEY, {scope}, '
        'source_id TEXT NOT NULL, record_id TEXT NOT NULL, status TEXT NOT NULL '
        "CHECK(status IN ('approved','revoked')), expires_at INTEGER NOT NULL, "
        'packet_json TEXT NOT NULL, packet_digest TEXT NOT NULL, reviewer TEXT NOT NULL, '
        'review_receipt TEXT NOT NULL)')
    yield 'ie_v1_alias', (f'CREATE TABLE ie_v1_alias ({scope}, intent TEXT NOT NULL, '
        'subject TEXT NOT NULL, attribute TEXT NOT NULL, contract_id TEXT NOT NULL '
        'REFERENCES ie_v1_contract(contract_id), '
        f'PRIMARY KEY({keys},intent,subject,attribute))')
    yield 'ie_v1_event', ('CREATE TABLE ie_v1_event (sequence INTEGER PRIMARY KEY AUTOINCREMENT, '
        'contract_id TEXT NOT NULL, action TEXT NOT NULL, actor TEXT NOT NULL, '
        'packet_digest TEXT NOT NULL, occurred_at INTEGER NOT NULL)')
    kinds = ','.join("'" + k + "'" for k in BUSINESS_KINDS)
    for op in ('INSERT', 'UPDATE', 'DELETE'):
        refs = ('NEW',) if op == 'INSERT' else ('OLD',) if op == 'DELETE' else ('OLD', 'NEW')
        statements = []
        for ref in refs:
            values = ','.join(ref + '.' + k for k in SCOPE_FIELDS)
            statements.append(f'INSERT INTO ie_v1_clock({keys},revision) SELECT {values},1 '
                f'WHERE {ref}.kind IN ({kinds}) ON CONFLICT({keys}) '
                'DO UPDATE SET revision=revision+1;')
        when = ' OR '.join(f'{ref}.kind IN ({kinds})' for ref in refs)
        name = 'ie_v1_records_' + op.lower()
        yield name, (f'CREATE TRIGGER {name} AFTER {op} ON records WHEN {when} BEGIN '
            + ' '.join(statements) + ' UPDATE ie_v1_state SET epoch=epoch+1 WHERE id=1; END')
    for table in guards:
        for op in ('INSERT', 'UPDATE', 'DELETE'):
            name = f'ie_v1_{table}_{op.lower()}'
            yield name, (f'CREATE TRIGGER {name} AFTER {op} ON {table} BEGIN '
                'UPDATE ie_v1_state SET epoch=epoch+1,graph_epoch=graph_epoch+1 WHERE id=1; END')


def validate_schema(conn):
    expected = dict(definitions(sorted(_tables(conn) & set(GUARD_TABLES))))
    actual = dict(conn.execute("SELECT name,sql FROM sqlite_master WHERE name LIKE 'ie_v1_%'"))
    clean = lambda sql: ' '.join(sql.strip().split())
    if any(name not in actual or clean(actual[name]) != clean(sql) for name, sql in expected.items()):
        raise CatalogError('catalog_schema_unavailable')
    state = conn.execute('SELECT schema_id,epoch,graph_epoch FROM ie_v1_state WHERE id=1').fetchone()
    if state is None or len(state[0]) != 32:
        raise CatalogError('catalog_schema_unavailable')
    return state


def install(conn):
    """Explicit, transactional, additive migration; never rewrite authority rows."""
    if conn.in_transaction:
        raise CatalogError('nested_transaction')
    if 'records' not in _tables(conn):
        raise CatalogError('authority_missing')
    conn.execute('BEGIN IMMEDIATE')
    try:
        if 'ie_v1_state' in _tables(conn):
            validate_schema(conn)  # Idempotent only for an intact schema.
        else:
            for _, sql in definitions(sorted(_tables(conn) & set(GUARD_TABLES))):
                conn.execute(sql)
            conn.execute('INSERT INTO ie_v1_state VALUES(1,?,0,0)', (uuid4().hex,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def stamp(conn, scope):
    state = validate_schema(conn)
    row = conn.execute('SELECT revision FROM ie_v1_clock WHERE ' +
        ' AND '.join(k + '=?' for k in SCOPE_FIELDS), scope_tuple(scope)).fetchone()
    return {'schema_id': state[0], 'partition_revision': row[0] if row else 0,
            'graph_epoch': state[2]}


def _authority_row(conn, scope, source_id, record_id):
    rows = conn.execute('SELECT kind,status,payload_json,payload_pointer_json,payload_digest,updated_at '
        'FROM records WHERE ' + ' AND '.join(k + '=?' for k in SCOPE_FIELDS) +
        ' AND source_id=? AND record_id=? LIMIT 2',
        (*scope_tuple(scope), source_id, record_id)).fetchall()
    if len(rows) != 1 or rows[0][0] not in ('memory', 'rule') or rows[0][1] != 'active':
        raise CatalogError('authority_not_unique_active')
    return rows[0]


def _default_projection(data):
    from eimemory.models.records import RecordEnvelope
    from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
    return candidate_record_keyword_text(RecordEnvelope.from_dict(data), max_text_chars=16000)


def review_fragments(conn, reference, *, project=None):
    """Private review material; not an answer response or approval receipt."""
    from eimemory.retrieval.evidence_fragments import evidence_fragments
    if not isinstance(reference,dict) or set(reference)!={'scope','source_id','record_id'}:
        raise CatalogError('reference_invalid')
    before=stamp(conn,reference['scope'])
    row=_authority_row(conn,reference['scope'],reference['source_id'],reference['record_id'])
    if row[3]:
        raise CatalogError('segmented_authority_requires_review_adapter')
    data=strict_json(row[2])
    text=(project or _default_projection)(data)
    if stamp(conn,reference['scope'])!=before:
        raise CatalogError('authority_changed')
    return {**reference,'schema':'independent-evidence-review.v1','authority_stamp':before,
            'fragments':evidence_fragments(text),'boundary':'private_review_not_approval'}


def prepare(conn, spec, *, now=None, project=None):
    """Freeze a PRIVATE review packet. Caller must review the original fragment.

    `project` is only a test injection point; CLI always uses the real repository
    projection. No inference, inferred alias, or approval is performed here.
    """
    from eimemory.retrieval.evidence_fragments import evidence_fragments
    now = int(time.time()) if now is None else int(now)
    allowed = {'scope','source_id','record_id','intent','subject','attribute','aliases',
               'fragment_id','expires_at'}
    if not isinstance(spec, dict) or set(spec) != allowed:
        raise CatalogError('spec_fields_invalid')
    scope_tuple(spec['scope'])
    if spec['intent'] not in ('procedure','fact'):
        raise CatalogError('intent_invalid')
    if spec['attribute'] not in (('',) if spec['intent']=='procedure' else ('地址','联系人','联系方式')):
        raise CatalogError('attribute_invalid')
    subject = label(spec['subject'])
    if (not isinstance(spec['aliases'], list) or len(spec['aliases']) > 8
            or any(not isinstance(x, str) for x in spec['aliases'])):
        raise CatalogError('aliases_invalid')
    aliases = sorted({subject, *(label(x) for x in spec['aliases'])})
    for k in ('source_id','record_id','fragment_id'):
        if not isinstance(spec[k], str) or not 1 <= len(spec[k]) <= 256:
            raise CatalogError('reference_invalid')
    expires = spec['expires_at']
    if type(expires) is not int or not now < expires <= now + MAX_LIFETIME:
        raise CatalogError('expiry_invalid')
    before = stamp(conn, spec['scope'])
    row = _authority_row(conn, spec['scope'], spec['source_id'], spec['record_id'])
    if row[3]:
        raise CatalogError('segmented_authority_requires_review_adapter')
    data = strict_json(row[2])
    if (data.get('scope') != spec['scope'] or data.get('source_id','default') != spec['source_id']
            or data.get('record_id') != spec['record_id'] or data.get('kind') != row[0]
            or data.get('status','active') != 'active'):
        raise CatalogError('authority_payload_identity_invalid')
    text = (project or _default_projection)(data)
    fragment = next((f for f in evidence_fragments(text) if f['id']==spec['fragment_id']), None)
    if not fragment or not 4 <= len(fragment['text'].strip()) <= 512:
        raise CatalogError('fragment_invalid')
    # Subject aliases require authority-owned literal support, not a question list.
    owned = {label(a) for a in data.get('aliases', []) if isinstance(a,str)
             and a.strip() and len(a)<=80 and _label_ok(a)}
    folded = label_text(fragment['text'])
    if any(a not in folded and a not in owned for a in aliases):
        raise CatalogError('alias_not_authority_owned')
    if spec['intent']=='fact' and spec['attribute'] not in fragment['text']:
        raise CatalogError('attribute_not_in_original')
    after = stamp(conn, spec['scope'])
    if before != after:
        raise CatalogError('authority_changed')
    packet = {**spec, 'subject':subject, 'aliases':aliases, 'schema':PROPOSAL,
        'query_policy':QUERY_POLICY, 'projection_policy':PROJECTION, 'created_at':now,
        'authority_stamp':before, 'row_digest':digest(row),
        'prefix_digest':text_digest(text[:768]), 'fragment':fragment}
    if len(canonical(packet).encode()) > MAX_PACKET_BYTES:
        raise CatalogError('packet_bound')
    return packet


def label_text(text):
    from eimemory.retrieval.evidence_query import normalized
    return normalized(text)


def _label_ok(value):
    try:
        label(value)
        return True
    except ValueError:
        return False


def packet_valid(conn, packet, *, now=None):
    now = int(time.time()) if now is None else int(now)
    if (packet.get('schema') != PROPOSAL or packet.get('query_policy') != QUERY_POLICY
            or packet.get('projection_policy') != PROJECTION
            or not packet['created_at'] <= now < packet['expires_at']
            or packet['expires_at'] - packet['created_at'] > MAX_LIFETIME
            or stamp(conn, packet['scope']) != packet['authority_stamp']):
        raise CatalogError('contract_stale')
    row = _authority_row(conn, packet['scope'], packet['source_id'], packet['record_id'])
    if digest(row) != packet['row_digest']:
        raise CatalogError('authority_changed')


def approve(conn, packet, *, expected_digest, reviewer, review_receipt, attestations,
            now=None, project=None):
    """Operator-only CAS approval; hashes identify review, not semantic truth."""
    now = int(time.time()) if now is None else int(now)
    if (digest(packet) != expected_digest or not isinstance(reviewer, str)
            or not 1 <= len(reviewer) <= 128 or not isinstance(review_receipt,str)
            or len(review_receipt)!=64 or any(c not in '0123456789abcdef' for c in review_receipt)
            or not isinstance(attestations,dict) or set(attestations)!=set(ATTESTATIONS)
            or any(attestations[k] is not True for k in ATTESTATIONS)):
        raise CatalogError('explicit_review_required')
    conn.execute('BEGIN IMMEDIATE')
    try:
        validate_schema(conn)
        packet_valid(conn, packet, now=now)
        # Rebuild from authority; a recomputed hash cannot authorize forged spans.
        keys = ('scope','source_id','record_id','intent','subject','attribute','aliases','fragment_id','expires_at')
        rebuilt = prepare(conn, {k:packet[k] for k in keys}, now=packet['created_at'], project=project)
        if rebuilt != packet:
            raise CatalogError('packet_not_authoritative')
        cid = digest({'packet':expected_digest, 'reviewer':reviewer, 'receipt':review_receipt})
        existing = conn.execute('SELECT status FROM ie_v1_contract WHERE contract_id=?',(cid,)).fetchone()
        if existing:
            if existing[0] != 'approved':
                raise CatalogError('revoked_contract_cannot_reactivate')
            conn.rollback()
            return cid
        # An already-approved request remains idempotent at full capacity.
        # Revoked entries still cannot reactivate and new approvals stay bounded.
        if conn.execute('SELECT count(*) FROM ie_v1_contract').fetchone()[0] >= MAX_CONTRACTS:
            raise CatalogError('catalog_bound')
        values = (cid,*scope_tuple(packet['scope']),packet['source_id'],packet['record_id'],
                  'approved',packet['expires_at'],canonical(packet),expected_digest,reviewer,review_receipt)
        conn.execute('INSERT INTO ie_v1_contract VALUES('+','.join('?' for _ in values)+')',values)
        for alias in packet['aliases']:
            vals=(*scope_tuple(packet['scope']),packet['intent'],alias,packet['attribute'],cid)
            conn.execute('INSERT INTO ie_v1_alias VALUES('+','.join('?' for _ in vals)+')',vals)
        conn.execute('INSERT INTO ie_v1_event(contract_id,action,actor,packet_digest,occurred_at) '
                     "VALUES(?,'approve',?,?,?)",(cid,reviewer,expected_digest,now))
        conn.execute('UPDATE ie_v1_state SET epoch=epoch+1 WHERE id=1')
        conn.commit()
        return cid
    except BaseException:
        conn.rollback()
        raise


def revoke(conn, contract_id, *, reviewer, now=None):
    if not isinstance(reviewer,str) or not 1 <= len(reviewer) <= 128:
        raise CatalogError('explicit_review_required')
    now = int(time.time()) if now is None else int(now)
    conn.execute('BEGIN IMMEDIATE')
    try:
        validate_schema(conn)
        row=conn.execute('SELECT status,packet_digest FROM ie_v1_contract WHERE contract_id=?',(contract_id,)).fetchone()
        if not row:
            raise CatalogError('contract_missing')
        if row[0] != 'revoked':
            conn.execute("UPDATE ie_v1_contract SET status='revoked' WHERE contract_id=?",(contract_id,))
            conn.execute('DELETE FROM ie_v1_alias WHERE contract_id=?',(contract_id,))
            conn.execute('INSERT INTO ie_v1_event(contract_id,action,actor,packet_digest,occurred_at) '
                         "VALUES(?,'revoke',?,?,?)",(contract_id,reviewer,row[1],now))
            conn.execute('UPDATE ie_v1_state SET epoch=epoch+1 WHERE id=1')
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def lookup(conn, scope, slot):
    vals=(*scope_tuple(scope),slot.intent,slot.subject,slot.attribute)
    row=conn.execute('SELECT c.contract_id,c.packet_json,c.packet_digest FROM ie_v1_alias a '
        'JOIN ie_v1_contract c ON c.contract_id=a.contract_id WHERE '+
        ' AND '.join('a.'+k+'=?' for k in SCOPE_FIELDS)+
        " AND a.intent=? AND a.subject=? AND a.attribute=? AND c.status='approved'",vals).fetchone()
    if not row:
        return None
    packet=strict_json(row[1])
    if digest(packet)!=row[2]:
        raise CatalogError('contract_corrupt')
    packet_valid(conn,packet)
    if (packet['scope'] != scope or packet['intent'] != slot.intent
            or packet['attribute'] != slot.attribute or slot.subject not in packet['aliases']):
        raise CatalogError('contract_slot_mismatch')
    return row[0],packet


def policy_token(root):
    """Invalidate proactive retries on approval/revoke/business writes/expiry.

    This is an identity stamp, not an answer cache. No source text is returned.
    """
    try:
        with connect(root,deadline=time.perf_counter()+.05) as conn:
            state=validate_schema(conn)
            expired=conn.execute("SELECT count(*) FROM ie_v1_contract WHERE status='approved' AND expires_at<=?",
                                 (int(time.time()),)).fetchone()[0]
            return digest([SCHEMA,*state,expired])[:24]
    except (OSError,ValueError,sqlite3.Error,TypeError):
        return 'unavailable'
