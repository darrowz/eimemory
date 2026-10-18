"""Protected historical business sources, measured through native recall.

Historical origin and controlled validation are separate facts.  This module
never claims an online natural query, delivery, or memory contribution.  It
requires an operator's exact physical source allowlist, then rechecks the
native origin and all supporting records without creating or changing memory.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from pathlib import PurePosixPath
import re

from eimemory.evaluation import controlled_memory_benchmark as benchmark
from eimemory.governance.learning.effect_policy import digest
from eimemory.metadata import business_metadata

PURPOSE = 'historical_business_qualification'
SCHEMA = PURPOSE + '.v1'
PROVENANCE_SCHEMA = 'historical_business_provenance.v1'
_SCOPE_KEYS = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}
_BINDING_KEYS = {'record_id', 'source_id', 'scope', 'payload_digest'}
_SOURCES = {'hermes.memory', 'hermes.l1'}
_HEX = re.compile('[0-9a-f]{64}')
_MARKERS = {'synthetic', 'is_synthetic', 'acceptance_generated', 'benchmark',
            'is_benchmark', 'test', 'is_test', 'test_only', 'fixture', 'mock',
            'evaluation_only', 'outcome_pollution'}
_ORIGIN_KEYS = {'source', 'origin', 'capture_origin', 'write_origin',
                'sample_origin', 'traffic_kind', 'purpose', 'record_purpose',
                'event_origin'}
_ORIGIN_TOKENS = {'synthetic', 'benchmark', 'test', 'pytest', 'fixture', 'mock',
                  'acceptance', 'evaluation', 'simulated', 'simulation'}


def _error(code):
    # The native owner imports these helpers; defer the reverse import.
    from eimemory.evaluation.controlled_production_acceptance import _error as fail
    fail(code)


def _text(value):
    return isinstance(value, str) and bool(value) and value.strip() == value and '\x00' not in value


def _valid_scope(value):
    return (isinstance(value, dict) and set(value) == _SCOPE_KEYS
            and all(_text(item) and not any(mark in item for mark in '*?[]') for item in value.values()))


def _ref_key(binding):
    scope = binding['scope']
    return tuple(scope[key] for key in ('tenant_id', 'agent_id', 'workspace_id', 'user_id')) + (
        binding['source_id'], binding['record_id'])


def _valid_binding(binding):
    return (isinstance(binding, dict) and set(binding) == _BINDING_KEYS
            and _valid_scope(binding.get('scope'))
            and _text(binding.get('record_id')) and _text(binding.get('source_id'))
            and not any(mark in binding['source_id'] for mark in '*?[]')
            and isinstance(binding.get('payload_digest'), str)
            and _HEX.fullmatch(binding['payload_digest']) is not None)


def _valid_version(version):
    return (isinstance(version, list) and len(version) == 3
            and all(isinstance(value, str) and _HEX.fullmatch(value) for value in version[:2])
            and _text(version[2]))


def validate_business_policy(policy):
    """Validate optional purpose without changing legacy policy serialization.

    The caller first validates the ordinary controlled policy and agent scopes.
    An allowlist entry binds one exact source and all three physical SQL tokens;
    a source label or caller-provided metadata grants no business authority.
    """
    if 'purpose' not in policy:
        if 'business_sources' in policy:
            _error('business_policy_invalid')
        return policy
    if policy.get('purpose') != PURPOSE:
        _error('business_policy_invalid')
    allowed = policy.get('business_sources')
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= 128:
        _error('business_sources_invalid')
    agents = policy['agents']
    counts = [0] * len(agents)
    seen = set()
    for entry in allowed:
        if (not isinstance(entry, dict) or set(entry) != {'source_binding', 'source_version', 'origin_source'}
                or not _valid_binding(entry.get('source_binding'))
                or not _valid_version(entry.get('source_version'))
                or not isinstance(entry.get('origin_source'), str)
                or entry['origin_source'] not in _SOURCES):
            _error('business_sources_invalid')
        binding = entry['source_binding']
        matches = [index for index, agent in enumerate(agents)
                   if binding['scope'] == agent['scope'] and binding['source_id'] in agent['source_ids']]
        key = _ref_key(binding)
        if len(matches) != 1 or key in seen:
            _error('business_sources_invalid')
        seen.add(key)
        counts[matches[0]] += 1
    if any(not policy['minimum_cases_per_scope'] <= count <= policy['max_cases_per_scope'] for count in counts):
        _error('business_sources_invalid')
    return policy


def _pollution(value, *, depth=0, remaining=None):
    """Inspect explicit provenance declarations, never free-form business text."""
    if remaining is None:
        remaining = [4096]
    remaining[0] -= 1
    if depth > 16 or remaining[0] < 0:
        return True
    if isinstance(value, dict):
        for key, child in value.items():
            name = str(key).strip().lower().replace('-', '_')
            if name in _MARKERS and child not in (None, False, 0, '', 'false', '0', 'no'):
                return True
            if name in _ORIGIN_KEYS and isinstance(child, str):
                if _ORIGIN_TOKENS.intersection(re.split(r'[^a-z0-9]+', child.lower())):
                    return True
            if isinstance(child, (dict, list)) and _pollution(child, depth=depth + 1, remaining=remaining):
                return True
    elif isinstance(value, list):
        return any(_pollution(child, depth=depth + 1, remaining=remaining) for child in value)
    return False


def _origin_metadata(record):
    meta = business_metadata(record.meta)
    quality = meta.get('quality')
    if (meta.get('authoritative') is not True or meta.get('runtime_channel') != 'hermes'
            or not _text(meta.get('source_event_id'))
            or not isinstance(quality, dict) or quality.get('capture_decision') != 'accept'
            or _pollution({'meta': record.meta, 'provenance': record.provenance})
            or any(str(tag).strip().lower().replace('-', '_') in _MARKERS for tag in record.tags)):
        _error('business_origin_invalid')
    return meta


def _native_import(record, meta):
    native = meta.get('native_import')
    if not isinstance(native, dict):
        _error('business_origin_invalid')
    filename = native.get('file')
    text = record.content.get('text')
    if (native.get('schema') != 'hermes_native_memory_import.v1'
            or not _text(filename)
            or not (filename.startswith('/') or re.match(r'^[A-Za-z]:[/\\]', filename))
            or PurePosixPath(filename.replace('\\', '/')).name not in {'MEMORY.md', 'USER.md'}
            or type(native.get('line')) is not int or native['line'] < 1
            or any(not isinstance(native.get(key), str) or not _HEX.fullmatch(native[key])
                   for key in ('file_digest', 'text_digest'))
            or not isinstance(text, str) or not text.strip()
            or sha256(text.strip().encode('utf-8')).hexdigest() != native['text_digest']):
        _error('business_origin_invalid')


def business_provenance(runtime, binding, version, scope, sources, cutoff, policy):
    """Recheck an authorized old business source and return only safe bindings."""
    from eimemory.evaluation.controlled_production_acceptance import _physical
    if policy.get('purpose') != PURPOSE:
        _error('business_policy_invalid')
    matches = [entry for entry in policy['business_sources'] if entry['source_binding'] == binding]
    if len(matches) != 1 or matches[0]['source_version'] != version:
        _error('business_source_not_authorized')
    if _physical(runtime, binding, scope, sources, cutoff) != version:
        _error('business_source_changed')
    record = runtime.store.get_by_exact_ref(binding['record_id'], scope=scope, source_id=binding['source_id'])
    if (record is None or benchmark._binding(record) != binding
            or record.source != matches[0]['origin_source']):
        _error('business_source_changed')
    meta = _origin_metadata(record)
    parents = []
    if record.source == 'hermes.memory':
        _native_import(record, meta)
    elif record.source == 'hermes.l1':
        message_ids = meta.get('source_message_ids')
        if (meta.get('capture_origin') != 'l1_extract'
                or not isinstance(message_ids, list) or not 1 <= len(message_ids) <= 128
                or any(not _text(item) for item in message_ids)):
            _error('business_origin_invalid')
        links = {link.target_id for link in record.links
                 if link.relation == 'derived_from' and link.target_kind in {'memory', 'record'}}
        if not 1 <= len(links) <= 2:
            _error('business_origin_invalid')
        for parent_id in sorted(links):
            parent = runtime.store.get_by_exact_ref(parent_id, scope=scope, source_id=binding['source_id'])
            if parent is None or parent.scope != scope or parent.source_id != binding['source_id'] or parent.source != 'hermes.memory':
                _error('business_origin_parent_invalid')
            parent_meta = _origin_metadata(parent)
            if (parent_meta.get('capture_origin') != 'turn_sync'
                    or not _text(parent_meta.get('session_id')) or not _text(parent_meta.get('turn_id'))):
                _error('business_origin_parent_invalid')
            parent_binding = benchmark._binding(parent)
            parent_version = _physical(runtime, parent_binding, scope, sources, cutoff,
                                       require_old=False, episode=True)
            parents.append({'binding': parent_binding, 'version': parent_version})
    else:
        _error('business_origin_invalid')
    return {'schema': PROVENANCE_SCHEMA, 'origin_source': record.source,
            'source_metadata_digest': digest({'metadata': meta, 'provenance': record.provenance,
                'source': record.source, 'links': [asdict(link) for link in record.links],
                'evidence': record.evidence}), 'parents': parents}


def business_agent_summaries(body):
    """Derive bounded public counts from the native case observations."""
    agents = []
    for entry in body.get('agents', []):
        samples = entry['samples']
        count = len(samples)
        passed = sum(sample.get('passed') is True and sample.get('complete') is True for sample in samples)
        complete = bool(samples) and all(sample.get('complete') is True for sample in samples)
        agents.append({'agent_id': entry['scope']['agent_id'], 'scope_digest': digest(entry['scope']),
                       'source_ids': deepcopy(entry['source_ids']), 'sample_count': count,
                       'pass_count': passed, 'fail_count': count - passed,
                       'accepted': entry.get('accepted') is True and complete and passed == count,
                       'complete': complete, 'blocked_reason': entry.get('blocked_reason', '')})
    return agents


def business_qualification(body):
    """Describe historical business qualification without inventing live traffic.

    The native owner alone signs and verifies this description.  Callers must
    still require a persisted, currently verified receipt before admission.
    """
    if body.get('acceptance_purpose') != PURPOSE:
        return {}
    agents = business_agent_summaries(body)
    count = sum(agent['sample_count'] for agent in agents)
    passed = sum(agent['pass_count'] for agent in agents)
    return {'schema': SCHEMA, 'purpose': PURPOSE,
            'accepted': body.get('accepted') is True and bool(agents) and all(agent['accepted'] for agent in agents),
            'business_origin': 'historical_business', 'validation_method': 'controlled_recall',
            'sample_count': count, 'pass_count': passed, 'fail_count': count - passed,
            'agents': agents, 'policy_digest': body.get('policy_digest', ''),
            'record_id': body.get('record_id', ''), 'release_identity': deepcopy(body.get('release_identity', {}))}
