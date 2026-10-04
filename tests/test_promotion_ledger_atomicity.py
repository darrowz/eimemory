"""Synthetic local-only promotion ledger/link transaction regressions."""
from __future__ import annotations
import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from release_report_fixtures import promotion_health_receipt
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.governance.promotion import promotion_manager as pm

SCOPE=ScopeRef(tenant_id='audit',agent_id='synthetic',workspace_id='local',user_id='user')

@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def reject(*args,**kwargs):
        raise AssertionError('External effects are outside this test contract')
    monkeypatch.setattr(subprocess,'Popen',reject)
    monkeypatch.setattr(socket,'create_connection',reject)
    monkeypatch.setattr(socket.socket,'connect',reject)
    monkeypatch.setattr(socket.socket,'bind',reject)

@pytest.fixture
def runtime(tmp_path):
    value=Runtime.create(root=tmp_path)
    try: yield value
    finally: value.close()

def request(runtime, *, scope=SCOPE, source_id='default', record_id=None, action='applied'):
    rec=RecordEnvelope.create(kind='promotion_request',title='Synthetic request',scope=scope,source_id=source_id,status='promoted',content={'action':action,'promotion_target':'tool_route','gate':{'ok':True},'side_effect':{'ok':True,'applied_artifact_ids':['synthetic-artifact']}},meta={'action':action})
    if record_id: rec.record_id=record_id
    return runtime.store.append(rec)

def exact(runtime,rec):
    return runtime.store.get_by_exact_ref(rec.record_id,scope=rec.scope,source_id=rec.source_id)

def state(runtime,rec):
    with runtime.store.locked() as sql:
        return {'ledger_count':sql.execute('SELECT COUNT(*) FROM policy_rollout_ledger').fetchone()[0], 'outbox_count':sql.execute('SELECT COUNT(*) FROM export_outbox').fetchone()[0], 'record':sql.get_by_exact_ref(rec.record_id,scope=rec.scope,source_id=rec.source_id).to_dict(), 'transaction':sql.in_transaction}

def ensure(runtime,rec):
    return pm._ensure_promotion_rollout_ledger(runtime,promotion_record=rec,scope=rec.scope)

def legacy_ledger(runtime,rec):
    with runtime.store.locked() as sql:
        result=sql._record_policy_rollout_ledger(action_type=pm.CAPABILITY_ROLLOUT_ACTION,scope=rec.scope,promotion_id=rec.record_id,source_opportunity_id=rec.record_id,source_opportunity={},trust_report={},replay_report={},is_auto=True,applied_pattern_id='synthetic-artifact',budget_decision='ok',details={})
        sql.commit()
        return result

def test_direct_ensure_commits_ledger_request_and_both_outboxes(runtime):
    rec=request(runtime); before=state(runtime,rec)
    result=ensure(runtime,rec); after=state(runtime,rec)
    assert result['created'] is True
    assert after['ledger_count']==before['ledger_count']+1
    assert after['outbox_count']==before['outbox_count']+2
    assert after['record']['content']['rollout_ledger_id']==result['id']
    assert after['record']['meta']['rollout_ledger_id']==result['id']
    assert not after['transaction']
    assert rec.content['rollout_ledger_id']==result['id']

@pytest.mark.parametrize('failure',['after_ledger_insert','after_record_write','after_record_outbox','before_commit'])
def test_failure_rolls_back_ledger_link_and_outboxes_and_retries(runtime,monkeypatch,tmp_path,failure):
    rec=request(runtime); before=state(runtime,rec); original_object=rec.to_dict()
    with monkeypatch.context() as patch:
        if failure=='after_ledger_insert':
            target=runtime.store.sqlite; name='_record_policy_rollout_ledger'
        elif failure=='after_record_write':
            target=runtime.store.sqlite; name='rewrite'
        elif failure=='after_record_outbox':
            target=runtime.store; name='_enqueue_record_exports'
        else:
            target=runtime.store.sqlite; name='commit'
        original=getattr(target,name)
        def fail(*args,**kwargs):
            if failure!='before_commit': original(*args,**kwargs)
            raise RuntimeError('synthetic_failure:'+failure)
        patch.setattr(target,name,fail)
        with pytest.raises(RuntimeError,match='synthetic_failure'):
            ensure(runtime,rec)
    assert state(runtime,rec)==before
    assert rec.to_dict()==original_object
    runtime.close(); reopened=Runtime.create(root=tmp_path)
    try:
        assert state(reopened,rec)==before
        result=ensure(reopened,rec)
        assert result['created'] is True
        assert state(reopened,rec)['ledger_count']==1
    finally: reopened.close()

def test_foreign_transaction_is_not_committed_or_rolled_back(runtime):
    rec=request(runtime)
    with runtime.store.locked() as sql:
        sql.execute('CREATE TABLE synthetic_sentinel(value TEXT)'); sql.commit()
        sql.execute('BEGIN'); sql.execute("INSERT INTO synthetic_sentinel VALUES ('caller')")
        changes=sql.conn.total_changes
        with pytest.raises(RuntimeError,match='promotion_rollout_ledger_requires_own_transaction'):
            ensure(runtime,rec)
        assert sql.in_transaction and sql.conn.total_changes==changes
        assert sql.execute('SELECT value FROM synthetic_sentinel').fetchone()[0]=='caller'
        sql.rollback()
        assert sql.execute('SELECT COUNT(*) FROM synthetic_sentinel').fetchone()[0]==0

def test_repeat_ensure_is_single_ledger_and_no_extra_outboxes(runtime):
    rec=request(runtime); first=ensure(runtime,rec); before=state(runtime,rec)
    second=ensure(runtime,rec)
    assert second=={'id':first['id'],'created':False}
    assert state(runtime,rec)==before

def test_concurrent_same_request_has_one_ledger(runtime):
    rec=request(runtime)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(lambda _:ensure(runtime,RecordEnvelope.from_dict(rec.to_dict())),range(2)))
    assert len({x['id'] for x in results})==1
    assert sorted(x['created'] for x in results)==[False,True]
    assert state(runtime,rec)['ledger_count']==1

def test_stale_request_reload_preserves_current_fields(runtime):
    rec=request(runtime); latest=exact(runtime,rec)
    latest.content['concurrent_field']='preserve'; latest.meta['concurrent_field']='preserve'
    runtime.store.rewrite(latest)
    ensure(runtime,rec)
    assert exact(runtime,rec).content['concurrent_field']=='preserve'
    assert exact(runtime,rec).meta['concurrent_field']=='preserve'

def test_missing_exact_request_fails_closed(runtime):
    rec=RecordEnvelope.create(kind='promotion_request',title='Absent',scope=SCOPE,content={'action':'applied'})
    with pytest.raises(ValueError,match='promotion_request_identity'):
        ensure(runtime,rec)
    with runtime.store.locked() as sql:
        assert sql.execute('SELECT COUNT(*) FROM policy_rollout_ledger').fetchone()[0]==0

def test_scope_mismatch_fails_without_touching_other_partition(runtime):
    rec=request(runtime); before=state(runtime,rec)
    other=ScopeRef(tenant_id='other',agent_id='synthetic')
    with pytest.raises(ValueError,match='promotion_request_scope'):
        pm._ensure_promotion_rollout_ledger(runtime,promotion_record=rec,scope=other)
    assert state(runtime,rec)==before

def test_other_source_same_id_cannot_fallback_or_replace_existing_request(runtime):
    left=request(runtime,source_id='left'); before=state(runtime,left)
    with pytest.raises(ValueError,match='source_id move'):
        request(runtime,source_id='right',record_id=left.record_id)
    assert state(runtime,left)==before
    wrong=RecordEnvelope.from_dict(left.to_dict()); wrong.source_id='right'
    with pytest.raises(ValueError,match='promotion_request_identity'):
        ensure(runtime,wrong)
    assert state(runtime,left)==before
    result=ensure(runtime,left)
    assert result['details']['promotion_request_source_id']=='left'
    assert exact(runtime,left).content['rollout_ledger_id']==result['id']

@pytest.mark.parametrize('fault', ['scope','source','record_id','kind'])
def test_incorrect_exact_hydration_fails_closed(runtime,monkeypatch,fault):
    rec=request(runtime); before=state(runtime,rec)
    wrong=RecordEnvelope.from_dict(rec.to_dict())
    if fault=='scope': wrong.scope=ScopeRef(tenant_id='other')
    elif fault=='source': wrong.source_id='other'
    elif fault=='record_id': wrong.record_id='different-record'
    else: wrong.kind='memory'
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store.sqlite,'get_by_exact_ref',lambda *args,**kwargs:wrong)
        with pytest.raises(ValueError,match='promotion_request_identity'):
            ensure(runtime,rec)
    assert state(runtime,rec)==before


def test_same_id_in_other_scope_keeps_distinct_ledger_and_link(runtime):
    left=request(runtime); right=request(runtime,scope=ScopeRef(tenant_id='other'),record_id=left.record_id)
    a=ensure(runtime,left); b=ensure(runtime,right)
    assert a['id']!=b['id']
    assert exact(runtime,left).content['rollout_ledger_id']==a['id']
    assert exact(runtime,right).content['rollout_ledger_id']==b['id']

def test_unique_legacy_ledger_link_is_repaired_without_duplicate(runtime):
    rec=request(runtime); old=legacy_ledger(runtime,rec)
    result=ensure(runtime,rec)
    assert result=={'id':old['id'],'created':False}
    assert exact(runtime,rec).content['rollout_ledger_id']==old['id']
    assert state(runtime,rec)['ledger_count']==1

def test_ambiguous_legacy_ledger_does_not_cross_source(runtime,monkeypatch):
    left=request(runtime,source_id='left')
    # Ordinary append rejects this collision. Model a historical/imported
    # noncanonical storage key only in this temporary test database.
    original_key=runtime.store.sqlite._storage_key
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store.sqlite,'_storage_key',lambda rec:original_key(rec)+':imported' if rec.source_id=='right' else original_key(rec))
        request(runtime,source_id='right',record_id=left.record_id)
    legacy_ledger(runtime,left); before=state(runtime,left)
    with pytest.raises(RuntimeError,match='promotion_ledger_source_ambiguous'):
        ensure(runtime,left)
    assert state(runtime,left)==before


def test_current_dry_run_skips_ledger_even_if_stale_object_says_applied(runtime):
    rec=request(runtime); latest=exact(runtime,rec)
    latest.content['action']='dry_run'; runtime.store.rewrite(latest)
    assert ensure(runtime,rec) is None
    assert state(runtime,rec)['ledger_count']==0

def test_export_failure_is_post_commit_recoverable(runtime,monkeypatch,tmp_path):
    rec=request(runtime)
    with monkeypatch.context() as patch:
        def fail(*args,**kwargs): raise OSError('synthetic export outage')
        patch.setattr(runtime.store,'_flush_committed_exports',fail)
        result=ensure(runtime,rec)
        assert exact(runtime,rec).content['rollout_ledger_id']==result['id']
        with runtime.store.locked() as sql:
            pending=sql.pending_exports()
            assert {'records','policy_rollout_ledger'} <= {x['stream'] for x in pending}
            assert not sql.in_transaction
    runtime.store.flush_exports()
    with runtime.store.locked() as sql: assert not sql.pending_exports()
    ledger_file=runtime.store.auxiliary_log_dir/'policy_rollout_ledger.jsonl'
    assert any(json.loads(line)['payload']['id']==result['id'] for line in ledger_file.read_text().splitlines())
    runtime.close(); reopened=Runtime.create(root=tmp_path)
    try:
        assert exact(reopened,rec).content['rollout_ledger_id']==result['id']
        assert ensure(reopened,rec)=={'id':result['id'],'created':False}
    finally: reopened.close()

def test_public_memory_rule_promotion_reaches_initialized_watch(runtime):
    candidate=runtime.store.append(RecordEnvelope.create(kind='capability_candidate',title='Synthetic rule',summary='Structured recall',scope=SCOPE,status='candidate',content={'promotion_target':'memory_rule','target_capability':'memory.recall','candidate_patch':{'task_type':'memory.recall','retrieval_policy':{'strategy':'structured'}}},meta={'authority_tier':'L1'}))
    result=pm.promote_candidate(runtime,candidate_id=candidate.record_id,scope=SCOPE,eval_result={'verdict':'pass','scores':{'safety':1.0,'regression':1.0}},health=promotion_health_receipt())
    assert result['ok'] is True, result
    assert result['applied'] is True
    assert result['post_promotion_watch']['ok'] is True
    rec=runtime.store.get_by_id(result['promotion_request_id'],scope=SCOPE)
    assert rec.content['rollout_ledger_id']
    with runtime.store.locked() as sql: assert not sql.in_transaction


def test_backfill_skips_global_fallback_without_writing_outside_exact_scope(runtime):
    shared_scope=ScopeRef(tenant_id=SCOPE.tenant_id,agent_id=SCOPE.agent_id,workspace_id=SCOPE.workspace_id,user_id='')
    shared=request(runtime,scope=shared_scope); personal=request(runtime)
    result=pm.backfill_promotion_rollout_ledger(runtime,scope=SCOPE,limit=20)
    assert result['created_count']==1
    assert result['skipped_scope_count']==1
    assert exact(runtime,personal).content.get('rollout_ledger_id')
    assert not exact(runtime,shared).content.get('rollout_ledger_id')
    with runtime.store.locked() as sql:
        rows=sql.execute('SELECT user_id FROM policy_rollout_ledger').fetchall()
    assert [row[0] for row in rows]==[SCOPE.user_id]


def test_backfill_unique_candidate_source_retains_artifact_evidence(runtime):
    candidate=runtime.store.append(RecordEnvelope.create(kind='capability_candidate',title='Imported candidate',scope=SCOPE,source_id='imported',content={'experiment_id':'exp-imported'},meta={'applied_artifact_ids':['artifact-imported']}))
    rec=request(runtime); rec.content['candidate_id']=candidate.record_id
    rec.content['side_effect']={'ok':True}; runtime.store.rewrite(rec)
    result=pm.backfill_promotion_rollout_ledger(runtime,scope=SCOPE,limit=20)
    assert result['created_count']==1
    with runtime.store.locked() as sql:
        row=sql.execute('SELECT applied_pattern_id,details_json FROM policy_rollout_ledger WHERE promotion_id=?',(rec.record_id,)).fetchone()
    assert row['applied_pattern_id']=='artifact-imported'
    assert json.loads(row['details_json'])['patch_id']=='exp-imported'


@pytest.mark.parametrize('fault',['missing','wrong_kind','wrong_scope','ambiguous'])
def test_candidate_resolution_failure_leaves_no_partial_evidence(runtime,monkeypatch,fault):
    scope=SCOPE if fault!='wrong_scope' else ScopeRef(tenant_id=SCOPE.tenant_id,agent_id=SCOPE.agent_id,workspace_id=SCOPE.workspace_id,user_id='')
    candidate=RecordEnvelope.create(kind='memory' if fault=='wrong_kind' else 'capability_candidate',title='Candidate identity fixture',scope=scope,source_id='imported')
    if fault!='missing': runtime.store.append(candidate)
    rec=request(runtime); rec.content['candidate_id']=candidate.record_id; runtime.store.rewrite(rec)
    before=state(runtime,rec)
    with monkeypatch.context() as patch:
        if fault=='ambiguous':
            # Defensive shape from an imported/corrupt source partition; normal
            # append does not allow one ID/scope in multiple source partitions.
            duplicate=RecordEnvelope.from_dict(candidate.to_dict()); duplicate.source_id='other'
            original=runtime.store.sqlite.list_by_record_id_exact_scope
            patch.setattr(runtime.store.sqlite,'list_by_record_id_exact_scope',lambda rid,**kw:[candidate,duplicate] if rid==candidate.record_id else original(rid,**kw))
        with pytest.raises(ValueError,match='promotion_candidate_identity'):
            ensure(runtime,rec)
    assert state(runtime,rec)==before


def test_explicit_stale_candidate_source_is_not_rebound(runtime):
    candidate=runtime.store.append(RecordEnvelope.create(kind='capability_candidate',title='Candidate',scope=SCOPE,source_id='imported'))
    rec=request(runtime); rec.content['candidate_id']=candidate.record_id; runtime.store.rewrite(rec)
    stale=RecordEnvelope.from_dict(candidate.to_dict()); stale.source_id='default'
    before=state(runtime,rec)
    with pytest.raises(ValueError,match='promotion_candidate_identity'):
        pm._ensure_promotion_rollout_ledger(runtime,promotion_record=rec,candidate=stale,scope=SCOPE)
    assert state(runtime,rec)==before


def test_backfill_deduplicates_exact_identity_not_global_record_id(runtime):
    personal=request(runtime)
    shared_scope=ScopeRef(tenant_id=SCOPE.tenant_id,agent_id=SCOPE.agent_id,workspace_id=SCOPE.workspace_id,user_id='')
    shared=request(runtime,scope=shared_scope,record_id=personal.record_id)
    result=pm.backfill_promotion_rollout_ledger(runtime,scope=SCOPE,limit=20)
    assert result['created_count']==1
    assert result['skipped_scope_count']==1
    assert exact(runtime,personal).content.get('rollout_ledger_id')
    assert not exact(runtime,shared).content.get('rollout_ledger_id')
