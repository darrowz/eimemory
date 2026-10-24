"""Validated scope-move envelopes shared by record-log recovery consumers."""
from __future__ import annotations

from dataclasses import asdict
import re
from typing import Any, Mapping
from uuid import uuid4

from eimemory.core.record_ids import validate_record_id
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_id


_MOVE_KEY = '_record_scope_move'
_MOVE_SCHEMA = 'record_scope_move.v1'
_SCOPE_KEYS = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}


def scope_move_payload(record: RecordEnvelope, previous_scope: ScopeRef) -> dict[str, Any]:
    """Allocate the event nonce once, inside the authoritative move transaction."""
    payload = {**record.to_dict(), _MOVE_KEY: {
        'schema': _MOVE_SCHEMA, 'nonce': uuid4().hex,
        'previous_ref': {'record_id': record.record_id, 'source_id': record.source_id,
                         'scope': asdict(previous_scope)},
    }}
    retired_record_ref(payload)
    return payload


def retired_record_ref(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the exact retired identity, or None for an ordinary legacy row.

    The current snapshot remains a complete RecordEnvelope. The reserved
    top-level marker is produced only by a committed storage scope move;
    ordinary imports/ingest cannot add it through record business metadata.
    """
    if _MOVE_KEY not in payload:
        return None
    marker = payload[_MOVE_KEY]
    if (not isinstance(marker, dict) or set(marker) != {'schema', 'nonce', 'previous_ref'}
            or marker.get('schema') != _MOVE_SCHEMA
            or not isinstance(marker.get('nonce'), str)
            or re.fullmatch(r'[0-9a-f]{32}', marker['nonce']) is None):
        raise ValueError('record_scope_move_invalid')
    previous = marker['previous_ref']
    if not isinstance(previous, dict) or set(previous) != {'record_id', 'source_id', 'scope'}:
        raise ValueError('record_scope_move_invalid')
    scopes = (previous['scope'], payload.get('scope'))
    if any(not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS
           or any(type(value) is not str for value in scope.values()) for scope in scopes):
        raise ValueError('record_scope_move_invalid')
    old_scope, current_scope = (ScopeRef.from_dict(scope) for scope in scopes)
    record_id = previous['record_id']
    source_id = previous['source_id']
    if (type(record_id) is not str or type(source_id) is not str or not source_id
            or record_id != payload.get('record_id')
            or source_id != payload.get('source_id')
            or source_id != normalize_source_id(source_id)
            or old_scope == current_scope):
        raise ValueError('record_scope_move_invalid')
    validate_record_id(record_id)
    return {'record_id': record_id, 'source_id': source_id, 'scope': asdict(old_scope)}
