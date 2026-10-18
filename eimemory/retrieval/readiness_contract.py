"""Pure, bounded projection of a native current backend measurement.

This structural contract does not grant authority to supplied dictionaries.
The source performs the real vector SELECT and both consistency fences.
"""
from collections.abc import Mapping
from datetime import datetime, timezone
import math
import re

SCHEMA = 'postgres_candidate_readiness.v1'
ORIGIN = 'native_readonly_vector_query'
MAX_AGE_SECONDS = 30
FLAGS = ('ready', 'enabled', 'configured', 'embedding_available', 'index_verified',
         'backend_query_verified', 'authority_unchanged', 'fingerprints_match', 'revision_matches')


def _finite(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def sanitize_readiness(value):
    value = value if isinstance(value, Mapping) else {}
    result = {key: value.get(key) is True for key in FLAGS}
    result.update(schema=SCHEMA if value.get('schema') == SCHEMA else '',
                  measurement_origin=ORIGIN if value.get('measurement_origin') == ORIGIN else '')
    for key, pattern in (('watermark', r'[A-Za-z0-9_.:-]{1,256}'),
                         ('authority_revision', r'[0-9]{1,64}')):
        text = value.get(key)
        result[key] = text if isinstance(text, str) and re.fullmatch(pattern, text) else ''
    for key in ('last_error', 'error'):
        text = value.get(key)
        result[key] = text if isinstance(text, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{0,80}', text) else 'readiness_probe_unavailable'
    circuit = value.get('circuit')
    result['circuit'] = circuit if circuit in ('closed', 'open', 'half_open') else 'open'
    lag = value.get('lag_seconds')
    result['lag_seconds'] = lag if _finite(lag) else None
    stamp = value.get('observed_at')
    result['observed_at'] = ''
    try:
        if isinstance(stamp, str) and len(stamp) <= 64:
            parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                result['observed_at'] = stamp
    except ValueError:
        pass
    result['ready'] = bool(all(result[key] is True for key in FLAGS)
        and result['schema'] == SCHEMA and result['measurement_origin'] == ORIGIN
        and result['watermark'] and result['authority_revision'] and result['observed_at']
        and result['circuit'] == 'closed' and result['last_error'] == result['error'] == ''
        and _finite(result['lag_seconds']) and 0 <= result['lag_seconds'] <= 60)
    return result


def readiness_ok(value, *, at=None):
    if not isinstance(value, Mapping):
        return False
    clean = sanitize_readiness(value)
    try:
        reference = datetime.now(timezone.utc) if at is None else (
            datetime.fromisoformat(at.replace('Z', '+00:00')) if isinstance(at, str) else at)
        observed = datetime.fromisoformat(clean['observed_at'].replace('Z', '+00:00'))
        age = (reference - observed).total_seconds()
        return bool(reference.tzinfo is not None and -5 <= age <= MAX_AGE_SECONDS
            and clean['schema'] == SCHEMA and clean['measurement_origin'] == ORIGIN
            and all(clean[key] is True for key in FLAGS)
            and clean['circuit'] == 'closed' and clean['last_error'] == clean['error'] == ''
            and clean['watermark'] and clean['authority_revision']
            and type(clean['lag_seconds']) in (int, float) and 0 <= clean['lag_seconds'] <= 60)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return False
