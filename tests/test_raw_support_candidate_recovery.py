"""Sparse character candidates require model selection and current authority."""
import json
import pytest
from eimemory.raw.retrieval import search_raw_chunks
from test_raw_answer_support import Client, no_support
from test_raw_answer_support_consumer import configured, fetch
from test_runtime_raw_citation_delivery import runtime, owner, CANONICAL, add_raw, persist_mutation

QUERY='上新后怎样告知同伴？'
RELATED='上新后不能删除旧资料。'
MISSING='发布结束，公告栏需列出新增内容。'

PARAPHRASE = "课上迟到该怎么进门？"
ANSWER = "晚到的学员先敲门，等教师应允再入场。"

@pytest.mark.parametrize("answerable", [True, False])
def test_empty_related_search_can_submit_authorized_passages_for_review(runtime, monkeypatch, answerable):
    text = ANSWER if answerable else "晚到的学员上周弄丢了门卡。"
    candidate = add_raw(runtime, text, scope=owner(CANONICAL))
    add_raw(runtime, ANSWER + " 外部文档。", scope=owner(CANONICAL), source_id="outside")
    add_raw(runtime, ANSWER + " 其他用户。", scope=owner({**CANONICAL, "user_id": "other"}))
    rows = search_raw_chunks(runtime.store, query=PARAPHRASE, scope=owner(CANONICAL),
        source_ids=["hermes"], task_context={"evidence_intent": "fact_citation"}, limit=5)
    assert not rows and rows.decision.status == "related"

    def review(packet):
        assert [p["text"] for p in packet["passages"]] == [candidate.content["text"]]
        if not answerable:
            return no_support(packet)
        return {"request_digest": packet["request_digest"], "status": "supported",
                "reason": "direct_support", "selected": [packet["passages"][0]["id"]]}

    client = Client(review)
    configured(monkeypatch, client)
    result = fetch(runtime, query=PARAPHRASE)
    lane = result["bundle"]["raw_citation"]
    assert lane["decision"]["status"] == "related" and not lane["evidence"]
    assert len(client.calls) == 1
    assert lane["answer_support"]["status"] == ("supported" if answerable else "unsupported")
    assert [p["ref"][0] for p in lane["support_evidence"]] == ([candidate.record_id] if answerable else [])


@pytest.mark.parametrize("case", [
    "disabled", "unconfigured", "reject", "row_limit", "snapshot_failure", "review_timeout", "withdrawn",
])
def test_empty_related_recovery_respects_review_and_authority_boundaries(runtime, monkeypatch, case):
    from eimemory.raw import citation_gate, support_candidates
    from eimemory.raw.boundary import RawRecallUnavailable

    record = add_raw(runtime, ANSWER, scope=owner(CANONICAL))
    def response(packet):
        if case == "review_timeout":
            raise TimeoutError
        if case == "withdrawn":
            record.status = "withdrawn"
            persist_mutation(runtime, record)
        return {"request_digest": packet["request_digest"], "status": "supported",
                "reason": "direct_support", "selected": [packet["passages"][0]["id"]]}
    client = Client(response)
    configured(monkeypatch, None if case == "unconfigured" else client)
    if case == "disabled":
        monkeypatch.delenv("EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED")
    if case == "row_limit":
        add_raw(runtime, "资料存档。", scope=owner(CANONICAL))
        monkeypatch.setattr(citation_gate, "MAX_ROWS", 1)
    if case == "snapshot_failure":
        def unavailable(*args, **kwargs):
            raise RawRecallUnavailable("authority_unavailable")
        monkeypatch.setattr(support_candidates, "recover_support_candidates", unavailable)
    result = fetch(runtime, query="Where is Xiphopyxis stored?" if case == "reject" else PARAPHRASE)
    lane = result["bundle"]["raw_citation"]
    assert lane["answer_support"]["status"] == "unknown"
    assert not lane["support_evidence"] and not lane["evidence"]
    assert len(client.calls) == int(case in {"withdrawn", "review_timeout"})
    if case == "reject":
        assert lane["decision"]["status"] == "reject"
    if case == "snapshot_failure":
        assert lane["admission"]["reason"] == "support_recovery_unavailable"
    if case == "withdrawn":
        assert lane["admission"]["reason"] == "support_snapshot_changed"


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
