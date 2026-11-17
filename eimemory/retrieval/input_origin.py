"""Host-attested input provenance, separate from outcomes and recall policy.

Absence, stale/tampered signatures and legacy data mean unknown. Readers never
create a table or backfill provenance. Producer authentication is independent
of the ordinary recall bearer; this does not claim model-inaccessible OS keys.
"""
from __future__ import annotations

from hashlib import sha256
import hmac
import json
import re
import sqlite3

from eimemory.core.clock import now_iso
from eimemory.governance.tool_receipts import receipt_key_set

SCHEMA = 'recall.input_origin.v1'
TABLE = 'proactive_input_origins_v1'
EXTERNAL = 'host_attested_external_input'
INTERNAL = 'host_attested_internal_input'
UNKNOWN = 'unknown'
KINDS = {EXTERNAL, INTERNAL, UNKNOWN}
EXTERNAL_PLATFORMS = frozenset({'feishu', 'telegram', 'discord', 'slack', 'signal', 'whatsapp', 'matrix'})
CLAIM_FIELDS = {'schema', 'origin_kind', 'profile_name', 'platform', 'home_digest',
    'actor_digest', 'chat_digest', 'message_digest', 'host_turn_digest',
    'host_query_digest', 'query_digest', 'native_receipt_digest'}
DIGEST_FIELDS = {key for key in CLAIM_FIELDS if key.endswith('_digest')}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def text_digest(value):
    return sha256(value.encode('utf-8', errors='replace')).hexdigest()


def validate_claim(claim):
    if (not isinstance(claim, dict) or set(claim) != CLAIM_FIELDS or claim.get('schema') != SCHEMA
            or claim.get('origin_kind') not in KINDS):
        raise ValueError('input_origin_claim_invalid')
    if any(not isinstance(claim[key], str) or re.fullmatch('[0-9a-f]{64}', claim[key]) is None
           for key in DIGEST_FIELDS):
        raise ValueError('input_origin_digest_invalid')
    if any(not isinstance(claim[key], str) or re.fullmatch('[a-zA-Z0-9_.-]{1,64}', claim[key]) is None
           for key in ('profile_name', 'platform')):
        raise ValueError('input_origin_source_invalid')
    if claim['origin_kind'] == EXTERNAL and (claim['platform'] not in EXTERNAL_PLATFORMS
            or claim['chat_digest'] == text_digest('') or claim['message_digest'] == text_digest('')):
        raise ValueError('input_origin_external_source_invalid')
    return json.loads(canonical(claim))


def _sign(body):
    keys = receipt_key_set()
    if keys is None:
        raise ValueError('input_origin_signing_unavailable')
    signed = {**body, 'key_id': keys.active_id}
    signed['signature'] = hmac.new(keys.active_key.encode(),
        SCHEMA.encode() + b'\0' + canonical(signed).encode(), sha256).hexdigest()
    return signed


def _verified(body):
    keys = receipt_key_set()
    if keys is None or not isinstance(body, dict):
        return False
    unsigned = dict(body)
    signature = unsigned.pop('signature', '')
    secret = keys.verification_keys.get(unsigned.get('key_id'))
    if not secret or not isinstance(signature, str):
        return False
    try:
        expected = hmac.new(secret.encode(), SCHEMA.encode() + b'\0' + canonical(unsigned).encode(), sha256).hexdigest()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(signature, expected)


def _namespace(decision):
    return {key: decision[key] for key in ('channel', 'scope', 'session_id', 'turn_id', 'decision_id')} | {
        'source_ids': sorted(decision['source_ids'])}


def _unique_input(claim, namespace):
    return digest({key: claim[key] for key in
        ('profile_name', 'home_digest', 'platform', 'chat_digest', 'message_digest')} | {
            key: namespace[key] for key in ('channel', 'scope', 'source_ids')})


def unknown(reason='not_recorded'):
    return {'schema': SCHEMA, 'origin_kind': UNKNOWN, 'verified': False,
            'natural_learning_eligible': False, 'reason': reason}


def record_input_origin(store, *, channel, scope, source_ids, session_id, turn_id,
                        decision_id, origin, attestation_producer):
    if channel != 'hermes' or attestation_producer != channel:
        raise ValueError('input_origin_producer_required')
    claim = validate_claim(origin)
    from eimemory.evaluation.hermes_profile_coverage import request_sampling_binding
    binding = request_sampling_binding(channel=channel, scope=scope, source_ids=source_ids)
    if (binding is None or binding['profile_name'] != claim['profile_name']
            or text_digest(binding['hermes_home']) != claim['home_digest']
            or text_digest(scope['user_id']) != claim['actor_digest']):
        raise ValueError('input_origin_profile_unbound')
    if claim['origin_kind'] == EXTERNAL and binding['natural_sample_eligible'] is not True:
        raise ValueError('input_origin_operator_not_external')
    namespace = dict(channel=channel, scope=scope, source_ids=sorted(source_ids),
                     session_id=session_id, turn_id=turn_id, decision_id=decision_id)
    with store.locked() as conn:
        if conn.in_transaction:
            raise ValueError('input_origin_transaction_active')
        try:
            conn.execute('BEGIN IMMEDIATE')
            decision = conn.load_proactive_decision(decision_id)
            if not decision or _namespace(decision) != namespace or decision['query_digest'] != claim['query_digest']:
                raise ValueError('input_origin_decision_mismatch')
            if decision.get('acceptance_generated') is not False and claim['origin_kind'] == EXTERNAL:
                raise ValueError('input_origin_acceptance_not_external')
            body = dict(schema=SCHEMA, namespace=namespace, claim=claim,
                unique_input_digest=_unique_input(claim, namespace),
                provenance='private_host_producer', producer=attestation_producer)
            # Signing first avoids creating an empty table when the trusted key
            # is unavailable. Only this authenticated writer creates the table.
            signed = _sign(body)
            conn.execute(f'CREATE TABLE IF NOT EXISTS {TABLE} '
                '(decision_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,'
                'FOREIGN KEY(decision_id) REFERENCES proactive_decisions(decision_id) ON DELETE CASCADE)')
            prior = conn.execute(f'SELECT payload_json FROM {TABLE} WHERE decision_id=?', (decision_id,)).fetchone()
            if prior:
                existing = json.loads(prior[0])
                if not isinstance(existing, dict):
                    raise ValueError('input_origin_identity_conflict')
                compared = {key: value for key, value in existing.items() if key not in {'key_id', 'signature'}}
                if not _verified(existing) or compared != body:
                    raise ValueError('input_origin_identity_conflict')
                conn.commit()
                return {'ok': True, 'replayed': True, 'origin_kind': claim['origin_kind']}
            conn.execute(f'INSERT INTO {TABLE} VALUES (?,?,?)', (decision_id, canonical(signed), now_iso()))
            conn.commit()
            return {'ok': True, 'replayed': False, 'origin_kind': claim['origin_kind']}
        except BaseException:
            conn.rollback()
            raise


def read_input_origin(sqlite, decision):
    """Content-free verification on the caller's existing read transaction."""
    try:
        if not sqlite.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            return unknown()
        row = sqlite.execute(f'SELECT payload_json FROM {TABLE} WHERE decision_id=?', (decision['decision_id'],)).fetchone()
        if not row:
            return unknown()
        body = json.loads(row[0])
        if set(body) != {'schema', 'namespace', 'claim', 'unique_input_digest', 'provenance', 'producer', 'key_id', 'signature'}:
            return unknown('unverified')
        claim = validate_claim(body['claim'])
        if (not _verified(body) or body.get('schema') != SCHEMA or body.get('namespace') != _namespace(decision)
                or body.get('producer') != 'hermes' or body.get('provenance') != 'private_host_producer'
                or claim['query_digest'] != decision['query_digest']):
            return unknown('unverified')
        expected_unique = _unique_input(claim, body['namespace'])
        if body.get('unique_input_digest') != expected_unique:
            return unknown('unverified')
        eligible = (claim['origin_kind'] == EXTERNAL and decision.get('acceptance_generated') is False)
        return dict(schema=SCHEMA, origin_kind=claim['origin_kind'], verified=True,
                    natural_learning_eligible=eligible, unique_input_digest=expected_unique,
                    host_turn_digest=claim['host_turn_digest'], profile_name=claim['profile_name'])
    except (TypeError, ValueError, KeyError, sqlite3.Error):
        return unknown('unverified')
