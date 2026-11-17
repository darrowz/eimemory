"""Sparse character candidates require model selection and current authority."""
import json
from eimemory.raw.retrieval import search_raw_chunks
from test_raw_answer_support import Client, no_support
from test_raw_answer_support_consumer import configured, fetch
from test_runtime_raw_citation_delivery import runtime, owner, CANONICAL, add_raw, persist_mutation

QUERY='上新后怎样告知同伴？'
RELATED='上新后不能删除旧资料。'
MISSING='发布结束，公告栏需列出新增内容。'


def setup(runtime):
    a=add_raw(runtime,RELATED,scope=owner(CANONICAL))
    b=add_raw(runtime,MISSING,scope=owner(CANONICAL))
    return a,b


def select(packet):
    selected=[p['id'] for p in packet['passages'] if p['text']==MISSING]
    assert selected
    return {'request_digest':packet['request_digest'],'status':'supported','reason':'direct_support','selected':selected}


def test_sparse_recovery_does_not_change_related_rank_or_create_lexical_proof(runtime,monkeypatch):
    first,second=setup(runtime);client=Client(select);configured(monkeypatch,client)
    before=search_raw_chunks(runtime.store,query=QUERY,scope=owner(CANONICAL),source_ids=['hermes'],limit=5)
    before_ids=[r['record']['record_id'] for r in before]
    assert second.record_id not in before_ids
    result=fetch(runtime,query=QUERY);lane=result['bundle']['raw_citation']
    assert [r['ref'][0] for r in lane['evidence']]==before_ids
    assert [r['ref'][0] for r in lane['support_evidence']]==[second.record_id]
    assert lane['decision']['status']=='related' and lane['answer_support']['status']=='supported'
    after=search_raw_chunks(runtime.store,query=QUERY,scope=owner(CANONICAL),source_ids=['hermes'],limit=5)
    assert [r['record']['record_id'] for r in after]==before_ids


def test_character_overlap_alone_never_grants_support(runtime,monkeypatch):
    first,second=setup(runtime);client=Client(no_support);configured(monkeypatch,client)
    lane=fetch(runtime,query=QUERY)['bundle']['raw_citation']
    assert lane['evidence'] and lane['support_evidence']==[]
    assert lane['answer_support']['status']=='unsupported'
    assert any(p['text']==MISSING for p in json.loads(client.calls[0]['user_prompt'])['passages'])


def test_recovered_source_is_revalidated_after_model(runtime,monkeypatch):
    first,second=setup(runtime)
    def mutate(packet):
        answer=select(packet);second.status='deprecated';persist_mutation(runtime,second);return answer
    configured(monkeypatch,Client(mutate))
    lane=fetch(runtime,query=QUERY)['bundle']['raw_citation']
    assert lane['support_evidence']==[] and lane['evidence']==[]
    assert lane['admission']['reason']=='support_snapshot_changed'


def test_recovery_never_reads_other_owner_or_unrequested_source(runtime,monkeypatch):
    setup(runtime)
    hidden_owner=add_raw(runtime,'发布结束，公告栏需列出新增内容。私有凭据甲。',
                         scope=owner({**CANONICAL,'user_id':'other-owner'}))
    hidden_source=add_raw(runtime,'发布结束，公告栏需列出新增内容。私有凭据乙。',
                          scope=owner(CANONICAL),source_id='excluded-source')
    seen=[];original=runtime.store.get_by_exact_ref
    def observe(record_id,**kwargs):
        seen.append(record_id)
        return original(record_id,**kwargs)
    monkeypatch.setattr(runtime.store,'get_by_exact_ref',observe)
    client=Client(no_support);configured(monkeypatch,client)
    lane=fetch(runtime,query=QUERY)['bundle']['raw_citation']
    assert lane['answer_support']['reviewed']
    assert hidden_owner.record_id not in seen and hidden_source.record_id not in seen
    assert '私有凭据' not in client.calls[0]['user_prompt']
