"""Final-output regression contracts; no model, database or network required."""
import copy
from hashlib import sha256
import itertools

import pytest
from eimemory.contracts.recall_evidence import (
    bind_compact_evidence, bind_final_selection, business_recall_supported,
)
from eimemory.contracts.recall_boundary import normalize_retrieval_state
from eimemory.retrieval.diagnostics import compact_recall_diagnostics


def proof(rid='r'):
    return dict(record_id=rid, quote_digest=sha256(b'fact').hexdigest(), span_start=0, span_end=4)


def payload(status='evidence_found', reason='reviewed_original_evidence'):
    supported = status in {'evidence_found', 'degraded'}
    return {'items':[{'record_id':'r', 'status':'active'}] if supported else [],
        'retrieval_status':status,
        'recall_diagnostics':{'admission_status':status, 'caller_assistance':{
            'status':'evidence_found' if supported else 'no_evidence',
            'outcome':'supported' if supported else 'no_support', 'reason':reason,
            'verification_outcome':'supported' if supported else 'no_support',
            **({'proofs':[proof()]} if supported else {})}}}


@pytest.mark.parametrize('reason',['model_no_selection','answer_requirements_rejected',
                                   'requested_attribute_absent','final_selection_unavailable',''])
def test_negative_never_gets_final_selection_failure(reason):
    original=payload('no_evidence',reason)
    out=bind_compact_evidence(original)
    caller=out['recall_diagnostics']['caller_assistance']
    assert out['retrieval_status']=='no_evidence'
    assert (caller['status'],caller['outcome'])==('no_evidence','no_support')
    assert not caller.get('reason','').startswith('final_')
    assert 'proofs' not in caller
    assert not business_recall_supported({'ok':True,'bundle':out})
    assert bind_compact_evidence(out)==out
    assert original==payload('no_evidence',reason), 'binding must not mutate input'


def test_supported_then_final_empty_is_failure_not_absence():
    original=payload();original['items']=[]
    out=bind_compact_evidence(original);caller=out['recall_diagnostics']['caller_assistance']
    assert out['retrieval_status']=='unavailable'
    assert caller['reason']=='final_selection_empty'
    assert caller['verification_outcome']=='supported'
    assert caller['outcome']=='unavailable' and not caller.get('proofs')
    assert out==bind_compact_evidence(out)


@pytest.mark.parametrize('failure',['caller_model_unavailable','authority_or_deadline_changed',
                                   'caller_verification_failed','assistance_budget_exhausted'])
def test_stale_public_absence_never_overwrites_operational_failure(failure):
    p=payload('no_evidence');d=p['recall_diagnostics'];d['admission_status']='unavailable'
    d['caller_assistance'].update(status='unavailable',outcome='unavailable',reason=failure)
    out=bind_compact_evidence(p)
    assert out['retrieval_status']=='unavailable'
    assert out['recall_diagnostics']['caller_assistance']['reason']==failure
    assert not business_recall_supported({'ok':True,'bundle':out})


@pytest.mark.parametrize('mutation',['digest','id','duplicate','missing','boolean_span'])
def test_broken_final_proof_has_specific_reason(mutation):
    p=payload();c=p['recall_diagnostics']['caller_assistance']
    if mutation=='digest':c['proofs'][0]['quote_digest']='not-a-digest'
    elif mutation=='id':c['proofs'][0]['record_id']='other'
    elif mutation=='duplicate':p['items']*=2
    elif mutation=='missing':c.pop('proofs')
    else:c['proofs'][0]['span_start']=False
    out=bind_compact_evidence(p)
    assert out['recall_diagnostics']['caller_assistance']['reason']=='final_proof_binding_failed'
    assert out['retrieval_status']=='unavailable'
    assert not business_recall_supported({'ok':True,'bundle':out})


@pytest.mark.parametrize('section',['items','persona'])
@pytest.mark.parametrize('status',['evidence_found','degraded'])
def test_bound_success_survives_persona_and_degraded_status(section,status):
    p=payload(status)
    if section=='persona':p['persona']=p['items'];p['items']=[]
    out=bind_compact_evidence(p)
    assert business_recall_supported({'ok':True,'bundle':out})
    assert out['recall_diagnostics']['selected_count']==1
    assert bind_compact_evidence(out)==out


@pytest.mark.parametrize('bad_items,bad_persona',[(None,[]),({},[]),([],{}),([],[{}, {}, {}])])
def test_malformed_final_sections_cannot_be_successful_absence(bad_items,bad_persona):
    p=payload('no_evidence');p.update(items=bad_items,persona=bad_persona)
    out=bind_compact_evidence(p)
    assert out['retrieval_status']=='unavailable'
    assert out['recall_diagnostics']['caller_assistance']['reason']=='final_payload_invalid'


def test_authority_normalization_clears_stale_proofs():
    p=payload();state={'status':'unavailable','caller_assistance':p['recall_diagnostics']['caller_assistance'],
                      'scored':[{'admitted':True}]}
    out=normalize_retrieval_state(state,selected_count=0,incomplete=True)
    assert out['caller_assistance']['outcome']=='unavailable'
    assert not out['caller_assistance'].get('proofs') and 'scored' not in out


def test_diagnostics_keep_stages_not_raw_candidates():
    c={'status':'no_evidence','outcome':'no_support','reason':'answer_requirements_rejected',
       'verification_outcome':'no_support', 'calls':1,'candidate_count':8,'visible_candidate_count':8,
       'visible_window_count':16,'candidate_text_chars':12000,'visible_text_chars':10000,
       'windowed_candidate_count':4,'model_selected_count':1,'answer_requirement_rejections':1,
       'quote_validation_rejections':0,'accepted_selection_count':0,
       'projection_trace':[{'text':'PRIVATE'}], 'raw_model_response':'PRIVATE',
       'failure_stage':'proof_validation','validation_reason':'invalid_assistance_quote'}
    d=compact_recall_diagnostics({'engine_diagnostics':{'drops':{'auxiliary_authority_changed':1}},
        'relevance_selector':{'status':'no_evidence','caller_assistance':c,
            'dropped_reasons':{'selection_deadline_exceeded':1,'authority_changed':2}}})
    safe=d['caller_assistance']
    for key in ('model_selected_count','answer_requirement_rejections','visible_window_count',
                'accepted_selection_count','failure_stage','validation_reason','verification_outcome'):
        assert safe[key]==c[key]
    assert 'projection_trace' not in safe and 'raw_model_response' not in safe
    assert 'PRIVATE' not in str(d)
    assert d['admission_drops']['authority_changed']==2
    assert d['engine_drops']['auxiliary_authority_changed']==1


def test_binding_is_idempotent_and_never_mints_receipt_over_state_matrix():
    for pub,adm,caller_status,outcome,count in itertools.product(
            ['evidence_found','degraded','no_evidence','unavailable','ambiguous'],
            ['evidence_found','degraded','no_evidence','unavailable'],
            ['evidence_found','no_evidence','unavailable'],['supported','no_support','unavailable'],[0,1]):
        p=payload();p['items']=p['items'][:count];p['retrieval_status']=pub
        p['recall_diagnostics']['admission_status']=adm
        p['recall_diagnostics']['caller_assistance'].update(status=caller_status,outcome=outcome)
        original=copy.deepcopy(p)
        bound=bind_compact_evidence(p)
        assert bound==bind_compact_evidence(bound)
        assert p==original
        if business_recall_supported({'ok':True,'bundle':bound}):
            assert count==1 and caller_status=='evidence_found' and outcome=='supported'
            assert pub in {'evidence_found','degraded'} and adm in {'evidence_found','degraded'}
