"""Native source binding and UID-only physical facts for the root proof issuer.

No function here supplies a negative knowledge fact to the issuer. The worker
returns exact typed metadata and actual receive sequence, including cold rows.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from eimemory.contracts.managed_code_incident_proof import (
    DOMAIN, SOURCE_KEY, FIELDS, SCOPE_FIELDS, REGISTERED_INGRESS, NATIVE_SOURCES,
    canonical, digest, exact_scope, stable_defect_key, validate_reference, validate_snapshot,
)

PROOF_RECORD_SOURCE = 'eimemory.managed_code_incident_proof'


def checkpoint_managed_ingress():
    try:
        from eimemory.core.runtime_manager import checkpoint_managed_incident_knowledge
        return checkpoint_managed_incident_knowledge()
    except (ImportError,AttributeError,TypeError,ValueError,RuntimeError,OSError) as exc:
        return {'qualification_domain':DOMAIN,'status':'unproven','reason':'managed_ingress_checkpoint_unavailable',
                'error_type':type(exc).__name__,'qualifies_for_product_completion':False}


def native_activation_reference():
    """First-write metadata only. Existing original sources are never updated."""
    try:
        from eimemory.core.runtime_manager import activation_attestation
        value = activation_attestation()
        if (not isinstance(value, dict) or set(value) != {'schema', 'receipt_id', 'digest'}
                or value.get('schema') != 'eimemory.runtime_activation_receipt_ref.v1'):
            return None
        from eimemory.contracts.managed_code_incident_proof import hex_value
        hex_value(value['receipt_id']); hex_value(value['digest'])
        return value
    except (ImportError, AttributeError, ValueError, RuntimeError, OSError):
        return None


def bind_native_activation(record):
    """Called only on a new, not-yet-appended detector envelope.

    The invocation's detector timestamp is not the physical receive time.
    Precise native source creation happens after the root cause attestation;
    idempotent append still returns the untouched existing original.
    """
    reference = native_activation_reference()
    if reference is not None:
        record.meta['activation_attestation'] = reference
    now = datetime.now(timezone.utc).isoformat()
    record.time.created_at = now
    record.time.updated_at = now
    record.time.occurred_at = now
    record.meta['managed_first_physical_source']={
        'schema':'managed_native_first_physical_source.v1','activation_reference':reference,'created_at':now}
    return record


def _root_verification(snapshot):
    from eimemory.core.runtime_manager import verify_managed_incident_proof
    value=verify_managed_incident_proof(snapshot['reference']['receipt_id'],snapshot['reference']['digest'])
    fields={'schema','snapshot','qualification_current','current_managed_facts','current_watermark','reason'}
    if (not isinstance(value,dict) or set(value)!=fields
            or value.get('schema')!='managed_code_incident_proof_verification.v1'
            or value.get('snapshot')!=snapshot or type(value.get('qualification_current')) is not bool
            or type(value.get('current_watermark')) is not int or value['current_watermark']<snapshot['history_watermark']
            or not isinstance(value.get('current_managed_facts'),dict) or set(value['current_managed_facts'])!=set(FIELDS)
            or any(item is not None and type(item) is not bool for item in value['current_managed_facts'].values())
            or value['qualification_current'] is not (snapshot['status']=='proven' and all(item is False for item in value['current_managed_facts'].values()))):
        raise ValueError('managed_incident_root_readback_mismatch')
    return value


def verified_managed_snapshot(snapshot, *, scope=None, incident_id=None, incident_digest=None,
                              return_verification=False):
    """Re-read root original authority, not an application-controlled flag."""
    expected = validate_snapshot(snapshot)
    verification=_root_verification(expected)
    actual = validate_snapshot(verification['snapshot'])
    if (scope is not None and actual['scope'] != exact_scope(asdict(scope) if hasattr(scope, 'tenant_id') else scope)
            or incident_id is not None and actual['incident_id'] != incident_id
            or incident_digest is not None and actual['incident_digest'] != incident_digest):
        raise ValueError('managed_incident_identity_mismatch')
    return verification if return_verification else actual


def qualification_evidence(transaction):
    """Return explicit-domain facts without changing the original transaction.

    A root readback is mandatory on every consumer call. An application flag,
    snapshot SHA or valid SQL projection alone cannot establish managed facts.
    """
    from eimemory.contracts.code_incident_provenance import qualification_provenance
    from eimemory.contracts.managed_code_incident_proof import TRANSACTION_SCHEMA,transaction_snapshot
    value=qualification_provenance(transaction)
    global_facts={key:value.get(key) for key in FIELDS}
    if SOURCE_KEY not in value:
        return {'qualification_domain':'global_code_incident_provenance.v1',
                'facts':global_facts,'global_facts':global_facts,'snapshot':None}
    schema=value.get('transaction_schema_version',value.get('schema_version'))
    if schema!=TRANSACTION_SCHEMA:
        raise ValueError('managed_incident_qualification_encoding_invalid')
    if 'source_evidence' in value:
        snapshot=transaction_snapshot(value)
        if snapshot!=value[SOURCE_KEY]:raise ValueError('managed_incident_projection_mismatch')
    owner={key:value.get(key) for key in SCOPE_FIELDS}
    current=verified_managed_snapshot(value[SOURCE_KEY],scope=owner,
        incident_id=value.get('incident_id'),incident_digest=value.get('incident_digest'),return_verification=True)
    snapshot=current['snapshot']
    if global_facts!=snapshot['global_facts']:
        raise ValueError('managed_incident_qualification_global_facts_changed')
    facts=current['current_managed_facts']
    if not current['qualification_current'] and all(item is False for item in facts.values()):
        facts={key:None for key in FIELDS}
    return {'qualification_domain':DOMAIN,'facts':facts,
            'global_facts':global_facts,'snapshot':snapshot}


def observe_managed_incident(runtime, *, scope, record):
    """Only root issues; absence cannot become a manufactured proof."""
    try:
        from eimemory.core.runtime_manager import issue_managed_incident_proof, verify_managed_incident_proof
        from eimemory.models.records import RecordEnvelope
        reference = validate_reference(issue_managed_incident_proof(record.record_id, digest(record.to_dict())))
        verification=verify_managed_incident_proof(reference['receipt_id'], reference['digest'])
        snapshot = validate_snapshot(verification['snapshot'])
        incident = record.content
        if (snapshot['reference']!=reference or snapshot['scope'] != asdict(record.scope) or snapshot['source_record_id'] != record.record_id
                or snapshot['source_digest'] != digest(record.to_dict())
                or snapshot['incident_id'] != incident['incident_id']
                or snapshot['incident_digest'] != incident['incident_digest']):
            raise ValueError('managed_incident_original_source_mismatch')
        envelope = RecordEnvelope.create(kind='reflection', title='Managed code incident root proof reference',
            scope=record.scope, source=PROOF_RECORD_SOURCE, content={SOURCE_KEY: snapshot},
            meta={'source_record_id': record.record_id, 'source_digest': snapshot['source_digest'],
                  'idempotency_key': 'managed-code-proof:' + reference['digest']})
        envelope.record_id = 'managed-code-proof-' + digest([record.record_id,snapshot['source_digest']])[:40]
        runtime.store.append(envelope, existing_match=lambda old: old.scope == record.scope
            and old.source == PROOF_RECORD_SOURCE and old.content == envelope.content)
        return {'qualification_domain':DOMAIN,'status':'proven' if verification['qualification_current'] else
                'adverse' if any(item is True for item in verification['current_managed_facts'].values()) else 'unproven',
                'reference':reference,'current_watermark':verification['current_watermark']}
    except (ImportError, AttributeError, KeyError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        return {'qualification_domain': DOMAIN, 'status': 'unproven',
                'reason': 'managed_incident_proof_unavailable', 'error_type': type(exc).__name__}


def source_managed_snapshot(runtime, record):
    owner = record.scope
    reader = getattr(runtime.store,'list_records_by_sources_exact_scope',None)
    if not callable(reader): return None
    record_id = 'managed-code-proof-' + digest([record.record_id,digest(record.to_dict())])[:40]
    rows = reader(kinds=['reflection'],sources=[PROOF_RECORD_SOURCE],scope=owner,
        record_ids=[record_id],limit=2)
    if len(rows)>1:raise ValueError('managed_incident_proof_source_page_conflicted')
    candidates = [row.content.get(SOURCE_KEY) for row in rows
        if row.meta.get('source_record_id') == record.record_id]
    if not candidates:
        return None
    if len({digest(item) for item in candidates}) != 1:
        raise ValueError('managed_incident_original_proof_conflict')
    value = verified_managed_snapshot(candidates[0], scope=owner,
        incident_id=record.content['incident_id'], incident_digest=record.content['incident_digest'])
    if value['source_digest'] != digest(record.to_dict()):
        raise ValueError('managed_incident_original_source_changed')
    return value


def collect_physical_facts(database_path, *, manifest, source_record_id, source_digest, after=0):
    """Fixed UID worker. URI read-only, SELECT-only and no Runtime/DAO writes.

Each call reads at most 5000 original receive rows. A root-owned persistent
cursor is advanced only after the issuer commits every positive fact. A
bounded call never declares an unfinished prefix complete.
"""
    from eimemory.contracts.managed_code_incident_proof import hex_value
    from eimemory.models.records import RecordEnvelope
    from eimemory.storage.migrations.code_incident_coverage import schema_ready, JOURNAL
    from eimemory.storage.code_incident_coverage_archive import CoverageArchive
    from eimemory.governance.learning.effect_policy import verify
    from eimemory.governance.evolution.code_automation_policy_incidents import _trusted_incident_identity
    if source_record_id:hex_value(source_digest)
    elif source_digest!='':raise ValueError('managed_incident_checkpoint_source_invalid')
    uri = Path(database_path).resolve().as_uri() + '?mode=ro'
    conn = sqlite3.connect(uri, uri=True); conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.set_authorizer(lambda action, _a, _b, _db, _trigger:
            sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ,
                sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_RECURSIVE} else sqlite3.SQLITE_DENY)
        conn.execute('BEGIN')
        if not schema_ready(conn):
            raise ValueError('managed_incident_ingress_capture_unverified')
        captured_schema = {row['name']: row['sql'] for row in conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE name LIKE 'trg_code_incident_coverage_%' OR name LIKE 'idx_incident_coverage_%' ORDER BY name")}
        if digest(captured_schema) != manifest['schema_digest']:
            raise ValueError('managed_incident_ingress_schema_changed')
        source={};owner=None;incident=None;key=None
        if source_record_id:
            matches = conn.execute('SELECT * FROM records WHERE record_id=? LIMIT 2', (source_record_id,)).fetchall()
            if len(matches) != 1 or matches[0]['payload_pointer_json']:
                raise ValueError('managed_incident_source_not_unique_inline')
            physical = dict(matches[0]); source = json.loads(physical['payload_json'])
            if digest(source) != source_digest or physical['payload_digest'] != source_digest:
                raise ValueError('managed_incident_source_digest_mismatch')
            owner = exact_scope(source.get('scope'))
            if owner not in manifest['scopes'] or any(physical[k] != owner[k] for k in SCOPE_FIELDS):
                raise ValueError('managed_incident_source_scope_unregistered')
            report = source.get('content', {}).get('detector_report', {})
            commit = report.get('release_commit') or report.get('expected_commit')
            record = RecordEnvelope.from_dict(source)
            incident = _trusted_incident_identity(record, commit)
            if incident is None:
                raise ValueError('managed_incident_native_source_unverified')
            key = stable_defect_key(incident['incident_class'], incident['diagnostic_codes'])
        facade = SimpleNamespace(sqlite=SimpleNamespace(conn=conn, path=Path(database_path)))
        cold = CoverageArchive(facade, verify)
        high = max(int(conn.execute(f'SELECT coalesce(max(sequence),0) FROM {JOURNAL}').fetchone()[0]), cold.high_water(JOURNAL))
        seq = conn.execute('SELECT seq FROM sqlite_sequence WHERE name=?', (JOURNAL,)).fetchone()
        if (seq is None and high!=0 or seq is not None and seq[0] != high
                or type(after) is not int or not 0 <= after <= high):
            raise ValueError('managed_incident_receive_sequence_changed')
        through = min(high, after + 5000)
        hot = [dict(row) for row in conn.execute(f'SELECT * FROM {JOURNAL} WHERE sequence>? AND sequence<=? ORDER BY sequence', (after, through))]
        rows = sorted(hot + cold.range_rows(JOURNAL, after, through), key=lambda row: row['sequence'])
        if len(rows) != through - after or any(row['sequence'] != after + i + 1 for i, row in enumerate(rows)):
            raise ValueError('managed_incident_receive_prefix_incomplete')
        first=[]
        if source_record_id:
            first = [dict(row) for row in conn.execute(f'''SELECT * FROM {JOURNAL}
                WHERE table_name='records' AND operation='INSERT' AND record_id=? AND payload_digest=?
                AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? LIMIT 2''',
                (source_record_id,source_digest,*(owner[k] for k in SCOPE_FIELDS)))]
            first += cold.key_rows(JOURNAL,scope=owner,record_id=source_record_id,payload_digest=source_digest,limit=2)
            if len(first)!=1 or first[0]['source']!=source['source']:
                raise ValueError('managed_incident_original_insert_unverified')
        # An actual retained typed prehistory is checked once at root cursor 0.
        history = []
        if after == 0:
            for scope in manifest['scopes']:
                typed = conn.execute('SELECT payload_json,payload_pointer_json,payload_digest FROM records WHERE kind=? AND tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? LIMIT 100001',
                    ('incident', *(scope[k] for k in SCOPE_FIELDS))).fetchall()
                if len(typed) > 100000:
                    raise ValueError('managed_incident_history_inventory_incomplete')
                for row in typed:
                    if row['payload_pointer_json']:
                        raise ValueError('managed_incident_history_source_unavailable')
                    raw = json.loads(row['payload_json'])
                    if digest(raw) != row['payload_digest']:
                        raise ValueError('managed_incident_history_digest_invalid')
                    content = raw.get('content', {})
                    if raw.get('source') not in REGISTERED_INGRESS:
                        raise ValueError('managed_incident_unclassified_code_input')
                    try:
                        old_key = stable_defect_key(content.get('incident_class'), content.get('diagnostic_codes'))
                    except ValueError:
                        # A known manual report without an exact defect mapping
                        # is adverse to every native class, never ignored.
                        old_key = '*'
                    human=(raw.get('source')=='eimemory.code_maintenance'
                        or raw.get('provenance',{}).get('origin')=='user_reported')
                    if human:old_key='*'
                    history.append({'scope': raw['scope'], 'record_id': raw['record_id'], 'defect_key': old_key,
                        'source_digest': row['payload_digest'], 'source_time': raw['time']['occurred_at'],
                        'human_input':human})
            # Old transactions remain positive prehistory even when source
            # records were subsequently removed. No cross-owner borrowing.
            table = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='code_evolution_transactions'").fetchone()
            if table:
                for scope in manifest['scopes']:
                    old = conn.execute('SELECT incident_id,incident_digest,payload_json FROM code_evolution_transactions WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? LIMIT 100001', tuple(scope[k] for k in SCOPE_FIELDS)).fetchall()
                    if len(old) > 100000: raise ValueError('managed_incident_transaction_history_incomplete')
                    for row in old:
                        payload = json.loads(row['payload_json']); incident_data = payload.get('incident', {})
                        try: old_key = stable_defect_key(incident_data.get('incident_class'), incident_data.get('diagnostic_codes'))
                        except ValueError: old_key = '*'
                        history.append({'scope': scope, 'record_id': row['incident_id'], 'defect_key': old_key,
                            'source_digest': row['incident_digest'], 'source_time': '', 'human_input': payload.get('prior_user_reported') is True})
        return dict(schema='managed_code_physical_facts.v1', scope=owner, source_record_id=source_record_id,
            source_digest=source_digest, incident=incident, defect_key=key,
            activation_reference=source.get('meta', {}).get('activation_attestation'),
            first_source_evidence=source.get('meta',{}).get('managed_first_physical_source'),
            global_facts={k: source.get('provenance',{}).get(k) for k in FIELDS},
            source_time=source.get('time',{}).get('occurred_at'),source_created_at=source.get('time',{}).get('created_at'),
            source_received_at=first[0]['observed_at'] if first else None,
            after=after, through=through, high_water=high, complete=through == high, rows=rows,
            history=history, schema_digest=manifest['schema_digest'], observed_at=datetime.now(timezone.utc).isoformat())
    finally:
        conn.close()
