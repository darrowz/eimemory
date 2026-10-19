"""Canonical tri-state incident facts, independent of permission to repair."""
from __future__ import annotations
from collections.abc import Mapping
from hashlib import sha256
import json
from typing import Any

FIELDS = ('known_before_detection', 'prior_user_reported', 'manual_bootstrap')
SCHEMA = 'code_incident_provenance.v1'
SOURCE_KEY = 'incident_provenance'
NATIVE_UNPROVEN_DETECTORS = frozenset({'eimemory.runtime_identity_drift.v1',
    'eimemory.release_closure_failure.v1', 'eimemory.system_code_repair_failure.v1'})


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def provenance_snapshot(value: Mapping[str, Any]) -> dict:
    facts = {key: value.get(key) for key in ('origin', 'detector', *FIELDS)}
    if facts['origin'] is None: facts['origin'] = ''
    if facts['detector'] is None: facts['detector'] = ''
    if (facts['origin'] not in {'', 'system_detector', 'user_reported', 'manual_bootstrap'}
            or not isinstance(facts['detector'], str)
            or any(facts[key] is not None and type(facts[key]) is not bool for key in FIELDS)):
        raise ValueError('incident_provenance_facts_invalid')
    material = {'schema': SCHEMA, 'facts': facts}
    return {**material, 'digest': _digest(material)}


def verified_provenance(snapshot: Any) -> dict:
    if (not isinstance(snapshot, Mapping) or set(snapshot) != {'schema', 'facts', 'digest'}
            or snapshot.get('schema') != SCHEMA or not isinstance(snapshot.get('facts'), Mapping)
            or set(snapshot['facts']) != {'origin', 'detector', *FIELDS}
            or provenance_snapshot(snapshot['facts']) != dict(snapshot)):
        raise ValueError('incident_provenance_snapshot_invalid')
    return dict(snapshot['facts'])


def native_detection_provenance(detector: str, original: Mapping[str, Any] | None = None) -> dict:
    """A detector sees a defect, not the absence of earlier human knowledge.

Explicit adverse original facts are retained. Absence or an unsupported
negative never establishes system-first novelty. A future proof producer
needs its own verified contract before it can establish those negatives.
"""
    original = original if isinstance(original, Mapping) else {}
    return provenance_snapshot({'origin': 'system_detector', 'detector': detector,
                                **{key: True if original.get(key) is True else None for key in FIELDS}})


def provenance_guard(value: Any) -> int:
    if value is None: return 1
    if type(value) is not bool: raise ValueError('incident_provenance_fact_invalid')
    return int(value)


def qualification_provenance(value: Mapping[str, Any]) -> dict:
    """Normalize known unsupported legacy native negatives for qualification.

Old native producers hard-coded False without proving prior human knowledge.
Their original signed declarations remain intact. No current native producer
can assert those negative facts, even with a self-consistent ordinary hash.
Other authoritative source contracts retain their declared typed facts.
"""
    result = dict(value)
    if value.get('detector') in NATIVE_UNPROVEN_DETECTORS:
        declared = {key: value.get(key) for key in FIELDS}
        if any(item is False or type(item) is int and item == 0 for item in declared.values()):
            result['declared_provenance'] = declared
            result['provenance_basis'] = 'native_negative_facts_unproven'
            for key in FIELDS:
                if result.get(key) is False or type(result.get(key)) is int and result[key] == 0:
                    result[key] = None
    return result
