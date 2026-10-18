"""The actual native cascade is support, with exact parent authority retained."""
from copy import deepcopy
from dataclasses import asdict

import pytest

from test_controlled_production_acceptance import setup, run, verified, SCOPES, OWNER
from eimemory.evaluation import controlled_production_acceptance as cpa
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


def with_native_cascade(setup):
    parents = {}
    for scope in SCOPES:
        parent = RecordEnvelope.create(kind='memory', title='Hermes completed turn',
            content={'text':'User: Original factual question.\nAssistant: Original factual response.',
                     'memory_type':'conversation'}, source='hermes.memory',source_id='hermes',scope=scope,
            meta={'memory_type':'conversation','capture_origin':'turn_sync'})
        parent.time.created_at='2020-01-01T00:00:00Z';parent.time.updated_at='2020-01-01T00:00:00Z'
        setup.runtime.store.append(parent);parents[scope.agent_id]=parent
        for gold in setup.memories[scope.agent_id]:
            gold.links.append(LinkRef(target_id=parent.record_id,target_kind='memory',relation='derived_from'))
            setup.runtime.store.append(gold)
    def recall(**kwargs):
        bundle=setup.recall(**kwargs)
        parent=parents[kwargs['scope']['agent_id']]
        bundle.explanation['cascade_evidence']=setup.runtime.memory._selected_record_summaries([parent])
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory,'recall',recall)
    return parents,recall


def test_native_same_scope_cascade_is_bound_and_verified_without_promoting_it_to_fact(setup):
    parents,_=with_native_cascade(setup)
    result=run(setup)
    assert result['accepted'],result
    assert all(a['sample_count']==a['pass_count']==3 for a in result['agents'])
    checked=verified(setup,record_id=result['record_id'])
    assert checked['accepted'] and not checked['certifies_natural_queries']
    packet=setup.runtime.store.get_by_exact_ref(result['record_id'],scope=OWNER,source_id='default')
    assert all(s['cascade_evidence'] and s['compact_digest'] for a in packet.content['agents'] for s in a['samples'])
    assert all(parent.content['memory_type']=='conversation' for parent in parents.values())
    assert 'Original factual question' not in cpa.canonical(packet.content)


def test_legacy_null_quality_does_not_invalidate_an_otherwise_authoritative_episode_parent(setup):
    parents,_=with_native_cascade(setup)
    parent=parents['hongtai'];parent.meta['quality']=None;setup.runtime.store.append(parent)
    result=run(setup)
    assert result['accepted'],result
    assert verified(setup,record_id=result['record_id'])['accepted']


@pytest.mark.parametrize('damage',['missing','unlinked','foreign_user','foreign_source','malformed','summary','stale','duplicate'])
def test_native_cascade_failure_retains_the_actual_case_count_and_safe_reason(setup,damage):
    parents,original=with_native_cascade(setup)
    if damage in {'missing','unlinked','foreign_user','foreign_source','stale'}:
        parent=parents['hongtai']
        if damage=='missing':
            setup.runtime.store.run_locked(lambda sql:(sql.execute('DELETE FROM records WHERE record_id=?',(parent.record_id,)),sql.commit()))
        if damage=='unlinked':
            for gold in setup.memories['hongtai']:
                gold.links=[];setup.runtime.store.append(gold)
        if damage=='foreign_user':
            parent.scope=ScopeRef.from_dict({**asdict(parent.scope),'user_id':'other-user'})
            setup.runtime.store.append(parent)
            setup.runtime.store.run_locked(lambda sql:(sql.execute('DELETE FROM records WHERE record_id=? AND user_id=?',
                (parent.record_id,SCOPES[1].user_id)),sql.commit()))
        if damage=='foreign_source':
            setup.runtime.store.run_locked(lambda sql:(sql.execute('UPDATE records SET source_id=? WHERE record_id=?',
                ('other-source',parent.record_id)),sql.commit()))
        if damage=='stale':parent.status='superseded';setup.runtime.store.append(parent)
    def recall(**kwargs):
        bundle=original(**kwargs)
        if kwargs['scope']['agent_id']=='hongtai':
            if damage=='malformed':bundle.explanation['cascade_evidence']=[{'record_id':None}]
            if damage=='summary':bundle.explanation['cascade_evidence'][0]['title']='Forged parent summary'
            if damage=='duplicate':bundle.explanation['cascade_evidence']*=2
        return bundle
    setup.monkeypatch.setattr(setup.runtime.memory,'recall',recall)
    result=run(setup)
    failed=next(a for a in result['agents'] if a['agent_id']=='hongtai')
    assert not result['accepted']
    assert failed['sample_count']==3 and failed['pass_count']==0
    assert failed['blocked_reason'].startswith('controlled_acceptance_')
    assert failed['blocked_reason']!='controlled_acceptance_native_execution_incomplete'
    assert not verified(setup,record_id=result['record_id'])['accepted']


@pytest.mark.parametrize('change',['parent_content','parent_version','derivation_edge','signed_summary'])
def test_cascade_certificate_rechecks_original_parent_and_link_authority(setup,change):
    parents,_=with_native_cascade(setup)
    result=run(setup);assert result['accepted']
    parent=parents['hongtai']
    if change=='parent_content':parent.content['text']+=' Changed after acceptance.';setup.runtime.store.append(parent)
    if change=='parent_version':
        setup.runtime.store.run_locked(lambda sql:(sql.execute('UPDATE records SET updated_at=? WHERE record_id=?',
            ('2021-01-01T00:00:00Z',parent.record_id)),sql.commit()))
    if change=='derivation_edge':
        gold=setup.memories['hongtai'][0];gold.links=[];setup.runtime.store.append(gold)
    if change=='signed_summary':
        packet=setup.runtime.store.get_by_exact_ref(result['record_id'],scope=OWNER,source_id='default')
        packet.content['agents'][1]['samples'][0]['cascade_evidence'][0]['summary_digest']='0'*64
        packet.content=cpa.seal({k:v for k,v in packet.content.items() if k not in {'signature','key_id'}},cpa.SCHEMA)
        packet.meta['report_digest']=cpa.digest(packet.content);setup.runtime.store.append(packet)
    assert not verified(setup,record_id=result['record_id'])['accepted']


def test_native_model_or_compact_failure_is_one_failed_case_not_zero_observations(setup):
    original=setup.recall
    def recall(**kwargs):
        if kwargs['scope']['agent_id']=='hongtai' and kwargs['query'].endswith('1'):
            raise TimeoutError('private provider details must never appear')
        return original(**kwargs)
    setup.monkeypatch.setattr(setup.runtime.memory,'recall',recall)
    result=run(setup)
    agent=next(a for a in result['agents'] if a['agent_id']=='hongtai')
    assert agent['sample_count']==3 and agent['pass_count']==2 and agent['fail_count']==1
    assert agent['blocked_reason']=='controlled_acceptance_native_recall_failed'
    assert 'private provider' not in cpa.canonical(result)


@pytest.mark.parametrize('damage',['oversize','foreign_ref','target_only_in_aux'])
def test_final_compact_boundary_and_budget_are_checked_after_native_retrieval(setup,damage):
    from eimemory.models.records import RecallBundle
    original=RecallBundle.to_compact_dict
    def compact(self,**kwargs):
        payload=original(self,**kwargs)
        if damage=='oversize':payload['next_action_hint']='x'*17000
        if damage=='foreign_ref':payload['items'].append({'record_id':'mem_foreign','source_id':'hermes'})
        if damage=='target_only_in_aux':payload['rules']=payload['items'];payload['items']=[]
        return payload
    setup.monkeypatch.setattr(RecallBundle,'to_compact_dict',compact)
    result=run(setup)
    assert not result['accepted'] and all(a['sample_count']==3 and a['pass_count']==0 for a in result['agents'])
