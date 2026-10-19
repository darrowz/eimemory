"""Explicit managed-domain knowledge. Legacy global facts are not redefined.

Shape validation is never issuer verification. Production qualification must
re-read the immutable receipt through the protected host-manager authority.
"""
from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import re

DOMAIN = 'registered_code_defect_knowledge.v1'
SCHEMA = 'managed_code_incident_proof.v1'
REFERENCE_SCHEMA = 'managed_code_incident_proof_ref.v1'
MANIFEST_SCHEMA = 'managed_code_incident_manifest.v1'
KEY_SCHEMA = 'managed_code_defect_key.v1'
SOURCE_KEY = 'managed_incident_proof'
TRANSACTION_SCHEMA = 'code_evolution_transaction.v4'
FIELDS = ('known_before_detection', 'prior_user_reported', 'manual_bootstrap')
SCOPE_FIELDS = ('tenant_id', 'agent_id', 'workspace_id', 'user_id')
NATIVE_SOURCES = ('eimemory.release_closure_failure', 'eimemory.runtime_identity_drift',
                  'eimemory.system_code_repair_failure')
REGISTERED_INGRESS = (*NATIVE_SOURCES, 'eimemory.code_maintenance')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode()).hexdigest()


def exact_scope(value):
    if (not isinstance(value, Mapping) or set(value) != set(SCOPE_FIELDS)
            or any(not isinstance(value[k], str) or not value[k] or value[k] != value[k].strip()
                   or len(value[k]) > 512 or any(ord(c) < 32 for c in value[k]) for k in SCOPE_FIELDS)):
        raise ValueError('managed_incident_scope_invalid')
    return {k: value[k] for k in SCOPE_FIELDS}


def hex_value(value, size=64):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{'+str(size)+'}', value) is None:
        raise ValueError('managed_incident_digest_invalid')
    return value


def stable_defect_key(incident_class, diagnostic_codes):
    """No release, producer fingerprint, policy, epoch or timestamp in the key.

    The codes are the original native producer's typed contract reason codes,
    never model text. Dynamic IDs/timestamps are rejected instead of turning a
    new release into apparent novelty. The manifest pins this key codec.
"""
    if (incident_class not in {'deployment.runtime_commit_drift', 'release.closure_internal_failure',
            'release.closure_report_failure', 'code.system_repair_policy_stale', 'code.incident_routing_stale'}
            or not isinstance(diagnostic_codes, list) or not diagnostic_codes
            or len(diagnostic_codes) > 128 or any(not isinstance(x, str) or not x or len(x) > 512
                or re.fullmatch('[A-Za-z0-9_.:-]+',x) is None or re.search('[0-9a-f]{40}',x)
                for x in diagnostic_codes)):
        raise ValueError('managed_incident_key_unclassified')
    return digest({'schema': KEY_SCHEMA, 'affected_contract': incident_class,
                   'diagnostic_codes': sorted(set(diagnostic_codes))})


def validate_reference(value):
    if (not isinstance(value, Mapping) or set(value) != {'schema', 'receipt_id', 'digest'}
            or value.get('schema') != REFERENCE_SCHEMA):
        raise ValueError('managed_incident_reference_invalid')
    hex_value(value['receipt_id']); hex_value(value['digest'])
    return dict(value)


def validate_snapshot(value):
    fields = {'schema', 'qualification_domain', 'reference', 'scope', 'source_record_id', 'source_digest',
        'incident_id', 'incident_digest', 'defect_key', 'activation_reference', 'manifest_digest',
        'history_watermark', 'source_sequence', 'managed_facts', 'global_facts', 'external_knowledge',
        'external_user_reports', 'status', 'reason', 'digest'}
    if (not isinstance(value, Mapping) or set(value) != fields or value.get('schema') != SCHEMA
            or value.get('qualification_domain') != DOMAIN or value.get('external_knowledge') is not None
            or value.get('external_user_reports') is not None or value.get('status') not in {'proven', 'unproven', 'adverse'}):
        raise ValueError('managed_incident_snapshot_invalid')
    exact_scope(value['scope']); validate_reference(value['reference'])
    for key in ('source_digest', 'incident_digest', 'defect_key', 'manifest_digest', 'digest'):
        hex_value(value[key])
    for key in ('history_watermark', 'source_sequence'):
        if type(value[key]) is not int or value[key] < 1:
            raise ValueError('managed_incident_watermark_invalid')
    if value['history_watermark'] < value['source_sequence']:
        raise ValueError('managed_incident_watermark_invalid')
    for key in ('global_facts', 'managed_facts'):
        facts = value[key]
        if (not isinstance(facts, Mapping) or set(facts) != set(FIELDS)
                or any(x is not None and type(x) is not bool for x in facts.values())):
            raise ValueError('managed_incident_facts_invalid')
    # A domain certificate has no power to establish a global negative.
    if any(x is False for x in value['global_facts'].values()):
        raise ValueError('managed_incident_global_negative_forbidden')
    if value['status'] == 'proven' and any(x is not False for x in value['managed_facts'].values()):
        raise ValueError('managed_incident_proven_facts_invalid')
    if digest({k: v for k, v in value.items() if k != 'digest'}) != value['digest']:
        raise ValueError('managed_incident_snapshot_digest_invalid')
    return dict(value)


def transaction_snapshot(transaction):
    """Immutable four-way projection; a present malformed proof is an error."""
    payload = transaction.get('payload', {})
    source = transaction.get('source_evidence', {})
    if not isinstance(payload, Mapping) or not isinstance(source, Mapping):
        raise ValueError('managed_incident_transaction_payload_invalid')
    nested = payload.get('payload', {})
    mirrored = payload.get('source_evidence', {})
    layers = (source, payload, nested, mirrored)
    present = [layer.get(SOURCE_KEY) if isinstance(layer, Mapping) else None for layer in layers]
    if not any(item is not None for item in present):
        if transaction.get('schema_version') == TRANSACTION_SCHEMA:
            raise ValueError('managed_incident_transaction_proof_missing')
        return None
    if transaction.get('schema_version') != TRANSACTION_SCHEMA or any(item != present[0] for item in present):
        raise ValueError('managed_incident_transaction_proof_mismatch')
    proof = validate_snapshot(present[0])
    owner = {k: transaction.get(k) for k in SCOPE_FIELDS}
    if proof['scope'] != owner or proof['incident_id'] != transaction.get('incident_id') or proof['incident_digest'] != transaction.get('incident_digest'):
        raise ValueError('managed_incident_transaction_identity_mismatch')
    if any(transaction.get(k) != proof['global_facts'][k] for k in FIELDS):
        raise ValueError('managed_incident_transaction_global_facts_mismatch')
    return proof
