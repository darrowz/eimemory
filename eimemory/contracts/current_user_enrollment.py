"""Typed current-epoch enrollment, independent of historical migration custody."""
from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import hmac
import json
import re

INTENT_SCHEMA = 'eimemory.user_current_enrollment_intent.v1'
PLAN_SCHEMA = 'eimemory.user_current_enrollment_plan.v1'
RECEIPT_SCHEMA = 'eimemory.user_current_enrollment_receipt.v1'
TOPOLOGY_SCHEMA = 'eimemory.hermes_current_topology_intent.v1'
DOMAIN = b'eimemory.current_user_enrollment.v1\x00'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                      allow_nan=False).encode('utf-8')


def digest(value):
    return sha256(canonical(value)).hexdigest()


def hexadecimal(value, length=64):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{'+str(length)+'}', value) is None:
        raise ValueError('current_enrollment_digest_invalid')
    return value


def _operator(value, uid, gid):
    if (type(uid) is not int or uid <= 0 or type(gid) is not int or gid <= 0
            or type(value.get('runtime_uid')) is not int or value['runtime_uid'] != uid
            or type(value.get('runtime_gid')) is not int or value['runtime_gid'] != gid
            or value.get('authorization') != 'explicit_user_operator'
            or value.get('historical_custody_recovered') is not False
            or value.get('autonomous_repair_credit') is not False):
        raise ValueError('current_enrollment_operator_invalid')


def validate_intent(value, *, uid, gid):
    fields = {'schema', 'enrollment_id', 'runtime_uid', 'runtime_gid', 'current_commit',
              'bundle_sha256', 'registry_sha256', 'runtime_settings_sha256', 'unit_scopes_sha256',
              'native_snapshot_sha256', 'authority_snapshot_sha256', 'topology_sha256',
              'authorization', 'historical_custody_recovered', 'autonomous_repair_credit'}
    if not isinstance(value, dict) or set(value) != fields or value.get('schema') != INTENT_SCHEMA:
        raise ValueError('current_enrollment_intent_invalid')
    _operator(value, uid, gid)
    for key in fields - {'schema', 'runtime_uid', 'runtime_gid', 'authorization',
                         'historical_custody_recovered', 'autonomous_repair_credit'}:
        hexadecimal(value[key], 32 if key == 'enrollment_id' else 40 if key == 'current_commit' else 64)
    return value


def validate_plan(value, *, uid, gid):
    fields = {'schema', 'enrollment_id', 'runtime_uid', 'runtime_gid', 'current_commit',
              'bundle_sha256', 'operator_intent_sha256', 'registry_sha256',
              'native_snapshot_sha256', 'authority_snapshot_sha256', 'topology_sha256',
              'activation_key_sha256', 'before_inventory_sha256', 'publication_sha256',
              'authorization', 'historical_custody_recovered', 'autonomous_repair_credit'}
    if not isinstance(value, dict) or set(value) != fields or value.get('schema') != PLAN_SCHEMA:
        raise ValueError('current_enrollment_plan_invalid')
    _operator(value, uid, gid)
    for key in fields - {'schema', 'runtime_uid', 'runtime_gid', 'authorization',
                         'historical_custody_recovered', 'autonomous_repair_credit'}:
        hexadecimal(value[key], 32 if key == 'enrollment_id' else 40 if key == 'current_commit' else 64)
    return value


def sign_completion(payload, key):
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError('current_enrollment_key_invalid')
    return {'payload': payload,
            'signature': hmac.new(key, DOMAIN+canonical(payload), 'sha256').hexdigest()}


def validate_materials(key, *, plan, intent, registry, topology_raw, native_raw, authority_raw):
    """Check immutable bindings without asserting a completion or timestamp."""
    if not isinstance(key, bytes) or len(key) != 32:
        raise ValueError('current_enrollment_key_invalid')
    validate_plan(plan, uid=registry['runtime_uid'], gid=registry['runtime_gid'])
    validate_intent(intent, uid=registry['runtime_uid'], gid=registry['runtime_gid'])
    if (intent['enrollment_id'] != plan['enrollment_id'] or intent['current_commit'] != plan['current_commit']
            or digest(registry) != plan['registry_sha256']
            or sha256(topology_raw).hexdigest() != plan['topology_sha256']
            or sha256(native_raw).hexdigest() != plan['native_snapshot_sha256']
            or sha256(authority_raw).hexdigest() != plan['authority_snapshot_sha256']
            or digest(intent) != plan['operator_intent_sha256']
            or any(intent[field] != plan[field] for field in
                   ('bundle_sha256','registry_sha256','native_snapshot_sha256','authority_snapshot_sha256','topology_sha256'))
            or sha256(key).hexdigest() != plan['activation_key_sha256']):
        raise ValueError('current_enrollment_material_binding_invalid')
    return True


def validate_completion(value, key, *, plan, intent, registry, topology_raw, topology_intent_raw,
                        native_raw, authority_raw):
    validate_materials(key,plan=plan,intent=intent,registry=registry,topology_raw=topology_raw,
                       native_raw=native_raw,authority_raw=authority_raw)
    fields = {'schema', 'enrollment_id', 'runtime_uid', 'runtime_gid', 'origin',
              'created_at', 'plan_sha256', 'topology_intent_sha256', 'publication_sha256',
              'authorization', 'historical_custody_recovered', 'release_accepted',
              'autonomous_repair_credit'}
    if (not isinstance(value, dict) or set(value) != {'payload', 'signature'}
            or not isinstance(value.get('payload'), dict) or set(value['payload']) != fields):
        raise ValueError('current_enrollment_receipt_invalid')
    payload = value['payload']
    _operator(payload, registry['runtime_uid'], registry['runtime_gid'])
    try:
        instant = datetime.fromisoformat(payload['created_at'].replace('Z', '+00:00'))
        valid_instant = instant.tzinfo is not None and instant.utcoffset() is not None
    except (ValueError, TypeError, AttributeError):
        valid_instant = False
    if (payload.get('schema') != RECEIPT_SCHEMA or payload.get('origin') != 'current_native_user_operator'
            or payload.get('release_accepted') is not False or not valid_instant
            or payload.get('enrollment_id') != plan['enrollment_id']
            or payload.get('plan_sha256') != digest(plan)
            or payload.get('topology_intent_sha256') != sha256(topology_intent_raw).hexdigest()
            or payload.get('publication_sha256') != plan['publication_sha256']):
        raise ValueError('current_enrollment_receipt_binding_invalid')
    expected = sign_completion(payload, key)['signature']
    if not isinstance(value['signature'], str) or not hmac.compare_digest(expected, value['signature']):
        raise ValueError('current_enrollment_receipt_signature_invalid')
    return payload
