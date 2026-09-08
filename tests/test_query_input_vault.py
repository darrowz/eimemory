from dataclasses import asdict
from hashlib import sha256

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.evaluation.query_input_vault import capture_query_input, load_query_input, capture_pipeline_status
from eimemory.evaluation.negative_production_query import accept_negative_query, evaluate_negative_queries
from eimemory.evaluation.production_query_dataset import collect_pending_production_queries
from eimemory.models.records import RecallBundle

BASE = {'tenant_id':'default','agent_id':'agent','workspace_id':'workspace','user_id':'owner'}


def seed(runtime):
    query = 'A genuine question without a known answer'
    exact = resolve_channel_scope('codex',BASE)
    digest = sha256(query.encode()).hexdigest()
    runtime.store.record_proactive_decision({'decision_id':'decision','channel':'codex','scope':exact,
        'source_key':sha256(b'codex').hexdigest(),'source_ids':['codex'],'session_id':'session',
        'turn_id':'turn','query_id':'turn','query_digest':digest,
        'effective_query_digest':sha256(('code.task\x1f'+query).encode()).hexdigest(),
        'task_type':'code.task','policy_version':'policy','release_identity':{'release_commit':'a'*40,
            'release_version':'1.0','deployment_receipt_id':'receipt','release_session_id':'release'},
        'release_bound':True,'control_cohort':False,'pair_id':'pair'},[],[])
    return query,exact


def test_vault_is_opt_in_digest_bound_and_scope_authorized(tmp_path,monkeypatch):
    runtime = Runtime.create(root=tmp_path)
    try:
        query,exact = seed(runtime)
        kwargs = dict(decision_id='decision',query=query,effective_query=query,
            explanation={'task_context':{'source_ids':['codex'],'secret':'must not capture'},'retrieval_status':'no_evidence'})
        assert capture_query_input(runtime,**kwargs)['status'] == 'disabled'
        monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY','1')
        assert capture_query_input(runtime,**kwargs)['status'] == 'captured'
        loaded = load_query_input(runtime,decision_id='decision',scope=exact,channel='codex',source_id='codex')
        assert loaded['query'] == query and 'secret' not in loaded['task_context']
        with pytest.raises(ValueError,match='boundary_mismatch'):
            load_query_input(runtime,decision_id='decision',scope={**exact,'user_id':'foreign'},channel='codex',source_id='codex')
        assert capture_query_input(runtime,**{**kwargs,'query':'different'})['status'] == 'decision_identity_mismatch'
        assert capture_pipeline_status(runtime,scope=BASE)['channels']['codex']['eligible_decisions'] == 1
    finally:
        runtime.close()


def test_vault_captures_real_proactive_decision(tmp_path, monkeypatch):
    from eimemory.retrieval.proactive import ProactiveRecallService
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    try:
        service = ProactiveRecallService(runtime, control_percent=0, release_identity={
            'release_commit':'a'*40, 'release_version':'1.0',
            'deployment_receipt_id':'receipt', 'release_session_id':'release'})
        result = service.decide(channel='codex', scope=BASE, source_ids=['codex'],
            session_id='real-session', query_id='real-turn',
            query='What did I require for reviewing documents?', task_type='code.task')
        loaded = load_query_input(runtime, decision_id=result['decision_id'],
            scope=resolve_channel_scope('codex', BASE), channel='codex', source_id='codex')
        assert loaded['query'] == 'What did I require for reviewing documents?'
        assert loaded['external_bundle'] is False
    finally:
        runtime.close()


def test_natural_negative_labels_bind_real_decision_without_positive_fabrication(tmp_path,monkeypatch):
    runtime = Runtime.create(root=tmp_path)
    try:
        query,exact = seed(runtime)
        collected = collect_pending_production_queries(runtime,scope=BASE)
        pending = collected['pending_record_ids'][0]
        result = accept_negative_query(runtime,pending_record_id=pending,
            packet={'query':query,'labeler':'operator','reason':'Reviewed the corpus and no answer is recorded.'},
            packet_evidence={'schema':'secure_dataset_fingerprint.v1','digest':'d'*64,'size':100},operator_scope=BASE)
        assert result['positive_coverage_increment'] == 0
        monkeypatch.setattr(runtime.memory,'recall',lambda **kwargs: RecallBundle(
            items=[],rules=[],reflections=[],confidence=0,next_action_hint='',explanation={'retrieval_status':'no_evidence'}))
        cases = [{'label_record_id':result['record_id'],'query':query}]
        report = evaluate_negative_queries(runtime,scope=BASE,cases=cases)
        assert report['passed'] and report['false_recall_rate'] == 0
        assert report['samples'][0]['observed_false_recall'] is False
        with pytest.raises(ValueError,match='authority_invalid'):
            evaluate_negative_queries(runtime,scope=BASE,cases=[{**cases[0],'query':'fabricated'}])
    finally:
        runtime.close()
