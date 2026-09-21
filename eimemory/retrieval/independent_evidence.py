"""Request-local independent-evidence routing before caller-model verification.

Three routes: reviewed local support, optional provable visible-set no-support,
or the UNCHANGED model verifier. Off/shadow/enforce are independent of the Luna
model, inference parameters and time budgets. No inference or answer cache here.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import isfinite
import os
import sqlite3
from time import perf_counter
from uuid import uuid4

from eimemory.storage import independent_evidence as catalog
from .evidence_query import POLICY as QUERY_POLICY, parse_query

POLICY = 'independent-evidence-router.v1'
_CONTEXT = ContextVar('independent_evidence_request', default=None)
LOCAL_CAP_SECONDS = .050
_TRANSPORT_CONTEXT = frozenset({'scope_strategy','exact_scope_only','recall_mode',
    'runtime_channel','task_type','_recall_deadline_monotonic','source_ids',
    'query_scope_limit','agent_id','workspace_id','user_id','tenant_id',
    'session_id','turn_id','query_id','include_report_records'})
REASONS = frozenset({'off','no_context','context_requires_model','query_requires_model',
    'no_contract','contract_unusable','contract_stale','catalog_unavailable','candidate_not_visible','ambiguous_candidates',
    'evidence_changed','authority_changed','local_budget_exhausted',
    'reviewed_original_evidence','requested_attribute_absent','local_error'})


def mode():
    value = os.environ.get('EIMEMORY_INDEPENDENT_EVIDENCE_MODE', 'off')
    return value if value in {'off','shadow','enforce'} else 'off'


def negative_enabled():
    return os.environ.get('EIMEMORY_RECALL_ATTRIBUTE_PRECHECK', '0') == '1'


def active():
    return mode() != 'off' or negative_enabled()


def identity():
    return {'policy':POLICY, 'mode':mode(), 'query_policy':QUERY_POLICY,
            'catalog_schema':catalog.SCHEMA, 'attribute_precheck':negative_enabled()}


def proactive_policy_suffix(root):
    if not active():
        return ''
    token = catalog.policy_token(root)
    if token == 'unavailable':
        # Missing authority must never give two persisted decisions a reusable identity.
        token += '-' + uuid4().hex[:16]
    return '+ie1:' + mode() + ':' + str(int(negative_enabled())) + ':' + token


@contextmanager
def evidence_scope(root, request):
    """Only the governed engine binds authority, never a candidate/meta/env root."""
    if not active():
        yield
        return
    scope = request.scope
    scope = {key:getattr(scope,key) for key in catalog.SCOPE_FIELDS}
    context = request.task_context_dict()
    token = _CONTEXT.set((root, request.query, scope, request.source_ids,
                          isinstance(context,dict) and not(set(context)-_TRANSPORT_CONTEXT)))
    try:
        yield
    finally:
        _CONTEXT.reset(token)


@dataclass
class Decision:
    status: str
    report: dict
    index: int | None = None
    proof: dict | None = None
    scored: dict | None = None


def _scope_of(record):
    return {k:getattr(record.scope,k) for k in catalog.SCOPE_FIELDS}


def _secret_absent(query, candidates):
    from .caller_assistance import _QUESTION
    from .answer_requirements import requested_attribute, supports_requested_attribute
    if (not negative_enabled() or not candidates or _QUESTION.search(query or '')
            or requested_attribute(query) != 'secret'):
        return False
    # Same first-eight/prefix-768 boundary as the existing model verifier.
    return all(not supports_requested_attribute('secret', text[:768])
               for _record,text in candidates[:8])


def probe(*, query, candidates, limit, deadline_at=0.0):
    started = perf_counter()
    def result(status, reason, **kwargs):
        return Decision(status, {'policy':POLICY,'mode':mode(),'status':status,
            'reason':reason,'elapsed_ms':round((perf_counter()-started)*1000,3),
            'visible_candidate_count':len(candidates[:8])}, **kwargs)
    if not active():
        return result('needs_model','off')
    if limit <= 0 or not candidates:
        return result('needs_model','candidate_not_visible')
    if deadline_at and perf_counter() >= deadline_at:
        return result('needs_model','local_budget_exhausted')
    bound = _CONTEXT.get()
    if bound is None or bound[1] != query:
        return result('needs_model','no_context')
    root, _, exact_scope, sources, context_compatible = bound
    # The negative shortcut proves only the existing final quote rule; it does
    # not need a reviewed positive contract and never certifies retrieval completeness.
    try:
        if _secret_absent(query,candidates):
            # Even negative-only rollout needs clocks for proactive retry invalidation.
            negative_deadline = min(deadline_at,started+LOCAL_CAP_SECONDS) if deadline_at else started+LOCAL_CAP_SECONDS
            with catalog.connect(root,deadline=negative_deadline) as conn:
                catalog.validate_schema(conn)
            if perf_counter() >= negative_deadline:
                return result('needs_model','local_budget_exhausted')
            return result('no_support','requested_attribute_absent')
        if mode() == 'off':
            return result('needs_model','off')
        if not context_compatible:
            return result('needs_model','context_requires_model')
        slot = parse_query(query)
        if slot is None:
            return result('needs_model','query_requires_model')
        local_deadline=min(deadline_at,started+LOCAL_CAP_SECONDS) if deadline_at else started+LOCAL_CAP_SECONDS
        with catalog.connect(root, deadline=local_deadline) as conn:
            found = catalog.lookup(conn,exact_scope,slot)
            if found is None:
                return result('needs_model','no_contract')
            cid,packet=found
            fragment=packet['fragment']
            indices=[]
            for index,(record,text) in enumerate(candidates[:8]):
                if (record.record_id == packet['record_id'] and record.source_id==packet['source_id']
                        and _scope_of(record)==exact_scope and record.kind in ('memory','rule')
                        and record.status=='active' and (sources is None or record.source_id in sources)):
                    indices.append(index)
            if len(indices)!=1:
                return result('needs_model','ambiguous_candidates' if indices else 'candidate_not_visible')
            index=indices[0]
            record,text=candidates[index]
            visible=text[:768]
            # A full-prefix caller and a fragment caller have different views;
            # both must be exact projections of the same reviewed authority.
            if (catalog.text_digest(visible) not in
                    {packet['prefix_digest'], catalog.text_digest(fragment['text'])}
                    or fragment['text'] not in visible):
                return result('needs_model','evidence_changed')
            from .answer_requirements import supports_answer_requirements
            if not supports_answer_requirements(query,fragment['text'],getattr(record,'aliases',())):
                return result('needs_model','query_requires_model')
            # A second read detects revocation, unseen additions and relation changes.
            current=catalog.lookup(conn,exact_scope,slot)
            if current is None or current[0]!=cid or current[1]!=packet:
                return result('needs_model','authority_changed')
            if perf_counter() >= local_deadline:
                return result('needs_model','local_budget_exhausted')
        span=visible.index(fragment['text'])
        proof={'record_id':record.record_id,'quote_digest':catalog.text_digest(fragment['text']),
               'span_start':span,'span_end':span+len(fragment['text'])}
        scored={'record_id':record.record_id,'source_id':record.source_id,'scope':exact_scope,
                'projection_text_chars':16000,'fragment_id':fragment['id'],
                'span_start':fragment['start'],'span_end':fragment['end'],'admitted':True}
        decision=result('supported','reviewed_original_evidence',index=index,proof=proof,scored=scored)
        decision.report['contract_id']=cid
        return decision
    except catalog.CatalogError as exc:
        code = str(exc)
        reason = {'contract_stale':'contract_stale',
                  'catalog_schema_unavailable':'catalog_unavailable',
                  'authority_changed':'authority_changed',
                  'local_deadline':'local_budget_exhausted'}.get(code,'contract_unusable')
        return result('needs_model',reason)
    except (OSError,ValueError,TypeError,AttributeError,KeyError,sqlite3.Error):
        # Optional local catalog cannot turn a model-capable request into an error.
        return result('needs_model','contract_unusable')


def local_result(decision, candidates):
    if decision.status=='no_support':
        return [], {'status':'no_evidence','outcome':'no_support','calls':0,
            'reason':'requested_attribute_absent','candidate_count':len(candidates),
            'local_evidence':decision.report}
    if decision.status=='supported' and mode()=='enforce':
        return [candidates[decision.index][0]], {'status':'evidence_found','outcome':'supported',
            'calls':0,'reason':'reviewed_original_evidence','candidate_count':len(candidates),
            'proofs':[decision.proof], 'independent_scored':[decision.scored],
            'local_evidence':decision.report}
    return None


def shadow_comparison(decision, chosen, report, candidates):
    info=dict(decision.report)
    if mode()=='shadow' and decision.status=='supported':
        if report.get('status')=='unavailable':
            info['selection_agreement']='model_unavailable'
        else:
            expected=candidates[decision.index][0]
            def key(r):
                return (*catalog.scope_tuple(_scope_of(r)),r.source_id,r.record_id)
            info['selection_agreement']='same_records' if [key(r) for r in chosen]==[key(expected)] else 'different_records'
    return info


def safe_report(value):
    """Explicit allowlist: no query, source text, arbitrary labels, or proof bodies."""
    if not isinstance(value,dict):
        return {}
    out={}
    for key,allowed in {'mode':{'off','shadow','enforce'},
            'status':{'supported','no_support','needs_model'},'reason':REASONS,
            'selection_agreement':{'same_records','different_records','model_unavailable'}}.items():
        if isinstance(value.get(key),str) and value[key] in allowed:
            out[key]=value[key]
    for key in ('elapsed_ms','visible_candidate_count'):
        number=value.get(key)
        if (type(number) in (int,float) and number>=0
                and (type(number) is int or isfinite(number))):
            out[key]=round(min(number,1_000_000),3)
    cid=value.get('contract_id')
    if isinstance(cid,str) and len(cid)==64 and all(c in '0123456789abcdef' for c in cid):
        out['contract_id']=cid
    return out


def final_revalidate(report, *, deadline_at=0.0):
    """Last local-proof fence after existing selected-record validation.

    An ordinary model result has no independent_scored metadata and is untouched.
    A local proof that becomes stale while the selector finishes is unavailable,
    not silently accepted or automatically retried.
    """
    if not report.get('independent_scored'):
        return True
    bound=_CONTEXT.get()
    if mode()!='enforce' or bound is None:
        return False
    root,query,scope,_sources,_compatible=bound
    slot=parse_query(query)
    if slot is None:
        return False
    started=perf_counter()
    deadline=min(deadline_at,started+LOCAL_CAP_SECONDS) if deadline_at else started+LOCAL_CAP_SECONDS
    try:
        with catalog.connect(root,deadline=deadline) as conn:
            found=catalog.lookup(conn,scope,slot)
            return (bool(found) and found[0]==report.get('local_evidence',{}).get('contract_id')
                    and perf_counter()<deadline)
    except (OSError,ValueError,TypeError,KeyError,sqlite3.Error):
        return False
