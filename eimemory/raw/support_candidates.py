"""Bounded CJK character candidates for model review, never lexical proof.

Ordinary RAW ranking is unchanged. A sparse related lane may miss paraphrases
that have no shared bigram; character overlap only proposes extra sources to
the same verifier call. Every source comes from the identical authorized,
complete snapshot and is rehydrated before review and again before delivery.
"""
from collections import Counter
from hashlib import sha256
from math import log
import re

from eimemory.contracts.recall_boundary import authority_digest, exact_ref
from eimemory.models.records import ScopeRef

POLICY = 'raw-support-cjk-character-candidates.v1'
_CJK = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')


class SupportRecoveryChanged(RuntimeError):
    """An observed authority/source change invalidates the original packet."""


def _rank(query, documents, check):
    terms = set(_CJK.findall(query))
    if len(terms) < 2:
        return []
    counts=[];frequency=Counter()
    for document in documents:
        check()
        count=Counter(_CJK.findall(document.text));counts.append(count)
        frequency.update(count.keys())
    total=len(documents)
    average=sum(sum(count.values()) for count in counts)/max(1,total)
    if not average:
        return []
    ranked=[]
    for doc,count in zip(documents,counts):
        check()
        overlap=terms & count.keys()
        if len(overlap)<2:
            continue
        length=sum(count.values())
        score=sum(log(1+(total-frequency[t]+.5)/(frequency[t]+.5))*count[t]
                  /(count[t]+.5+1.5*length/average) for t in overlap)
        ranked.append((score,doc))
    ranked.sort(key=lambda pair:(-pair[0],pair[1].ref))
    return [doc for _score,doc in ranked]


def recover_support_candidates(store, *, query, scope, context, decision, excluded, maximum, deadline):
    """Return optional canonical candidates; never alter the kernel receipt."""
    from .boundary import raw_request_boundary, raw_read_scope
    from .citation_gate import CitationDecision, _authority, _snapshot, _Budget
    from .retrieval import _record_text
    from .applicability import assess_applicability
    if (maximum<=0 or len(set(_CJK.findall(query)))<2
            or not isinstance(decision,CitationDecision) or decision.status!='related'):
        return []
    context={**context,'_recall_deadline_monotonic':deadline,
             'source_ids':decision.source_ids,'kinds':decision.kinds}
    result=[]
    @raw_request_boundary
    def read(bound_store,**kwargs):
        authority=_authority(scope,context);budget=_Budget(authority.deadline)
        snapshot,_=_snapshot(bound_store,authority,budget)
        if snapshot.digest!=decision.snapshot_digest or snapshot.scope_digest!=decision.scope_digest:
            raise SupportRecoveryChanged('support_recovery_snapshot_changed')
        for document in _rank(query,snapshot.documents,budget.check):
            if document.ref in excluded:
                continue
            ref=document.ref
            owner=ScopeRef(tenant_id=ref[1],agent_id=ref[2],workspace_id=ref[3],user_id=ref[4])
            with raw_read_scope(bound_store):
                record=bound_store.get_by_exact_ref(ref[0],scope=owner,source_id=ref[-1],fail_on_unavailable=True)
            if (record is None or exact_ref(record)!=ref or authority_digest(record)!=document.authority_sha256
                    or sha256(_record_text(record).encode()).hexdigest()!=document.source_sha256):
                raise SupportRecoveryChanged('support_recovery_source_changed')
            if assess_applicability(record,query=query,task_context=context).status!='applicable':
                continue
            result.append(record)
            if len(result)>=min(8,maximum):
                break
        budget.check()
        return []
    read(store,query=query,scope=scope,task_context=context,source_ids=decision.source_ids)
    return result
