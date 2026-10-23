from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation import controlled_production_acceptance as cpa
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef

OWNER = ScopeRef(agent_id='hongrui', workspace_id='embodied', user_id='operator')
SCOPES = [ScopeRef(agent_id=agent, workspace_id='embodied::channel::hermes', user_id='member-' + agent)
          for agent in ('hongrui', 'hongtai', 'hongxin', 'xiaomage')]
RELEASE = dict(release_commit='a' * 40, release_version='1.14.140',
               deployment_receipt_id='receipt-fixture', release_session_id='session-fixture')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz')
    monkeypatch.delenv(cpa.POLICY_ENV, raising=False)
    monkeypatch.delenv('EIMEMORY_RECALL_GATE_AGENTS', raising=False)
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    policy = dict(schema=cpa.POLICY_SCHEMA, mode=cpa.MODE, request_digest='b' * 64,
        owner_scope=asdict(OWNER), agents=[dict(scope=asdict(s), source_ids=['hermes']) for s in SCOPES],
        stable_before=cutoff, minimum_cases_per_scope=3, max_cases_per_scope=3, freshness_seconds=3600)
    monkeypatch.setattr(cpa, '_read_policy', lambda path: deepcopy(policy))
    monkeypatch.setenv(cpa.POLICY_ENV, str(tmp_path / 'policy.json'))
    monkeypatch.setattr(cpa, '_release', lambda runtime, scope: (deepcopy(RELEASE), {'record_ref': 'receipt-fixture', 'record_digest': 'c' * 64}))
    with Runtime.create(root=tmp_path / 'runtime') as runtime:
        memories = {}
        for scope in SCOPES:
            records = []
            for index in range(3):
                record = RecordEnvelope.create(kind='memory', title=f'{scope.agent_id} known project {index}',
                    summary=f'Project {index} uses an established green notebook.',
                    content={'text': f'Project {index} uses an established green notebook.', 'memory_type': 'fact'},
                    source='operator.memory', source_id='hermes', scope=scope, meta={'memory_type': 'fact'})
                record.time.created_at = '2020-01-01T00:00:00Z'
                record.time.updated_at = '2020-01-01T00:00:00Z'
                runtime.store.append(record);records.append(record)
            memories[scope.agent_id] = records
        calls = []
        def recall(**kwargs):
            calls.append(kwargs)
            scope = ScopeRef.from_dict(kwargs['scope'])
            record = next(r for r in memories[scope.agent_id] if r.title == kwargs['query'])
            return RecallBundle(items=[record], rules=[], reflections=[], confidence=1, next_action_hint='',
                explanation={'retrieval_status': 'evidence_found',
                    'relevance_selector': {'status': 'evidence_found', 'collection_complete': True},
                    'engine_diagnostics': {'source_searches': 1, 'source_budget_exhausted': 0, 'drops': {}, 'fallback': False}})
        monkeypatch.setattr(runtime.memory, 'recall', recall)
        policy['engine_contract_digest'] = cpa.digest(cpa.controlled_retrieval_contract(runtime))
        yield SimpleNamespace(runtime=runtime, policy=policy, memories=memories, calls=calls,
                              recall=recall, monkeypatch=monkeypatch)


def run(setup, **kwargs):
    return cpa.run_controlled_production_acceptance(setup.runtime, scope=asdict(OWNER), **kwargs)


def verified(setup, **kwargs):
    return cpa.verify_controlled_production_acceptance(setup.runtime, scope=asdict(OWNER), **kwargs)


def configured_pg(setup):
    """Native source type with an isolated measured-readiness test boundary."""
    from eimemory.retrieval.postgres_vector import IndexState, PostgresVectorCandidateSource
    from test_candidate_readiness_barrier import ready_measurement
    engine = setup.runtime.memory.recall_engine
    raw = deepcopy(engine.effective_identity())
    raw['candidate_source'].update(candidate_source_type='PostgresVectorCandidateSource',
        enabled=True, configured=True, config_fingerprint='e' * 64, projection_fingerprint='f' * 64,
        embedding={'provider_type': 'fixture', 'model': 'fixture', 'fingerprint': 'a' * 64},
        postgres={'state': 'bypassed', 'committed_watermark': '', 'index_revision': '1',
                  'circuit': 'closed', 'index_verified': False, 'query_valid': False, 'bypass_reason': 'cold'})
    state = IndexState(ready=True, watermark='old-watermark', authority_revision='1',
        embedding_fingerprint='a' * 64, projection_digest_schema='fixture.v1', projection_fingerprint='f' * 64)
    source = PostgresVectorCandidateSource.__new__(PostgresVectorCandidateSource)
    source.repository = SimpleNamespace(read_index_state=lambda: state)
    setup.monkeypatch.setattr(source, 'readiness', lambda **kwargs: ready_measurement())
    setup.monkeypatch.setattr(engine, 'effective_identity', lambda: deepcopy(raw))
    setup.monkeypatch.setattr(engine, 'candidate_source', source)
    setup.policy['engine_contract_digest'] = cpa.digest(cpa.controlled_retrieval_contract(setup.runtime))
    return source, raw


def test_native_seedless_all_four_scopes_signed_receipt_revalidates_without_natural_labels(setup):
    before = setup.runtime.store.count_records(kinds=['memory'])
    report = run(setup)
    assert report['ok'] and report['accepted'] and len(report['agents']) == 4
    assert len(setup.calls) == 12
    assert all(a['sample_count'] == a['pass_count'] == 3 and a['complete'] for a in report['agents'])
    assert report['natural_samples_created'] == 0
    assert not report['certifies_natural_queries'] and not report['certifies_no_answer']
    assert setup.runtime.store.count_records(kinds=['memory']) == before
    assert setup.runtime.store.count_records(kinds=['evaluation_packet']) == 1
    result = verified(setup, record_id=report['record_id'])
    assert result['ok'] and result['record_id'] == report['record_id'], result
    assert len(setup.calls) == 12, 'verification cannot run recall or a judge'
    assert 'member-hongtai' not in json.dumps(result) and 'green notebook' not in json.dumps(result)


def test_dry_run_cannot_be_imported_as_persisted_acceptance(setup):
    report = run(setup, persist=False)
    assert report['passed_threshold'] and not report['accepted'] and not report['record_id'] and not report['persisted']
    assert not verified(setup)['ok']


def test_old_targets_can_coexist_with_valid_newer_same_scope_results(setup):
    newer={}
    for scope in SCOPES:
        record=RecordEnvelope.create(kind='memory',title='New live fact after the old target cutoff',
            summary='A newly captured legitimate live fact.',content={'text':'A legitimate new fact.','memory_type':'fact'},
            source='operator.memory',source_id='hermes',scope=scope,meta={'memory_type':'fact'})
        setup.runtime.store.append(record);newer[scope.agent_id]=record
    def recall(**kwargs):
        bundle=setup.recall(**kwargs);bundle.items.append(newer[kwargs['scope']['agent_id']]);return bundle
    setup.monkeypatch.setattr(setup.runtime.memory,'recall',recall)
    report=run(setup)
    assert report['accepted'],report
    assert verified(setup,record_id=report['record_id'])['accepted']


def test_disabled_vector_with_complete_lexical_retrieval_is_not_a_failure(setup):
    def recall(**kwargs):
        bundle = setup.recall(**kwargs)
        engine = bundle.explanation['engine_diagnostics']
        engine['fallback'] = True
        engine['fallback_error_codes'] = {'disabled': 1}
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', recall)
    report = run(setup)
    assert report['accepted'], report
    stored = setup.runtime.store.get_by_exact_ref(report['record_id'], scope=OWNER, source_id='default')
    state = stored.content['agents'][0]['samples'][0]['state']
    assert state['fallback'] is True
    assert state['fallback_error_codes'] == {'disabled': 1}


@pytest.mark.parametrize('codes', [{'recall_budget_exhausted': 1}, {'unknown': 1}, {'postgres_timeout': 1}, {}])
def test_real_or_unknown_vector_bypass_still_fails_acceptance(setup, codes):
    def recall(**kwargs):
        bundle = setup.recall(**kwargs)
        engine = bundle.explanation['engine_diagnostics']
        engine['fallback'] = True
        if codes:
            engine['fallback_error_codes'] = dict(codes)
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', recall)
    assert not run(setup)['accepted']


@pytest.mark.parametrize('change', ['collection', 'selector', 'top', 'source_budget', 'recall_budget',
    'missing_source_budget', 'missing_drops', 'truncated_drops', 'fallback', 'missing_fallback',
    'boolean_budget', 'negative_budget', 'none_budget'])
def test_final_hit_never_certifies_incomplete_unknown_or_fallback_retrieval(setup, change):
    def recall(**kwargs):
        bundle = setup.recall(**kwargs)
        selector = bundle.explanation['relevance_selector'];engine = bundle.explanation['engine_diagnostics']
        if change == 'collection': selector['collection_complete'] = False
        if change == 'selector': selector['status'] = 'degraded'
        if change == 'top': bundle.explanation['retrieval_status'] = 'degraded'
        if change == 'source_budget': engine['source_budget_exhausted'] = 2
        if change == 'recall_budget': engine['drops']['recall_budget_exhausted'] = 2
        if change == 'missing_source_budget': engine.pop('source_budget_exhausted')
        if change == 'missing_drops': engine.pop('drops')
        if change == 'truncated_drops': engine['drops'] = {str(i): 1 for i in range(8)}
        if change == 'fallback': engine['fallback'] = True
        if change == 'missing_fallback': engine.pop('fallback')
        if change == 'boolean_budget': engine['source_budget_exhausted'] = False
        if change == 'negative_budget': engine['drops']['recall_budget_exhausted'] = -1
        if change == 'none_budget': engine['drops']['recall_budget_exhausted'] = None
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', recall)
    report = run(setup)
    assert not report['accepted'] and not verified(setup)['accepted']


@pytest.mark.parametrize('section', ['items', 'rules', 'reflections'])
def test_every_actual_returned_section_is_exact_scope_and_source_checked(setup, section):
    foreign = deepcopy(setup.memories['hongtai'][0])
    def recall(**kwargs):
        bundle = setup.recall(**kwargs)
        if kwargs['scope']['agent_id'] == 'hongrui': getattr(bundle, section).append(foreign)
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', recall)
    assert not run(setup)['accepted']


@pytest.mark.parametrize('change', ['payload', 'source', 'status', 'policy', 'release', 'engine', 'hmac', 'expiry', 'duplicate'])
def test_certificate_is_invalidated_by_current_authority_or_signed_evidence_drift(setup, change):
    report = run(setup);assert report['accepted']
    if change in {'payload', 'source', 'status'}:
        record = setup.memories['hongtai'][0]
        if change == 'payload': record.content['text'] = 'Changed after the successful measurement.'
        if change == 'source':
            def move(sqlite):
                sqlite.execute('UPDATE records SET source_id=? WHERE record_id=?', ('other-source', record.record_id))
                sqlite.commit()
            setup.runtime.store.run_locked(move)
        if change == 'status': record.status = 'superseded'
        if change != 'source': setup.runtime.store.append(record)
    elif change == 'policy': setup.policy['request_digest'] = 'd' * 64
    elif change == 'release':
        setup.monkeypatch.setattr(cpa, '_release', lambda runtime, scope: ({**RELEASE, 'release_session_id': 'new-session'}, {}))
    elif change == 'engine':
        setup.monkeypatch.setattr(cpa, '_engine', lambda runtime: {'identity_digest': 'd' * 64})
    else:
        record = setup.runtime.store.get_by_exact_ref(report['record_id'], scope=OWNER, source_id='default')
        if change == 'hmac': record.content['signature'] = '0' * 64
        if change == 'expiry':
            record.content['completed_at'] = '2020-01-01T00:00:00Z'
            record.content['started_at'] = '2020-01-01T00:00:00Z'
            record.content = cpa.seal({k:v for k,v in record.content.items() if k not in {'key_id','signature'}}, cpa.SCHEMA)
        if change == 'duplicate':
            record.content['agents'][0]['samples'][1] = deepcopy(record.content['agents'][0]['samples'][0])
            record.content = cpa.seal({k:v for k,v in record.content.items() if k not in {'key_id','signature'}}, cpa.SCHEMA)
        record.meta['report_digest'] = cpa.digest(record.content)
        setup.runtime.store.append(record)
    assert not verified(setup, record_id=report['record_id'])['ok']


def test_later_failed_attempt_blocks_reuse_of_older_accepted_certificate(setup):
    prior = run(setup);assert prior['accepted']
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', lambda **kw: RecallBundle(
        items=[], rules=[], reflections=[], confidence=0, next_action_hint='', explanation={}))
    failed = run(setup);assert not failed['accepted'] and failed['record_id']
    assert not verified(setup, record_id=prior['record_id'])['ok']
    assert not verified(setup)['ok']


@pytest.mark.parametrize('mutation', ['status', 'release_meta', 'source', 'kind'])
def test_mutable_envelope_cannot_hide_a_newer_failed_high_water(setup, mutation):
    prior = run(setup);assert prior['accepted']
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', lambda **kw: RecallBundle(
        items=[], rules=[], reflections=[], confidence=0, next_action_hint='', explanation={}))
    failed = run(setup);assert failed['record_id'] and not failed['accepted']
    record = setup.runtime.store.get_by_exact_ref(failed['record_id'], scope=OWNER, source_id='default')
    if mutation == 'status':record.status = 'rejected'
    if mutation == 'release_meta':record.meta['release_key'] = '0' * 64
    if mutation == 'source':record.source = 'unrelated.producer'
    if mutation == 'kind':record.kind = 'reflection'
    setup.runtime.store.append(record)
    assert not verified(setup)['accepted']
    assert not verified(setup, record_id=prior['record_id'])['accepted']


@pytest.mark.parametrize('damage', ['scope', 'source', 'wildcard', 'quota', 'age', 'unknown', 'duplicate_agent'])
def test_policy_is_explicit_bounded_scope_authority_before_any_recall(setup, damage):
    policy = setup.policy
    if damage == 'scope': policy['owner_scope']['user_id'] = 'other'
    if damage == 'source': policy['agents'][0]['source_ids'] = []
    if damage == 'wildcard': policy['agents'][0]['scope']['user_id'] = '*'
    if damage == 'quota': policy['minimum_cases_per_scope'] = 2
    if damage == 'age': policy['freshness_seconds'] = 86401
    if damage == 'unknown': policy['accepted'] = True
    if damage == 'duplicate_agent': policy['agents'].append(deepcopy(policy['agents'][0]))
    with pytest.raises(cpa.ControlledAcceptanceError): run(setup)
    assert not setup.calls


def test_missing_default_is_disabled_but_explicit_missing_is_error(tmp_path, monkeypatch):
    monkeypatch.delenv(cpa.POLICY_ENV, raising=False)
    monkeypatch.setattr(cpa, 'DEFAULT_POLICY', str(tmp_path/'absent'/'policy.json'))
    assert cpa.load_controlled_policy(None, OWNER) is None
    monkeypatch.setenv(cpa.POLICY_ENV, str(tmp_path/'absent'/'policy.json'))
    with pytest.raises(cpa.ControlledAcceptanceError): cpa.load_controlled_policy(None, OWNER)


def test_operator_policy_cannot_omit_a_discovered_traffic_agent(setup):
    setup.monkeypatch.setenv('EIMEMORY_RECALL_GATE_AGENTS', 'hongrui,hongtai,hongxin,xiaomage,uncovered-colleague')
    with pytest.raises(cpa.ControlledAcceptanceError, match='traffic_agent_not_authorized'):
        run(setup)
    assert not setup.calls


def test_policy_binds_the_live_runtime_configuration_before_recall(setup):
    setup.policy['engine_contract_digest'] = 'f' * 64
    result = run(setup)
    assert result['blocked_reason'] == 'controlled_acceptance_production_engine_contract_mismatch'
    assert not setup.calls


def test_source_change_after_individual_cases_is_rejected_in_the_append_transaction(setup):
    original=setup.runtime.store.mutate_records_atomically
    def raced(callback):
        source=setup.memories['hongrui'][0]
        source.content['text']='Concurrent authoritative source update before certificate commit.'
        setup.runtime.store.append(source)
        return original(callback)
    setup.monkeypatch.setattr(setup.runtime.store,'mutate_records_atomically',raced)
    result=run(setup)
    assert not result['accepted'] and not result['record_id']
    assert setup.runtime.store.count_records(kinds=['evaluation_packet']) == 0


def test_verification_final_snapshot_catches_change_to_an_already_checked_source(setup):
    report=run(setup);assert report['accepted']
    original=cpa._physical
    count=0
    def raced(*args,**kwargs):
        nonlocal count
        result=original(*args,**kwargs);count+=1
        if count==3:
            source=setup.memories['hongrui'][0]
            source.content['text']='Changed after the first verification read.'
            setup.runtime.store.append(source)
        return result
    setup.monkeypatch.setattr(cpa,'_physical',raced)
    assert not verified(setup,record_id=report['record_id'])['accepted']


def test_postgres_cold_to_warm_state_and_watermark_do_not_change_configuration_certificate(setup):
    source, raw = configured_pg(setup)
    def recall(**kwargs):
        raw['candidate_source']['authority_revision'] = '3'
        raw['candidate_source']['postgres'].update(state='available', committed_watermark='new-watermark',
            index_revision='3', index_verified=True, query_valid=True, bypass_reason='')
        return setup.recall(**kwargs)
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', recall)
    result=run(setup)
    assert result['accepted'],result
    assert verified(setup, record_id=result['record_id'])['accepted']
    raw['candidate_source']['config_fingerprint'] = 'b' * 64
    assert not verified(setup, record_id=result['record_id'])['accepted']


def test_projection_wait_finishes_before_native_business_sampling_without_changing_recall_budget(setup):
    from eimemory.core.budgets import recall_budget_seconds
    from eimemory.ops import candidate_readiness_barrier as barrier
    from test_candidate_readiness_barrier import Clock, ready_measurement
    source, _ = configured_pg(setup)
    clock, probes = Clock(), []
    setup.monkeypatch.setattr(barrier, 'monotonic', clock.monotonic)
    setup.monkeypatch.setattr(barrier, 'sleep', clock.sleep)
    setup.monkeypatch.setenv('EIMEMORY_RECALL_BUDGET_SECONDS', '8')
    def probe(**kwargs):
        assert not setup.calls
        probes.append(kwargs)
        return ready_measurement() if len(probes) == 2 else ready_measurement(
            ready=False, error='authority_revision_changed', last_error='authority_revision_changed')
    setup.monkeypatch.setattr(source, 'readiness', probe)
    result = run(setup)
    assert result['accepted'] and len(setup.calls) == 12
    assert probes == [dict(force=True, timeout_seconds=.2)] * 2
    assert recall_budget_seconds() == 8
    assert result['candidate_readiness_barrier']['attempt_count'] == 2
    assert verified(setup, record_id=result['record_id'])['accepted']
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    assert packet.content['candidate_readiness_barrier'] == result['candidate_readiness_barrier']


def test_persistent_projection_lag_records_signed_infrastructure_failure_without_sampling_or_hiding_latest(setup):
    from eimemory.ops import candidate_readiness_barrier as barrier
    from test_candidate_readiness_barrier import Clock, ready_measurement
    source, _ = configured_pg(setup)
    prior = run(setup);assert prior['accepted']
    setup.calls.clear()
    clock = Clock()
    setup.monkeypatch.setattr(barrier, 'monotonic', clock.monotonic)
    setup.monkeypatch.setattr(barrier, 'sleep', clock.sleep)
    setup.monkeypatch.setattr(source, 'readiness', lambda **kwargs: ready_measurement(
        ready=False, error='authority_revision_changed', last_error='authority_revision_changed'))
    result = run(setup)
    assert not result['accepted'] and result['persisted'] and result['record_id']
    assert result['blocked_reason'] == 'controlled_acceptance_candidate_readiness_unverified'
    assert result['candidate_readiness_barrier']['elapsed_seconds'] == 300
    assert not setup.calls and all(a['sample_count'] == a['pass_count'] == 0 for a in result['agents'])
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    assert cpa.verify(packet.content, cpa.SCHEMA) and not packet.content['accepted']
    assert not verified(setup, record_id=prior['record_id'])['accepted']
    assert not verified(setup, record_id=result['record_id'])['accepted']


def test_index_not_ready_may_finish_building_before_engine_contract_and_sampling(setup):
    from dataclasses import replace
    from eimemory.ops import candidate_readiness_barrier as barrier
    from test_candidate_readiness_barrier import Clock, ready_measurement
    source, _ = configured_pg(setup)
    original = source.repository.read_index_state()
    state = [replace(original, ready=False)]
    setup.monkeypatch.setattr(source.repository, 'read_index_state', lambda: state[0])
    clock, probes = Clock(), []
    setup.monkeypatch.setattr(barrier, 'monotonic', clock.monotonic)
    setup.monkeypatch.setattr(barrier, 'sleep', clock.sleep)
    def probe(**kwargs):
        assert not setup.calls
        probes.append(kwargs)
        if len(probes) == 1:
            return ready_measurement(ready=False, error='index_not_ready', last_error='index_not_ready')
        state[0] = original
        return ready_measurement()
    setup.monkeypatch.setattr(source, 'readiness', probe)
    result = run(setup)
    assert result['accepted'] and len(probes) == 2 and len(setup.calls) == 12
    assert verified(setup, record_id=result['record_id'])['accepted']


def test_permanently_unreadable_index_contract_keeps_failed_signed_attempt_without_fabricating_identity(setup):
    from eimemory.ops import candidate_readiness_barrier as barrier
    from test_candidate_readiness_barrier import Clock, ready_measurement
    source, _ = configured_pg(setup)
    prior = run(setup);assert prior['accepted']
    setup.calls.clear()
    clock = Clock()
    setup.monkeypatch.setattr(barrier, 'monotonic', clock.monotonic)
    setup.monkeypatch.setattr(barrier, 'sleep', clock.sleep)
    setup.monkeypatch.setattr(source, 'readiness', lambda **kwargs: ready_measurement(
        ready=False, error='index_not_ready', last_error='index_not_ready'))
    def unavailable(): raise RuntimeError('private backend details')
    setup.monkeypatch.setattr(source.repository, 'read_index_state', unavailable)
    result = run(setup)
    assert not result['accepted'] and result['persisted'] and not setup.calls
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    assert cpa.verify(packet.content, cpa.SCHEMA) and packet.content['engine_identity'] == {}
    assert 'private backend' not in cpa.canonical(packet.content)
    assert not verified(setup, record_id=prior['record_id'])['accepted']


def test_ready_projection_does_not_waive_later_native_fallback_and_retains_real_bounded_diagnostics(setup):
    configured_pg(setup)
    def recall(**kwargs):
        bundle = setup.recall(**kwargs)
        bundle.explanation['engine_diagnostics'].update(fallback=True,
            fallback_error_codes={'authority_revision_changed': 1}, elapsed_ms=13.25,
            source_stages_ms={'index_read': .75, 'embedding_wait': 2.5},
            source_samples=[dict(search_index=0, elapsed_ms=3.25, failed_stage='index_recheck', cache_hit=False)])
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory, 'recall', recall)
    result = run(setup)
    assert not result['accepted'] and result['candidate_readiness_barrier']['ready']
    assert len(setup.calls) == 12 and not verified(setup)['accepted']
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    sample = packet.content['agents'][0]['samples'][0]
    assert sample['state']['fallback'] and not sample['complete']
    assert sample['diagnostics']['fallback_error_codes'] == {'authority_revision_changed': 1}
    assert sample['diagnostics']['source_stages_ms'] == {'index_read': .75, 'embedding_wait': 2.5}
    assert sample['diagnostics']['source_samples'][0]['failed_stage'] == 'index_recheck'


def test_unknown_configured_source_cannot_skip_the_formal_barrier(setup):
    engine = setup.runtime.memory.recall_engine
    identity = deepcopy(engine.effective_identity())
    setup.monkeypatch.setattr(engine, 'candidate_source', SimpleNamespace(configured=True))
    setup.monkeypatch.setattr(engine, 'effective_identity', lambda: deepcopy(identity))
    result = run(setup)
    assert not result['accepted'] and not setup.calls
    assert result['candidate_readiness_barrier']['applicable'] is True
    assert result['candidate_readiness_barrier']['blocked_reason'] == 'candidate_source_unsupported'


@pytest.mark.parametrize('damage', ['missing', 'inapplicable', 'metadata', 'late', 'boolean_attempts'])
def test_new_pg_signed_certificate_cannot_remove_or_downgrade_the_barrier(setup, damage):
    configured_pg(setup)
    current = {**RELEASE, 'release_version': '1.14.152'}
    setup.monkeypatch.setattr(cpa, '_release', lambda runtime, scope: (deepcopy(current),
        {'record_ref': 'receipt-fixture', 'record_digest': 'c' * 64}))
    result = run(setup);assert result['accepted']
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    if damage == 'missing': packet.content.pop('candidate_readiness_barrier')
    if damage == 'inapplicable': packet.content['candidate_readiness_barrier']['applicable'] = False
    if damage == 'metadata': packet.content['candidate_readiness_barrier']['final_measurement']['measurement_origin'] = 'metadata'
    if damage == 'late': packet.content['candidate_readiness_barrier']['elapsed_seconds'] = 301
    if damage == 'boolean_attempts': packet.content['candidate_readiness_barrier']['attempt_count'] = True
    packet.content = cpa.seal({k:v for k,v in packet.content.items() if k not in {'key_id','signature'}}, cpa.SCHEMA)
    packet.meta['report_digest'] = cpa.digest(packet.content)
    setup.runtime.store.append(packet)
    assert not verified(setup, record_id=result['record_id'])['accepted']


@pytest.mark.parametrize('version', ['1.14.147','1.14.148','1.14.149','1.14.150','1.14.151'])
def test_older_signed_pg_contract_remains_verifiable_without_rewriting_its_missing_barrier(setup, version):
    configured_pg(setup)
    current = {**RELEASE, 'release_version': version}
    setup.monkeypatch.setattr(cpa, '_release', lambda runtime, scope: (deepcopy(current),
        {'record_ref': 'receipt-fixture', 'record_digest': 'c' * 64}))
    result = run(setup);assert result['accepted']
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    packet.content.pop('candidate_readiness_barrier')
    packet.content = cpa.seal({k:v for k,v in packet.content.items() if k not in {'key_id','signature'}}, cpa.SCHEMA)
    packet.meta['report_digest'] = cpa.digest(packet.content)
    setup.runtime.store.append(packet)
    before = deepcopy(packet.content)
    assert verified(setup, record_id=result['record_id'])['accepted']
    assert setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default').content == before


@pytest.mark.parametrize('version', ['1.14.150', '1.14.151'])
@pytest.mark.parametrize('damage', ['empty', 'inapplicable', 'metadata', 'late', 'boolean_attempts'])
def test_older_version_cannot_downgrade_an_explicit_signed_pg_barrier(setup, version, damage):
    configured_pg(setup)
    current = {**RELEASE, 'release_version': version}
    setup.monkeypatch.setattr(cpa, '_release', lambda runtime, scope: (deepcopy(current),
        {'record_ref': 'receipt-fixture', 'record_digest': 'c' * 64}))
    result = run(setup);assert result['accepted']
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    if damage == 'empty': packet.content['candidate_readiness_barrier'] = {}
    if damage == 'inapplicable': packet.content['candidate_readiness_barrier']['applicable'] = False
    if damage == 'metadata': packet.content['candidate_readiness_barrier']['final_measurement']['measurement_origin'] = 'metadata'
    if damage == 'late': packet.content['candidate_readiness_barrier']['elapsed_seconds'] = 301
    if damage == 'boolean_attempts': packet.content['candidate_readiness_barrier']['attempt_count'] = True
    packet.content = cpa.seal({k:v for k,v in packet.content.items() if k not in {'key_id','signature'}}, cpa.SCHEMA)
    packet.meta['report_digest'] = cpa.digest(packet.content)
    setup.runtime.store.append(packet)
    assert not verified(setup, record_id=result['record_id'])['accepted']


def test_native_recall_diagnostics_omit_private_names_and_malformed_numbers():
    bundle = SimpleNamespace(explanation={'engine_diagnostics': dict(elapsed_ms=True,
        fallback_error_codes={'private secret':3, 'authority_revision_changed':2, 'postgres_timeout':True},
        source_stages_ms={'private secret':1, 'sqlite':float('nan'), 'index_read':-1, 'postgres_search':.75},
        source_samples=[dict(search_index=False, elapsed_ms=float('inf'),failed_stage='private secret',cache_hit='yes')],
        private_query='private secret')})
    value = cpa._recall_diagnostics(bundle)
    assert 'private secret' not in cpa.canonical(value) and 'elapsed_ms' not in value
    assert value['fallback_error_codes'] == {'unknown':3, 'authority_revision_changed':2}
    assert value['source_stages_ms'] == {'postgres_search':.75}
    assert value['source_samples'] == [{}]


@pytest.mark.parametrize('damage',['unavailable','degraded','collection','budget','fallback'])
def test_incomplete_recall_preserves_actual_references_without_claiming_compact_failure(setup,damage):
    def recall(**kwargs):
        bundle=setup.recall(**kwargs)
        selector=bundle.explanation['relevance_selector'];engine=bundle.explanation['engine_diagnostics']
        if damage in {'unavailable','degraded'}:
            selector.update(status=damage,collection_complete=False)
            bundle.explanation['retrieval_status']=damage
        if damage=='collection':selector['collection_complete']=False
        if damage=='budget':engine['drops']['recall_budget_exhausted']=1
        if damage=='fallback':engine.update(fallback=True,fallback_error_codes={'postgres_timeout':1})
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory,'recall',recall)
    setup.monkeypatch.setattr(RecallBundle,'to_compact_dict',lambda *_a,**_k:pytest.fail('incomplete recall projected'))
    result=run(setup)
    assert not result['accepted'] and not verified(setup)['accepted']
    packet=setup.runtime.store.get_by_exact_ref(result['record_id'],scope=OWNER,source_id='default')
    for agent in packet.content['agents']:
        for sample in agent['samples']:
            assert sample['failure_reason']=='controlled_acceptance_retrieval_incomplete'
            assert sample['bundle_observed'] and sample['returned_counts']==dict(items=1,rules=0,reflections=0)
            assert len(sample['returned'])==len(sample['returned_versions'])==1
            assert sample['returned'][0]['record_id']==sample['source_binding']['record_id']
            assert sample['compact_observation']==dict(attempted=False,structure_valid=None)
            assert sample['complete'] is False and sample['compact_refs']==[]
            if damage in {'unavailable','degraded'}:assert sample['state']['retrieval_status']==damage


@pytest.mark.parametrize('damage,reason',[
    ('size','compact_result_size_exceeded'),('non_object','compact_result_structure_invalid'),
    ('section','compact_result_structure_invalid'),('row','compact_result_structure_invalid'),
    ('encoding','compact_result_structure_invalid'),('status','compact_result_status_invalid'),
    ('boundary','compact_result_boundary_invalid'),('projection','compact_result_projection_failed')])
def test_compact_rejection_is_distinct_and_keeps_bundle_observation(setup,damage,reason):
    original=RecallBundle.to_compact_dict
    def compact(bundle,*args,**kwargs):
        if damage=='projection':raise RuntimeError('PRIVATE_TEXT')
        result=original(bundle,*args,**kwargs)
        if damage=='size':result['private_padding']='PRIVATE_TEXT'*2000
        if damage=='non_object':return []
        if damage=='section':result['items']='PRIVATE_TEXT'
        if damage=='row':result['items']=['PRIVATE_TEXT']
        if damage=='encoding':result['private_padding']=object()
        if damage=='status':result['retrieval_status']='unavailable'
        if damage=='boundary':result['items'][0]['record_id']='mem_foreign'
        return result
    setup.monkeypatch.setattr(RecallBundle,'to_compact_dict',compact)
    result=run(setup)
    assert not result['accepted'] and not verified(setup)['accepted']
    packet=setup.runtime.store.get_by_exact_ref(result['record_id'],scope=OWNER,source_id='default')
    sample=packet.content['agents'][0]['samples'][0]
    assert sample['failure_reason']=='controlled_acceptance_'+reason
    assert sample['bundle_observed'] and len(sample['returned'])==len(sample['returned_versions'])==1
    assert sample['state']['collection_complete'] is True and sample['complete'] is False
    assert sample['compact_observation']['attempted'] is True
    assert 'PRIVATE_TEXT' not in cpa.canonical(packet.content)
    if damage=='size':assert sample['compact_observation']['size_bytes']>16384
    if damage=='status':assert sample['compact_observation']['retrieval_status']=='unavailable'


def test_native_exception_before_bundle_is_explicitly_unobserved(setup):
    def recall(**kwargs):raise TimeoutError('PRIVATE_TEXT')
    setup.monkeypatch.setattr(setup.runtime.memory,'recall',recall)
    result=run(setup)
    packet=setup.runtime.store.get_by_exact_ref(result['record_id'],scope=OWNER,source_id='default')
    sample=packet.content['agents'][0]['samples'][0]
    assert not sample['bundle_observed'] and sample['returned_counts']=={} and sample['returned']==[]
    assert sample['failure_reason']=='controlled_acceptance_native_recall_failed'
    assert 'PRIVATE_TEXT' not in cpa.canonical(packet.content)


def two_owner_policy(setup):
    # The deployment caller uses its actual channel owner; nightly uses its
    # independently configured logical owner. Neither identity is rewritten.
    primary = SCOPES[0]
    nightly = ScopeRef(agent_id=primary.agent_id, workspace_id='embodied', user_id='darrow')
    setup.policy['owner_scope'] = asdict(primary)
    setup.policy['additional_owner_scopes'] = [asdict(nightly)]
    return primary, nightly


def test_one_protected_policy_creates_and_verifies_separate_native_receipts_for_both_owners(setup):
    primary, nightly = two_owner_policy(setup)
    policy_digest = cpa.digest(setup.policy)
    count_before = setup.runtime.store.count_records(kinds=['memory'])
    reports = {}
    for owner in (primary, nightly):
        result = cpa.run_controlled_production_acceptance(setup.runtime, scope=asdict(owner))
        assert result['accepted'] and result['policy_digest'] == policy_digest, result
        record = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=owner, source_id='default')
        assert record is not None and record.scope == owner and record.content['scope'] == asdict(owner)
        assert cpa.verify(record.content, cpa.SCHEMA), 'native signature must bind the actual caller body'
        assert record.content['policy_digest'] == policy_digest
        reports[owner.workspace_id] = result
    assert len(setup.calls) == 24
    assert setup.runtime.store.count_records(kinds=['memory']) == count_before
    assert setup.runtime.store.count_records(kinds=['evaluation_packet']) == 2
    for owner, foreign in ((primary, nightly), (nightly, primary)):
        own = reports[owner.workspace_id]
        result = cpa.verify_controlled_production_acceptance(setup.runtime,
            scope=asdict(owner), record_id=own['record_id'])
        assert result['accepted'] and result['record_id'] == own['record_id']
        assert not cpa.verify_controlled_production_acceptance(setup.runtime, scope=asdict(owner),
            record_id=reports[foreign.workspace_id]['record_id'])['accepted']
    assert len(setup.calls) == 24, 'native verification must not recall again or fan out owners'
    assert setup.policy['owner_scope'] == asdict(primary)
    assert setup.policy['additional_owner_scopes'] == [asdict(nightly)]


@pytest.mark.parametrize('defect', ['empty', 'too_many', 'not_list', 'not_object', 'partial',
    'extra_field', 'wildcard', 'question_wildcard', 'bracket_wildcard', 'whitespace', 'duplicate', 'duplicate_primary', 'foreign_tenant', 'foreign_agent'])
def test_additional_owner_allowlist_is_complete_bounded_and_unambiguous_before_recall(setup, defect):
    primary, nightly = two_owner_policy(setup)
    entries = setup.policy['additional_owner_scopes']
    if defect == 'empty': setup.policy['additional_owner_scopes'] = []
    if defect == 'too_many': setup.policy['additional_owner_scopes'] = [
        {**asdict(nightly), 'user_id': 'owner-' + str(i)} for i in range(5)]
    if defect == 'not_list': setup.policy['additional_owner_scopes'] = asdict(nightly)
    if defect == 'not_object': entries[0] = 'owner'
    if defect == 'partial': entries[0].pop('user_id')
    if defect == 'extra_field': entries[0]['channel'] = 'hermes'
    if defect == 'wildcard': entries[0]['workspace_id'] = '*'
    if defect == 'question_wildcard': entries[0]['workspace_id'] = 'embodie?'
    if defect == 'bracket_wildcard': entries[0]['workspace_id'] = 'embodied[12]'
    if defect == 'whitespace': entries[0]['user_id'] += ' '
    if defect == 'duplicate': entries.append(deepcopy(entries[0]))
    if defect == 'duplicate_primary': entries[0] = asdict(primary)
    if defect == 'foreign_tenant': entries[0]['tenant_id'] = 'other-tenant'
    if defect == 'foreign_agent': entries[0]['agent_id'] = 'hongtai'
    with pytest.raises(cpa.ControlledAcceptanceError):
        cpa.run_controlled_production_acceptance(setup.runtime, scope=asdict(primary))
    assert not setup.calls and setup.runtime.store.count_records(kinds=['evaluation_packet']) == 0


@pytest.mark.parametrize('change', ['tenant', 'agent', 'workspace', 'user', 'nested_channel', 'evaluation_agent', 'partial'])
def test_unlisted_owner_cannot_infer_authority_from_alias_or_evaluation_agent_scope(setup, change):
    primary, nightly = two_owner_policy(setup)
    caller = asdict(nightly)
    if change == 'tenant': caller['tenant_id'] = 'other-tenant'
    if change == 'agent': caller['agent_id'] = 'other-agent'
    if change == 'workspace': caller['workspace_id'] = 'other-workspace'
    if change == 'user': caller['user_id'] = 'another-user'
    if change == 'nested_channel': caller = {**asdict(primary), 'workspace_id': primary.workspace_id + '::channel::hermes'}
    if change == 'evaluation_agent': caller = asdict(SCOPES[1])
    if change == 'partial': caller.pop('user_id')
    with pytest.raises(cpa.ControlledAcceptanceError):
        cpa.run_controlled_production_acceptance(setup.runtime, scope=caller)
    assert not setup.calls


def test_legacy_policy_stays_byte_equivalent_and_does_not_authorize_extra_owner(setup):
    normalized = cpa._validate_policy(setup.policy, OWNER)
    assert cpa.canonical(normalized) == cpa.canonical(setup.policy)
    assert 'additional_owner_scopes' not in normalized
    channel = SCOPES[0]
    with pytest.raises(cpa.ControlledAcceptanceError):
        cpa.load_controlled_policy(setup.runtime, channel)
    assert not setup.calls


def test_four_distinct_additional_owners_are_allowed_but_primary_is_retained(setup):
    primary, nightly = two_owner_policy(setup)
    setup.policy['additional_owner_scopes'] = [{**asdict(nightly), 'user_id': 'owner-' + str(i)} for i in range(4)]
    for caller in [asdict(primary), *setup.policy['additional_owner_scopes']]:
        assert cpa._validate_policy(setup.policy, ScopeRef.from_dict(caller)) == setup.policy
    assert setup.policy['owner_scope'] == asdict(primary) and not setup.calls


def test_removing_additional_owner_revokes_its_receipt_without_reassigning_or_rewriting_it(setup):
    primary, nightly = two_owner_policy(setup)
    result = cpa.run_controlled_production_acceptance(setup.runtime, scope=asdict(nightly))
    assert result['accepted']
    prior = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=nightly, source_id='default').to_dict()
    setup.policy.pop('additional_owner_scopes')
    assert not cpa.verify_controlled_production_acceptance(setup.runtime,
        scope=asdict(nightly), record_id=result['record_id'])['accepted']
    with pytest.raises(cpa.ControlledAcceptanceError):
        cpa.run_controlled_production_acceptance(setup.runtime, scope=asdict(nightly))
    assert setup.runtime.store.get_by_exact_ref(result['record_id'], scope=nightly, source_id='default').to_dict() == prior
    assert cpa.load_controlled_policy(setup.runtime, primary)['owner_scope'] == asdict(primary)


@pytest.mark.skipif(os.name != 'posix' or not hasattr(os, 'geteuid') or os.geteuid() != 0,
                    reason='native root-owned policy requires Linux root')
@pytest.mark.parametrize('damage', ['valid', 'writable', 'symlink', 'foreign_owner', 'parent_writable', 'parent_symlink'])
def test_native_operator_policy_file_and_all_ancestors_are_protected(tmp_path, damage):
    parent=tmp_path/'authority';parent.mkdir();path=parent/'policy.json'
    path.write_text('{"probe":true}');path.chmod(0o640)
    if damage == 'writable':path.chmod(0o660)
    if damage == 'foreign_owner':os.chown(path,65534,65534)
    if damage == 'parent_writable':parent.chmod(0o777)
    if damage == 'symlink':
        target=parent/'target.json';path.rename(target);path.symlink_to(target)
    if damage == 'parent_symlink':
        target=tmp_path/'target';parent.rename(target);parent.symlink_to(target,target_is_directory=True)
    if damage == 'valid':assert cpa._read_policy(path)=={'probe':True}
    else:
        with pytest.raises(cpa.ControlledAcceptanceError):cpa._read_policy(path)


@pytest.fixture
def historical_business_setup(setup):
    from hashlib import sha256
    policy = setup.policy
    policy['purpose'] = 'historical_business_qualification'
    policy['business_sources'] = []
    for records in setup.memories.values():
        for index, record in enumerate(records):
            record.source = 'hermes.memory'
            record.meta = {
                'memory_type': 'fact', 'authoritative': True, 'runtime_channel': 'hermes',
                'source_event_id': record.record_id + ':original-import',
                'quality': {'capture_decision': 'accept'},
                'native_import': {'schema': 'hermes_native_memory_import.v1',
                    'file': '/original-business/MEMORY.md', 'line': index + 1,
                    'file_digest': 'd' * 64,
                    'text_digest': sha256(record.content['text'].strip().encode()).hexdigest()},
            }
            setup.runtime.store.append(record)
            binding = cpa.benchmark._binding(record)
            version = cpa._physical(setup.runtime, binding, record.scope, ('hermes',), policy['stable_before'])
            policy['business_sources'].append(dict(source_binding=binding, source_version=version,
                                                  origin_source=record.source))
    return setup


def test_verified_historical_business_samples_are_qualified_without_inventing_online_traffic(historical_business_setup):
    setup = historical_business_setup
    before = setup.runtime.store.count_records(kinds=['memory'])
    result = run(setup)
    assert result['accepted'], result
    business = result['business_qualification']
    assert result['acceptance_purpose'] == 'historical_business_qualification'
    assert business['accepted'] and business['business_origin'] == 'historical_business'
    assert business['validation_method'] == 'controlled_recall'
    assert business['sample_count'] == business['pass_count'] == 12 and business['fail_count'] == 0
    assert business['agents'] == result['agents']
    assert business['record_id'] == result['record_id'] and business['policy_digest'] == result['policy_digest']
    assert business['release_identity'] == RELEASE
    assert result['natural_samples_created'] == 0 and not result['certifies_natural_queries']
    assert not result['host_delivery_certified'] and not result['memory_contribution_certified']
    assert setup.runtime.store.count_records(kinds=['memory']) == before
    checked = verified(setup, record_id=result['record_id'])
    assert checked['business_qualification'] == business and checked['accepted'], checked
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    assert all(sample['business_provenance']['schema'] == 'historical_business_provenance.v1'
               for agent in packet.content['agents'] for sample in agent['samples'])
    assert 'green notebook' not in cpa.canonical(result)


def test_historical_business_dry_run_is_not_an_accepted_business_certificate(historical_business_setup):
    result = run(historical_business_setup, persist=False)
    assert result['passed_threshold'] and not result['accepted']
    assert not result['business_qualification']['accepted'] and not result['business_qualification']['record_id']
    assert not verified(historical_business_setup)['accepted']


def test_ordinary_operational_receipt_does_not_acquire_historical_business_purpose(setup):
    result = run(setup)
    assert result['accepted'] and result['acceptance_purpose'] == 'operational'
    assert result['business_qualification'] == {}
    assert verified(setup)['business_qualification'] == {}


@pytest.mark.parametrize('field', ['purpose', 'scope', 'version', 'missing_source', 'duplicate_source'])
def test_historical_business_authority_is_explicit_and_exact_before_any_recall(historical_business_setup, field):
    setup = historical_business_setup
    if field == 'purpose': setup.policy['purpose'] = 'arbitrary_business_claim'
    elif field == 'scope': setup.policy['business_sources'][0]['source_binding']['scope']['user_id'] = 'other-user'
    elif field == 'version': setup.policy['business_sources'][0]['source_version'] = ['0' * 64]
    elif field == 'missing_source': setup.policy['business_sources'].pop()
    else: setup.policy['business_sources'][1] = deepcopy(setup.policy['business_sources'][0])
    with pytest.raises(cpa.ControlledAcceptanceError):
        run(setup)
    assert not setup.calls


@pytest.mark.parametrize('field', ['count', 'agent', 'policy', 'record', 'purpose', 'provenance'])
def test_signed_historical_business_fields_are_recomputed_not_accepted_as_labels(historical_business_setup, field):
    setup = historical_business_setup
    result = run(setup); assert result['accepted'], result
    packet = setup.runtime.store.get_by_exact_ref(result['record_id'], scope=OWNER, source_id='default')
    if field == 'count': packet.content['business_qualification']['sample_count'] += 1
    elif field == 'agent': packet.content['business_qualification']['agents'][0]['pass_count'] -= 1
    elif field == 'policy': packet.content['business_qualification']['policy_digest'] = '0' * 64
    elif field == 'record': packet.content['business_qualification']['record_id'] = 'cpa_other'
    elif field == 'purpose': packet.content['acceptance_purpose'] = 'operational'
    else: packet.content['agents'][0]['samples'][0]['business_provenance']['source_metadata_digest'] = '0' * 64
    packet.content = cpa.seal({k:v for k,v in packet.content.items() if k not in {'key_id', 'signature'}}, cpa.SCHEMA)
    packet.meta['report_digest'] = cpa.digest(packet.content)
    setup.runtime.store.append(packet)
    assert not verified(setup, record_id=result['record_id'])['accepted']


def test_new_business_purpose_invalidates_prior_operational_receipt(historical_business_setup):
    setup = historical_business_setup
    sources = setup.policy.pop('business_sources'); setup.policy.pop('purpose')
    old = run(setup); assert old['accepted'] and old['business_qualification'] == {}
    setup.policy.update(purpose='historical_business_qualification', business_sources=sources)
    assert not verified(setup, record_id=old['record_id'])['accepted']
    current = run(setup)
    assert current['accepted'] and current['business_qualification']['accepted']


def test_historical_authorized_source_still_rejects_acceptance_generated_memory(historical_business_setup):
    setup = historical_business_setup
    record = setup.memories['hongtai'][0]
    record.meta['acceptance_generated'] = True
    setup.runtime.store.append(record)
    binding = cpa.benchmark._binding(record)
    setup.policy['business_sources'][3] = dict(source_binding=binding,
        source_version=cpa._physical(setup.runtime,binding,record.scope,('hermes',),setup.policy['stable_before']),
        origin_source=record.source)
    result = run(setup)
    assert not result['accepted'] and not result['business_qualification']['accepted']


def ingest_after_grant(setup,*,count=80):
    for index in range(count):
        r=RecordEnvelope.create(kind='memory',title=f'New raw host turn {index}',
            content={'text':f'User: New ordinary discussion {index}.\nAssistant: New ordinary response.',
                     'memory_type':'conversation'},source='hermes.memory',source_id='hermes',scope=SCOPES[0],
            meta={'memory_type':'conversation','capture_origin':'turn_sync'})
        setup.runtime.store.append(r)


def test_historical_grants_remain_measured_after_new_host_history_fills_the_scan_window(historical_business_setup):
    setup=historical_business_setup;policy_before=deepcopy(setup.policy)
    ingest_after_grant(setup)
    result=run(setup)
    assert result['accepted'] and result['business_qualification']['pass_count']==12,result
    assert setup.policy==policy_before and len(setup.calls)==12
    assert verified(setup,record_id=result['record_id'])['accepted']
    assert len(setup.calls)==12


def test_historical_grants_use_actual_native_recall_after_new_host_history(historical_business_setup):
    setup=historical_business_setup;ingest_after_grant(setup)
    memory=setup.runtime.memory;actual=type(memory).recall.__get__(memory,type(memory));calls=[]
    def recall(**kwargs):
        calls.append(kwargs)
        return actual(**kwargs)
    setup.monkeypatch.setattr(memory,'recall',recall)
    result=run(setup)
    assert result['accepted'] and result['business_qualification']['pass_count']==12,result
    assert len(calls)==12 and verified(setup,record_id=result['record_id'])['accepted']
    assert len(calls)==12


@pytest.mark.parametrize('damage',['missing','archived','payload','sql_version','later_than_cutoff'])
def test_historical_grants_never_replace_a_missing_or_changed_authorized_source(historical_business_setup,damage):
    setup=historical_business_setup;previous=run(setup);assert previous['accepted']
    target=setup.memories['hongrui'][0]
    if damage=='missing':
        setup.runtime.store.run_locked(lambda sql:(sql.execute('DELETE FROM records WHERE record_id=?',(target.record_id,)),sql.commit()))
    elif damage=='sql_version':
        setup.runtime.store.run_locked(lambda sql:(sql.execute('UPDATE records SET updated_at=? WHERE record_id=?',('2099-01-01T00:00:00Z',target.record_id)),sql.commit()))
    else:
        if damage=='archived':target.status='archived'
        if damage=='payload':target.content['text']+=' Corrected later.'
        if damage=='later_than_cutoff':target.time.created_at='2099-01-01T00:00:00Z'
        setup.runtime.store.append(target)
    ingest_after_grant(setup)
    result=run(setup)
    assert not result['accepted'] and result['persisted']
    assert not next(a for a in result['agents'] if a['agent_id']=='hongrui')['accepted']
    assert not verified(setup,record_id=previous['record_id'])['accepted']
    assert result['record_id']!=previous['record_id']


def test_historical_grant_does_not_select_an_unlisted_old_fact(historical_business_setup):
    setup=historical_business_setup;before=deepcopy(setup.policy)
    for index in range(80):
        record=RecordEnvelope.create(kind='memory',title=f'Another legitimate old fact {index}',
            content={'text':'An unrelated existing business fact.','memory_type':'fact'},
            source='hermes.memory',source_id='hermes',scope=SCOPES[0],meta={'memory_type':'fact'})
        record.time.created_at='2020-01-01T00:00:00Z';setup.runtime.store.append(record)
    result=run(setup)
    assert result['accepted'] and len(setup.calls)==12 and setup.policy==before
    expected={entry['source_binding']['record_id'] for entry in before['business_sources']}
    packet=setup.runtime.store.get_by_exact_ref(result['record_id'],scope=OWNER,source_id='default')
    assert {sample['source_binding']['record_id'] for agent in packet.content['agents'] for sample in agent['samples']}==expected


from eimemory.retrieval.postgres_vector import PostgresVectorConfig, PostgresVectorCandidateSource


class AbsentIndex:
    def __init__(self):self.reads=0
    def read_index_state(self):
        self.reads+=1
        raise RuntimeError('isolated index does not exist')


def native_pg(setup,config):
    engine=setup.runtime.memory.recall_engine
    repository=AbsentIndex()
    source=PostgresVectorCandidateSource(sqlite_source=engine.candidate_source,
        config=config,repository=repository)
    setup.monkeypatch.setattr(engine,'candidate_source',source)
    return source,repository


@pytest.mark.parametrize('config',[PostgresVectorConfig(enabled=False),PostgresVectorConfig(enabled=True)],ids=['disabled','not_configured'])
def test_explicit_native_bypass_no_index_can_issue_and_revalidate_signed_cpa(setup,config):
    source,repository=native_pg(setup,config)
    contract=cpa.controlled_retrieval_contract(setup.runtime)
    setup.policy['engine_contract_digest']=cpa.digest(contract)
    report=run(setup)
    assert report['accepted'],report
    assert len(setup.calls)==12
    assert report['candidate_readiness_barrier']['status']=='not_applicable'
    packet=setup.runtime.store.get_by_exact_ref(report['record_id'],scope=OWNER,source_id='default')
    assert cpa.verify(packet.content,cpa.SCHEMA)
    assert verified(setup,record_id=report['record_id'])['accepted']
    assert len(setup.calls)==12,'verification must not rerun any case'
    assert repository.reads==0


@pytest.mark.parametrize('change',['enabled','semantics'])
def test_signed_explicit_bypass_does_not_survive_changed_native_configuration(setup,change):
    source,repository=native_pg(setup,PostgresVectorConfig(enabled=False))
    setup.policy['engine_contract_digest']=cpa.digest(cpa.controlled_retrieval_contract(setup.runtime))
    report=run(setup);assert report['accepted']
    source.config=replace(source.config,**({'enabled':True} if change=='enabled' else {'top_k_max':11}))
    assert not verified(setup,record_id=report['record_id'])['accepted']
    assert repository.reads==0


@pytest.mark.parametrize('damage',['barrier','identity','index_mode'])
def test_signed_bypass_cannot_relabel_a_forged_projection_proof(setup,damage):
    _,repository=native_pg(setup,PostgresVectorConfig(enabled=False))
    setup.policy['engine_contract_digest']=cpa.digest(cpa.controlled_retrieval_contract(setup.runtime))
    report=run(setup);assert report['accepted']
    packet=setup.runtime.store.get_by_exact_ref(report['record_id'],scope=OWNER,source_id='default')
    if damage=='barrier':packet.content['candidate_readiness_barrier']['source_configuration']['enabled']=True
    if damage=='identity':packet.content['engine_identity']['candidate_source']['enabled']=True
    if damage=='index_mode':packet.content['engine_identity']['candidate_source']['candidate_index_requirement']['reason']='not_configured'
    packet.content=cpa.seal({k:v for k,v in packet.content.items() if k not in {'signature','key_id'}},cpa.SCHEMA)
    packet.meta['report_digest']=cpa.digest(packet.content);setup.runtime.store.append(packet)
    assert not verified(setup,record_id=report['record_id'])['accepted']
    assert repository.reads==0


def test_enabled_pg_contract_and_older_signed_certificate_keep_the_ready_index_binding(setup):
    source,raw=configured_pg(setup)
    contract=cpa.controlled_retrieval_contract(setup.runtime)
    assert 'candidate_index_requirement' not in contract['candidate_source']
    assert contract['candidate_source']['durable_index_contract']['ready'] is True
    report=run(setup);assert report['accepted']
    assert verified(setup,record_id=report['record_id'])['accepted']


def test_enabled_configured_native_pg_still_requires_actual_ready_index(setup):
    _,repository=native_pg(setup,PostgresVectorConfig(enabled=True,dsn='postgresql://localhost/isolated'))
    with pytest.raises(cpa.ControlledAcceptanceError,match='controlled_acceptance_index_contract_unavailable'):
        cpa.controlled_retrieval_contract(setup.runtime)
    assert repository.reads==1


@pytest.mark.parametrize('config',[PostgresVectorConfig(enabled=False),PostgresVectorConfig(enabled=True)],ids=['disabled','not_configured'])
def test_explicit_bypass_signs_actual_native_sqlite_recall_without_backend_or_index(setup,config):
    _,repository=native_pg(setup,config)
    memory=setup.runtime.memory
    actual_recall=type(memory).recall.__get__(memory,type(memory))
    calls=[]
    def recall(**kwargs):
        calls.append(kwargs)
        return actual_recall(**kwargs)
    setup.monkeypatch.setattr(memory,'recall',recall)
    setup.policy['engine_contract_digest']=cpa.digest(cpa.controlled_retrieval_contract(setup.runtime))
    report=run(setup)
    assert report['accepted'],report
    assert len(calls)==12
    packet=setup.runtime.store.get_by_exact_ref(report['record_id'],scope=OWNER,source_id='default')
    assert cpa.verify(packet.content,cpa.SCHEMA)
    assert verified(setup,record_id=report['record_id'])['accepted']
    assert len(calls)==12 and repository.reads==0
