from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
from types import SimpleNamespace

import pytest

from eimemory.evaluation import controlled_production_acceptance as native
from eimemory.evaluation import controlled_memory_benchmark as benchmark
from eimemory.evaluation import historical_business_qualification as business
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef

SCOPE = ScopeRef(agent_id='hongtai', workspace_id='business::channel::hermes', user_id='member')
CUTOFF = '2026-10-18T00:00:00Z'


def record(index=0, *, source='hermes.memory', scope=SCOPE, **kwargs):
    text = f'Historical project {index} uses an established delivery process.'
    meta = {'authoritative': True, 'runtime_channel': 'hermes', 'source_event_id': f'old-event-{index}',
            'memory_type': 'fact', 'quality': {'capture_decision': 'accept'},
            'native_import': {'schema': 'hermes_native_memory_import.v1',
                              'file': '/home/member/.hermes/memories/MEMORY.md',
                              'line': index + 1, 'file_digest': 'a' * 64,
                              'text_digest': sha256(text.encode()).hexdigest()}}
    meta.update(kwargs.pop('meta', {}))
    item = RecordEnvelope.create(kind='memory', title=text, content={'text': text},
                                scope=scope, source=source, source_id='hermes', meta=meta, **kwargs)
    item.time.created_at = '2026-09-01T00:00:00Z'
    return item


def version(item):
    return [benchmark._binding(item)['payload_digest'], 'b' * 64, 'physical-' + item.record_id]


def policy(items):
    return {'purpose': business.PURPOSE,
            'agents': [{'scope': asdict(SCOPE), 'source_ids': ['hermes']}],
            'minimum_cases_per_scope': 3, 'max_cases_per_scope': 3,
            'business_sources': [{'source_binding': benchmark._binding(item), 'source_version': version(item),
                                  'origin_source': item.source} for item in items]}


@pytest.fixture
def setup(monkeypatch):
    items = [record(index) for index in range(3)]
    records = {item.record_id: item for item in items}
    calls = []
    def get(record_id, *, scope, source_id):
        item = records.get(record_id)
        return deepcopy(item) if item is not None and item.scope == scope and item.source_id == source_id else None
    def physical(runtime, binding, scope, sources, cutoff, **kwargs):
        calls.append((binding, kwargs))
        item = get(binding['record_id'], scope=scope, source_id=binding['source_id'])
        if item is None or benchmark._binding(item) != binding:
            native._error('source_version_changed')
        return version(item)
    monkeypatch.setattr(native, '_physical', physical)
    runtime = SimpleNamespace(store=SimpleNamespace(get_by_exact_ref=get))
    return SimpleNamespace(items=items, records=records, runtime=runtime, policy=policy(items), calls=calls)


def provenance(setup, index=0):
    item = setup.items[index]
    return business.business_provenance(setup.runtime, benchmark._binding(item), version(item),
                                        SCOPE, ('hermes',), CUTOFF, setup.policy)


def test_absent_purpose_preserves_legacy_policy_bytes_and_cannot_accept_an_allowlist():
    value = {'agents': []}
    original = deepcopy(value)
    assert business.validate_business_policy(value) is value and value == original
    assert business.business_qualification({}) == {}
    with pytest.raises(native.ControlledAcceptanceError, match='business_policy_invalid'):
        business.validate_business_policy({**value, 'business_sources': []})


def test_exact_source_allowlist_validates_without_mutation(setup):
    before = deepcopy(setup.policy)
    assert business.validate_business_policy(setup.policy) is setup.policy
    assert setup.policy == before


@pytest.mark.parametrize('change', ['purpose', 'missing', 'empty', 'over_limit', 'entry_extra', 'binding_extra',
    'origin', 'origin_type', 'wildcard', 'scope', 'source_id', 'payload', 'version_bool', 'storage_key',
    'duplicate', 'too_few', 'too_many', 'agent_missing'])
def test_policy_refuses_ambiguous_or_unbound_business_sources(setup, change):
    value = deepcopy(setup.policy)
    entry = value['business_sources'][0]
    if change == 'purpose': value['purpose'] = 'natural'
    if change == 'missing': value.pop('business_sources')
    if change == 'empty': value['business_sources'] = []
    if change == 'over_limit': value['business_sources'] *= 43
    if change == 'entry_extra': entry['accepted'] = True
    if change == 'binding_extra': entry['source_binding']['accepted'] = True
    if change == 'origin': entry['origin_source'] = 'synthetic.memory'
    if change == 'origin_type': entry['origin_source'] = []
    if change == 'wildcard': entry['source_binding']['scope']['user_id'] = '*'
    if change == 'scope': entry['source_binding']['scope']['user_id'] = 'other'
    if change == 'source_id': entry['source_binding']['source_id'] = 'other'
    if change == 'payload': entry['source_binding']['payload_digest'] = 'not-a-digest'
    if change == 'version_bool': entry['source_version'][1] = True
    if change == 'storage_key': entry['source_version'][2] = ''
    if change == 'duplicate': value['business_sources'][1] = deepcopy(entry)
    if change == 'too_few': value['business_sources'].pop()
    if change == 'too_many': value['business_sources'].append({**deepcopy(entry),
        'source_binding': {**deepcopy(entry['source_binding']), 'record_id': 'another-memory'}})
    if change == 'agent_missing': value['agents'].append({'scope': {**asdict(SCOPE), 'agent_id': 'hongxin'}, 'source_ids': ['hermes']})
    with pytest.raises(native.ControlledAcceptanceError, match='business_.*invalid'):
        business.validate_business_policy(value)


def test_native_file_origin_is_physical_bound_and_never_exports_text_or_path(setup):
    result = provenance(setup)
    assert result['schema'] == business.PROVENANCE_SCHEMA
    assert result['origin_source'] == 'hermes.memory' and result['parents'] == []
    assert len(result['source_metadata_digest']) == 64
    assert len(setup.calls) == 1
    assert 'Historical project' not in str(result) and '/home/' not in str(result)


@pytest.mark.parametrize('field,value', [('schema', 'other'), ('file', '/tmp/fixture.txt'),
    ('file', 'MEMORY.md'), ('line', True), ('line', 0), ('file_digest', ''), ('text_digest', 'c' * 64)])
def test_native_import_requires_bound_original_text_and_well_formed_origin(setup, field, value):
    for location in (setup.items[0].meta, setup.items[0].meta['business_meta']):
        location['native_import'][field] = value
    setup.records[setup.items[0].record_id] = setup.items[0]
    setup.policy = policy(setup.items)
    with pytest.raises(native.ControlledAcceptanceError, match='business_origin_invalid'):
        provenance(setup)


@pytest.mark.parametrize('change', ['not_authorized', 'version', 'source_label', 'physical_changed'])
def test_allowlist_and_current_physical_source_are_independent_requirements(setup, change):
    if change == 'not_authorized': setup.policy['business_sources'].pop(0)
    if change == 'version': setup.policy['business_sources'][0]['source_version'][2] = 'another-storage-key'
    if change == 'source_label': setup.policy['business_sources'][0]['origin_source'] = 'hermes.l1'
    if change == 'physical_changed': setup.records[setup.items[0].record_id].content['text'] = 'Changed business source.'
    with pytest.raises(native.ControlledAcceptanceError):
        provenance(setup)


def test_canonical_binding_digest_and_raw_sql_version_digest_are_separate_authorities(setup, monkeypatch):
    entry = setup.policy['business_sources'][0]
    binding = entry['source_binding']
    physical = ['f' * 64, 'b' * 64, entry['source_version'][2]]
    assert physical[0] != binding['payload_digest']
    entry['source_version'] = physical
    business.validate_business_policy(setup.policy)
    monkeypatch.setattr(native, '_physical', lambda *args, **kwargs: physical[:])
    result = business.business_provenance(setup.runtime, binding, physical,
                                         SCOPE, ('hermes',), CUTOFF, setup.policy)
    assert result['origin_source'] == 'hermes.memory'
    # A proven legacy normalization may have different canonical/raw hashes;
    # this never allows any of the actual physical SQL tokens to drift.
    for index in range(3):
        changed = physical[:]
        changed[index] = 'e' * 64 if index < 2 else 'different-storage-key'
        monkeypatch.setattr(native, '_physical', lambda *args, changed=changed, **kwargs: changed)
        with pytest.raises(native.ControlledAcceptanceError, match='business_source_changed'):
            business.business_provenance(setup.runtime, binding, physical,
                                         SCOPE, ('hermes',), CUTOFF, setup.policy)


@pytest.mark.parametrize('marker', ['synthetic', 'acceptance_generated', 'benchmark', 'test_only', 'outcome_pollution'])
def test_explicit_synthetic_markers_cannot_be_whitelisted_as_business(setup, marker):
    setup.items[0].meta['business_meta'][marker] = True
    setup.policy = policy(setup.items)
    with pytest.raises(native.ControlledAcceptanceError, match='business_origin_invalid'):
        provenance(setup)


def l1_setup(setup):
    parent = record(10, meta={'capture_origin': 'turn_sync', 'session_id': 'live-session',
                             'turn_id': 'live-turn', 'memory_type': 'conversation', 'memory_layer': 'l0'})
    child = record(0, source='hermes.l1', meta={'capture_origin': 'l1_extract', 'memory_layer': 'l1',
        'source_message_ids': ['message-10'], 'force_capture': True},
        links=[LinkRef(relation='derived_from', target_kind='memory', target_id=parent.record_id)],
        evidence=['message-10'])
    setup.items[0] = child
    setup.records[child.record_id] = child
    setup.records[parent.record_id] = parent
    setup.policy = policy(setup.items)
    return parent, child


def test_actual_l1_history_keeps_force_capture_and_binds_same_scope_parent(setup):
    parent, child = l1_setup(setup)
    result = provenance(setup)
    assert result['origin_source'] == 'hermes.l1'
    assert result['parents'] == [{'binding': benchmark._binding(parent), 'version': version(parent)}]
    assert setup.calls[-1][1] == {'require_old': False, 'episode': True}
    assert child.provenance == {} and child.meta['force_capture'] is True
    assert 'live-session' not in str(result)
    before = deepcopy(result)
    parent.content['text'] = 'Actual source edited later.'
    assert provenance(setup) != before, 'Native owner must compare and physically revalidate the recorded parent tokens.'


@pytest.mark.parametrize('change', ['missing', 'foreign_scope', 'foreign_source', 'bad_origin', 'session', 'turn',
    'rejected', 'synthetic', 'no_link', 'no_messages', 'too_many_parents'])
def test_l1_requires_actual_non_synthetic_host_turn_parent(setup, change):
    parent, child = l1_setup(setup)
    if change == 'missing': setup.records.pop(parent.record_id)
    if change == 'foreign_scope': parent.scope = ScopeRef(agent_id='other', workspace_id=SCOPE.workspace_id, user_id=SCOPE.user_id)
    if change == 'foreign_source': parent.source = 'operator.memory'
    if change == 'bad_origin': parent.meta['capture_origin'] = 'fixture'
    if change == 'session': parent.meta['session_id'] = ''
    if change == 'turn': parent.meta['turn_id'] = ''
    if change == 'rejected': parent.meta['quality']['capture_decision'] = 'reject'
    if change == 'synthetic': parent.meta['acceptance_generated'] = True
    if change == 'no_link': child.links = []
    if change == 'no_messages': child.meta['source_message_ids'] = []
    if change == 'too_many_parents':
        child.links.extend([LinkRef(relation='derived_from', target_kind='memory', target_id=f'mem_extra-{index}') for index in range(2)])
    setup.policy = policy(setup.items)
    with pytest.raises(native.ControlledAcceptanceError):
        provenance(setup)


def test_business_counts_derive_native_samples_and_never_claim_online_natural_delivery():
    entry = {'scope': asdict(SCOPE), 'source_ids': ['hermes'], 'sample_count': 99, 'pass_count': 99,
             'accepted': True, 'blocked_reason': '',
             'samples': [{'passed': True, 'complete': True} for _ in range(3)]}
    body = {'acceptance_purpose': business.PURPOSE, 'accepted': True, 'agents': [entry],
            'policy_digest': 'b' * 64, 'record_id': 'cpa_test', 'release_identity': {'release_commit': 'a' * 40}}
    report = business.business_qualification(body)
    assert report['accepted'] and report['sample_count'] == report['pass_count'] == 3
    assert report['fail_count'] == 0 and report['agents'][0]['sample_count'] == 3
    assert report['business_origin'] == 'historical_business' and report['validation_method'] == 'controlled_recall'
    assert not {'certifies_natural_queries', 'host_delivery_certified', 'memory_contribution_certified'} & set(report)
    body['agents'][0]['samples'][0]['complete'] = False
    failed = business.business_qualification(body)
    assert not failed['accepted'] and failed['pass_count'] == 2 and failed['fail_count'] == 1
    assert not failed['agents'][0]['complete']


def test_real_sqlite_parent_versions_are_rechecked_by_native_atomic_snapshot(tmp_path):
    from eimemory.api.runtime import Runtime
    from eimemory.identity_ops import _repair_snapshot

    parent = record(10, meta={'capture_origin': 'turn_sync', 'session_id': 'actual-session',
        'turn_id': 'actual-turn', 'memory_type': 'conversation', 'memory_layer': 'l0'})
    child = record(0, source='hermes.l1', meta={'capture_origin': 'l1_extract', 'memory_layer': 'l1',
        'source_message_ids': ['actual-message'], 'force_capture': True},
        links=[LinkRef(relation='derived_from', target_kind='memory', target_id=parent.record_id)])
    items = [child, record(1), record(2)]
    with Runtime.create(root=tmp_path / 'runtime') as runtime:
        for item in [parent, *items]:
            runtime.store.append(item)
        def actual_version(item):
            ref = (*[getattr(item.scope, key) for key in ('tenant_id', 'agent_id', 'workspace_id', 'user_id')],
                   item.source_id, item.record_id)
            return list(runtime.store.read_consistent(lambda sqlite: _repair_snapshot(sqlite, ref))[1])
        authorized = policy(items)
        for entry, item in zip(authorized['business_sources'], items):
            entry['source_version'] = actual_version(item)
        business.validate_business_policy(authorized)
        binding = benchmark._binding(child)
        measured = business.business_provenance(runtime, binding, actual_version(child),
                                               SCOPE, ('hermes',), CUTOFF, authorized)
        assert measured['parents'] == [{'binding': benchmark._binding(parent), 'version': actual_version(parent)}]
        sample = {'source_binding': binding, 'source_version': actual_version(child), 'returned': [],
                  'returned_versions': [], 'business_provenance': measured}
        runtime.store.read_consistent(lambda sqlite: native._validate_versions(sqlite, [{'samples': [sample]}]))
        parent.content['text'] = 'The actual historic source was subsequently corrected.'
        runtime.store.append(parent)
        with pytest.raises(native.ControlledAcceptanceError, match='source_version_changed'):
            runtime.store.read_consistent(lambda sqlite: native._validate_versions(sqlite, [{'samples': [sample]}]))
