"""Controlled source contracts, NOT a real-provider/deployed-smoke receipt."""
from contextlib import nullcontext
from dataclasses import asdict
from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from eimemory.retrieval import caller_assistance as caller
from eimemory.retrieval import lightweight_admission as lightweight
from eimemory.retrieval import verification_budget as fence
from eimemory.retrieval import independent_evidence as independent
from eimemory.retrieval import postgres_vector as projection
from eimemory.retrieval import authority_gate as authority
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval.evidence_fragments import evidence_fragments, POLICY
from eimemory.retrieval.verifier_projection import (
    EvidenceWindow, project_evidence_windows, locate_visible_quote, verified_parent_excerpt,
)
from eimemory.models.records import RecordEnvelope, ScopeRef, RecallBundle
from eimemory.contracts.recall_evidence import business_recall_supported, bind_compact_evidence


@pytest.fixture(autouse=True)
def controlled_runtime(monkeypatch):
    # Explicit boundary: index/DTO projection, provider and wall clock are test
    # fixtures. Assertion checks, selector, binding and budget run real code.
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED','1')
    monkeypatch.setenv('EIMEMORY_OPERATOR_DISPLAY_NAME','鸿哥')
    monkeypatch.delenv('EIMEMORY_RECALL_EXPECTED_MODEL',raising=False)
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_POOL','0')
    monkeypatch.setenv('EIMEMORY_RECALL_GATEWAY_PREWARM','0')
    monkeypatch.setattr(independent,'active',lambda:False)
    monkeypatch.setattr(independent,'final_revalidate',lambda *a,**k:True)
    monkeypatch.setattr(caller,'current_verifier_route',lambda:None)
    monkeypatch.setattr(projection,'candidate_record_keyword_text',lambda record,*,max_text_chars:record.summary[:max_text_chars])
    monkeypatch.setattr(lightweight,'candidate_record_keyword_text',projection.candidate_record_keyword_text)


def record(text, rid='r'):
    row=RecordEnvelope.create(kind='memory',title='Saved original',summary=text,content={'text':text},
        scope=ScopeRef(tenant_id='test',agent_id='agent',workspace_id='project',user_id='user'))
    row.record_id=rid
    return row


def model(monkeypatch, callback):
    client=SimpleNamespace(timeout_seconds=90, complete=lambda **kw:SimpleNamespace(
        text=json.dumps(callback(json.loads(kw['user_prompt'])),ensure_ascii=False)))
    monkeypatch.setattr(caller,'configured_client',lambda:client)
    return client


def select_if_visible(quote):
    def complete(prompt):
        rows=prompt['candidates']
        for row in rows:
            if any(quote in text for text in [row['text'],*row.get('context',[])]):
                return {'selected':[{'id':row['id'],'quote':quote}]}
        return {'selected':[]}
    return complete


def lightweight_select(rows, query, *, fragment_index=0, deadline=0):
    def hints(r):
        fragments=evidence_fragments(r.summary)
        fragment=fragments[min(fragment_index,len(fragments)-1)]
        return {'evidence_fragment_id':fragment['id'],'fragment_policy':POLICY,
                'dense_vector_score':.9,'fragment_fts_score':.9,'_candidate_projection_text_chars':16000}
    selector=lightweight.LightweightAdmission(lightweight.LightweightConfig(enabled=True))
    return selector.select(rows,query=query,limit=1,validate=lambda r:True,
        backend_available=True,hints_for=hints,deadline_at=deadline)


@pytest.mark.parametrize('query,quote,count',[
    ('用户称呼鸿哥','请称呼用户为鸿哥',6),('提交带推送','提交之后需要推送到远端',8)])
def test_support_beyond_indexed_fragment_survives_full_chain(monkeypatch,query,quote,count):
    body=('项目背景说明。'*160)+quote+'。'+('其他历史背景。'*80)
    rows=[record(body)]+[record('记录天气与步行路线。',f'd{i}') for i in range(count-1)]
    model(monkeypatch,select_if_visible(quote))
    chosen,report=lightweight_select(rows,query)
    assert chosen==rows[:1]
    c=report['caller_assistance'];assert c['calls']==1 and c['candidate_count']==count
    assert c['model_selected_count']==1 and c['accepted_selection_count']==1
    p=c['proofs'][0];assert body[p['span_start']:p['span_end']]==quote
    assert p['span_start']>768
    bundle=RecallBundle(chosen,[],[],.8,'',{'engine_diagnostics':{},'relevance_selector':report})
    out=bundle.to_compact_dict(limit=1)
    assert out['items'][0]['evidence_excerpt']==quote
    assert business_recall_supported({'ok':True,'bundle':out})
    out['persona']=out.pop('items');out['items']=[]
    assert business_recall_supported({'ok':True,'bundle':bind_compact_evidence(out)})


def test_empty_model_selection_does_not_trigger_name_label_override(monkeypatch):
    row=record('用户（鸿哥）喜欢先给结论。');model(monkeypatch,lambda _: {'selected':[]})
    chosen,d=caller.verify_candidates(query='用户称呼鸿哥',candidates=[(row,row.summary)],limit=1)
    assert chosen==[] and d['outcome']=='no_support' and d['reason']=='model_no_selection'
    assert d['model_selected_count']==0 and not d.get('proofs')


def test_real_attribute_rejection_is_separate_from_empty_model(monkeypatch):
    row=record('项目 Alpha 的合同已讨论过。')
    model(monkeypatch,select_if_visible(row.summary))
    selected,d=caller.verify_candidates(query='项目 Alpha 的合同金额是多少？',candidates=[(row,row.summary)],limit=1)
    assert selected==[] and d['reason']=='answer_requirements_rejected'
    assert d['model_selected_count']==1 and d['answer_requirement_rejections']==1
    assert d['verification_outcome']=='no_support'


@pytest.mark.parametrize('response,code',[
    ({'selected':[{'id':'0','quote':'伪造不存在的证据'}]},'invalid_assistance_quote'),
    ({'selected':[{'id':'8','quote':'原始权威事实'}]},'invalid_assistance_reference'),
    ({'selected':[{'id':'0','quote':'原始权威事实','extra':1}]},'invalid_assistance_selection'),
    ({'selected':[],'other':1},'invalid_assistance_response')])
def test_invalid_model_output_is_unavailable_not_absence(monkeypatch,response,code):
    row=record('原始权威事实');model(monkeypatch,lambda _:response)
    selected,d=caller.verify_candidates(query='原始事实',candidates=[(row,row.summary)],limit=1)
    assert selected==[] and d['outcome']=='unavailable'
    assert d['validation_reason']==code and d['failure_stage']=='proof_validation'
    assert d['reason']=='caller_verification_failed'


def test_offsets_use_presented_duplicate_not_parent_first_occurrence(monkeypatch):
    quote='提交后执行推送操作'
    body='头部。'+('一般背景。'*180)+quote+'。'+('无关内容。'*400)+'提交带推送：'+quote+'。'
    row=record(body);model(monkeypatch,select_if_visible(quote))
    selected,d=caller.verify_candidates(query='提交带推送',candidates=[(row,body)],limit=1)
    assert selected==[row]
    assert d['proofs'][0]['span_start']==body.rindex(quote)
    assert d['proofs'][0]['span_start']!=body.index(quote)


def test_projection_never_splices_nonadjacent_windows():
    windows=(EvidenceWindow(0,'需要提交'),EvidenceWindow(100,'并推送'))
    assert locate_visible_quote('需要提交并推送',windows) is None
    assert locate_visible_quote('并推送',windows)==(100,103)


@pytest.mark.parametrize('length',[0,1,767,768,769,3000,16000,20000])
def test_projection_bounds_and_provenance(length):
    body=('连续背景和尾部事实。'*2000)[:length]
    windows=project_evidence_windows('尾部事实',body)
    assert 1<=len(windows)<=2
    for w in windows:
        assert 0<=w.start<=w.end<=16000
        assert len(w.text)<=768 and body[w.start:w.end]==w.text


@pytest.mark.parametrize('invalid',[0,-1,769,True,float('inf')])
def test_projection_rejects_invalid_window_bound(invalid):
    with pytest.raises(ValueError):project_evidence_windows('query','text',limit=invalid)


def test_delayed_completed_model_survives_lightweight_final_check(monkeypatch):
    now=[100.0]
    for module in (caller,lightweight,fence,authority):monkeypatch.setattr(module,'perf_counter',lambda:now[0])
    row=record('提交之后需要推送到远端。')
    def complete(prompt):
        now[0]=104.0
        return {'selected':[{'id':'0','quote':'提交之后需要推送到远端'}]}
    model(monkeypatch,complete)
    with fence.verification_budget_scope():
        chosen,state=lightweight_select([row],'提交带推送',deadline=102.0)
    assert chosen==[row] and state['caller_assistance']['outcome']=='supported'
    assert fence.final_authority_deadline(102)==102


def test_fake_diagnostics_cannot_grant_budget(monkeypatch):
    now=[100.0]
    for module in (caller,lightweight,fence):monkeypatch.setattr(module,'perf_counter',lambda:now[0])
    row=record('提交之后需要推送到远端。')
    def fake(**kwargs):
        now[0]=104
        return [],{'status':'no_evidence','outcome':'no_support','calls':1,'reason':'model_no_selection'}
    monkeypatch.setattr(caller,'verify_candidates',fake)
    with fence.verification_budget_scope():
        chosen,state=lightweight_select([row],'提交带推送',deadline=102)
    assert chosen==[] and state['status']=='unavailable'


def test_engine_entry_owns_and_resets_budget(monkeypatch):
    observed=[]
    monkeypatch.setattr(independent,'evidence_scope',lambda *a:nullcontext())
    class Example:
        store=object()
        recall=GovernedRecallEngine.recall
        def _recall(self, request):
            observed.append(fence._BUDGET.get())
            raise RuntimeError('controlled')
    with pytest.raises(RuntimeError):Example().recall(SimpleNamespace(query='query'))
    assert observed and observed[0] is not None
    assert fence._BUDGET.get() is None


@pytest.mark.parametrize('outcome',['success','exception','late','repeat'])
def test_only_bounded_success_grants_fence(monkeypatch,outcome):
    now=[100.0];monkeypatch.setattr(fence,'perf_counter',lambda:now[0])
    with fence.verification_budget_scope():
        if outcome=='exception':
            with pytest.raises(RuntimeError):
                with fence.bounded_verification_call(10):raise RuntimeError('controlled')
            assert fence.final_authority_deadline(101)==101
        else:
            with fence.bounded_verification_call(10):now[0]=111.0 if outcome=='late' else 104.0
            assert fence.final_authority_deadline(101)==(101 if outcome=='late' else 104.75)
            if outcome=='repeat':
                with pytest.raises(ValueError):
                    with fence.bounded_verification_call(10):pass


@pytest.mark.parametrize('change',['none','content','status','source','scope'])
def test_new_authority_read_still_rejects_inflight_change(monkeypatch,change):
    import copy
    now=[100.0]
    for m in (caller,lightweight,fence,authority):monkeypatch.setattr(m,'perf_counter',lambda:now[0])
    row=record('提交之后需要推送到远端。');fresh=copy.deepcopy(row);reads=[]
    class Engine:
        def _record_key(self,item):return (item.record_id,tuple(asdict(item.scope).values()),item.source_id)
        def _hydrate_records_batch(self,rows,*,deadline_at):
            reads.append(deadline_at)
            return {self._record_key(r):copy.deepcopy(fresh) for r in rows
                    if self._record_key(r)==self._record_key(fresh)}
        @authority.enforce_selection_authority
        def select(self,items,**kwargs):
            return lightweight_select(items,'提交带推送',deadline=kwargs['deadline_at'])
    def complete(prompt):
        now[0]=104
        if change=='content':fresh.summary='中途改写后的内容'
        if change=='status':fresh.status='deleted'
        if change=='source':fresh.source_id='other-source'
        if change=='scope':fresh.scope.user_id='other-user'
        return {'selected':[{'id':'0','quote':'提交之后需要推送到远端'}]}
    model(monkeypatch,complete)
    with fence.verification_budget_scope():
        selected,state=Engine().select([row],limit=1,deadline_at=102,validate=lambda r:True)
    assert len(reads)==2 and reads[-1]>104
    if change=='none':assert selected==[row]
    else:
        assert not selected and state['status']=='unavailable'
        assert not state['caller_assistance'].get('proofs')


def test_excerpt_requires_parent_digest_and_bounded_span():
    row=record('原始权威事实')
    p={'record_id':'r','quote_digest':sha256(row.summary.encode()).hexdigest(),'span_start':0,'span_end':len(row.summary)}
    assert verified_parent_excerpt(row,p)['evidence_excerpt']==row.summary
    p['quote_digest']='0'*64
    assert verified_parent_excerpt(row,p)=={}


def test_compact_quote_budget_omits_preview_not_records_or_proofs():
    from eimemory.models.records import _fit_compact_payload
    from eimemory.contracts.recall_evidence import bind_compact_evidence
    quote='很长的原文证据'*100
    p={'items':[{'record_id':'r','status':'active','title':'标题','summary':'说明',
                 'evidence_excerpt':quote}], 'retrieval_status':'evidence_found',
       'recall_diagnostics':{'admission_status':'evidence_found','caller_assistance':{
           'status':'evidence_found','outcome':'supported',
           'proofs':[{'record_id':'r','quote_digest':sha256(quote.encode()).hexdigest(),
                      'span_start':0,'span_end':len(quote)}]}}}
    out=_fit_compact_payload(p,maximum_bytes=1500)
    assert len(json.dumps(out,ensure_ascii=False,separators=(',',':')).encode())<=1500
    assert len(out['items'])==1 and 'evidence_excerpt' not in out['items'][0]
    assert business_recall_supported({'ok':True,'bundle':bind_compact_evidence(out)})


def test_no_answer_full_chain_is_clean_absence(monkeypatch):
    rows=[record('只记录了步行路线和天气。',f'd{i}') for i in range(8)]
    model(monkeypatch,lambda _: {'selected':[]})
    selected,state=lightweight_select(rows,'未记录的旅行订票偏好')
    assert selected==[] and state['status']=='no_evidence'
    bundle=RecallBundle([],[],[],0,'',{'engine_diagnostics':{},'relevance_selector':state})
    p=bundle.to_compact_dict(limit=1);d=p['recall_diagnostics']['caller_assistance']
    assert p['retrieval_status']=='no_evidence' and d['reason']=='model_no_selection'
    assert d['candidate_count']==8 and d['calls']==1 and d['model_selected_count']==0
    assert d['outcome']=='no_support' and not d.get('proofs')
    assert not business_recall_supported({'ok':True,'bundle':p})


def test_window_edge_cannot_create_standalone_name(monkeypatch):
    body=('無关背景'*300)+'大鸿哥。'+('x'*765)
    assert body[-768:].startswith('鸿哥。')
    row=record(body);model(monkeypatch,lambda _: {'selected':[{'id':'0','quote':'鸿哥'}]})
    selected,d=caller.verify_candidates(query='记忆称呼',candidates=[(row,body)],limit=1)
    assert selected==[] and d['outcome']=='unavailable'
    assert d['validation_reason']=='invalid_assistance_quote'


def test_model_identity_guard_is_preserved(monkeypatch):
    row=record('提交之后需要推送到远端。')
    monkeypatch.setenv('EIMEMORY_RECALL_EXPECTED_MODEL','verified-route-model')
    model(monkeypatch,select_if_visible(row.summary))
    selected,d=caller.verify_candidates(query='提交带推送',candidates=[(row,row.summary)],limit=1)
    assert selected==[] and d['reason']=='caller_model_identity_changed'
    assert d['outcome']=='unavailable' and not d.get('proofs')


def test_unauthorized_candidate_never_reaches_selector():
    row=record('提交之后需要推送到远端。');calls=[]
    class Engine:
        def _record_key(self,item):return item.record_id
        def _hydrate_records_batch(self,rows,*,deadline_at):return {}
        @authority.enforce_selection_authority
        def select(self,items,**kwargs):
            calls.append(items)
            raise AssertionError('must not run')
    selected,state=Engine().select([row],limit=1,validate=lambda item:False)
    assert not selected and not calls and state['status']=='unavailable'
