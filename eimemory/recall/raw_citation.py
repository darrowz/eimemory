"""Consumer-only delivery of the frozen RAW gate; no retrieval/ranking policy.

The kernel proof stays immutable. Consumer admission and presentation are
separate, subtractive decisions, and are never represented as kernel proof.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
import json
import os
from time import perf_counter

from eimemory.contracts.recall_boundary import authority_digest, bounded_deadline, exact_ref, scope_tuple
from eimemory.models.records import RecallBundle, ScopeRef
from eimemory.recall.query_clean import clean_user_query

RAW_SCOPE_BASIS = "frozen_raw_request_scope.v1"
PROACTIVE_DELIVERY_MODE = "hermes_proactive_raw_sidecar.v1"


def _support_unknown(reason, *, reviewed=False):
    return {"status": "unknown", "reason": reason, "reviewed": reviewed,
            "scope": "presented_candidate_set", "calls": 0, "selected": []}


def support_request_budget(operation):
    """One service request owns one verifier slot and an actual delivery cap."""
    @wraps(operation)
    def bounded(*args, **kwargs):
        if os.environ.get("EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED", "0") != "1":
            return operation(*args, **kwargs)
        from eimemory.retrieval.verification_budget import verification_budget_scope
        from eimemory.retrieval.caller_assistance import host_delivery_deadline, _HOST_DEADLINE
        from eimemory.core.budgets import explicit_recall_timeout_seconds
        hard = perf_counter() + explicit_recall_timeout_seconds()
        if operation.__name__ == "proactive_prefetch":
            from eimemory.retrieval.proactive import ProactiveRecallService
            host = ProactiveRecallService._host_deadline(str(kwargs.get("channel") or ""))
            if host:
                hard = min(hard, host)
        if _HOST_DEADLINE.get():
            hard = min(hard, _HOST_DEADLINE.get())
        with verification_budget_scope(), host_delivery_deadline(hard):
            return operation(*args, **kwargs)
    return bounded


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def pin_deadline(context):
    from eimemory.core.budgets import recall_budget_seconds
    from eimemory.retrieval.caller_assistance import _HOST_DEADLINE
    now = perf_counter()
    deadline = min(bounded_deadline(context.get("_recall_deadline_monotonic"), started=now),
                   now + recall_budget_seconds())
    host = _HOST_DEADLINE.get()
    return min(deadline, host) if host > 0 else deadline


@dataclass
class RawCitationLane:
    # Only collect_citation constructs this from actual kernel output. Caller
    # task_context/JSON receipts never enter this object or serialization path.
    decision: object = None
    evidence: list = field(default_factory=list)
    admission: dict = field(default_factory=lambda: {"status": "unavailable", "reason": "not_collected"})
    request: dict = field(default_factory=dict)
    presentation: dict = field(default_factory=lambda: {"status": "not_rendered"})
    answer_support: dict = field(default_factory=lambda: _support_unknown(
        "not_reviewed" if os.environ.get("EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED", "0") == "1"
        else "feature_disabled"))
    # Related retrieval and model-selected factual support have distinct
    # contracts. A negative support review must not erase related material.
    support_evidence: list = field(default_factory=list)

    def withhold(self, reason):
        support = {**self.answer_support, "status": "unknown", "reason": reason, "selected": []}
        return RawCitationLane(self.decision, [], {"status": "unavailable", "reason": reason},
                               dict(self.request), answer_support=support)

    def to_dict(self):
        from eimemory.raw.citation_gate import CitationDecision
        proof = None
        if isinstance(self.decision, CitationDecision):
            original = self.decision.to_dict()
            proof = {key: original[key] for key in (
                "status", "reason", "query_digest", "scope_digest", "snapshot_digest", "complete",
                "corpus_records", "kinds", "policy_version", "tokenizer_version")}
            # Bounded commitments, not a dump of every owner/source/query term.
            for key in ("owner_scopes", "source_ids", "candidate_source_digests"):
                proof[key + "_digest"] = digest(original[key])
        return {"status": self.admission["status"], "decision": proof,
                "evidence": list(self.evidence), "admission": dict(self.admission),
                "request": dict(self.request), "presentation": dict(self.presentation),
                "answer_support": dict(self.answer_support),
                "support_evidence": list(self.support_evidence)}


def _owners(scope, context):
    """The existing RAW request domain, independent of L1's broader aliases.

    No caller-provided owner list or L1 alias list grants RAW authority.
    An explicit full-engine-union consumer separately checks exact equality.
    """
    from eimemory.identity import hongtu_query_scopes
    from eimemory.raw.boundary import _CURRENT
    outer = _CURRENT.get()
    def intersect(owners):
        return owners & {outer.exact_scope} if outer and outer.exact_scope is not None else owners
    exact = (context.get("exact_scope_only") is True
             or str(context.get("scope_strategy") or "").strip().lower() == "exact")
    if exact:
        return intersect({scope_tuple(scope)}), True

    def visible(scopes):
        owners = set()
        for owner in scopes:
            owners.add(scope_tuple(owner))
            if owner.user_id:
                owners.add((*scope_tuple(owner)[:3], ""))
        return owners

    raw = intersect(visible(hongtu_query_scopes(scope)))
    return raw, True


def _unbound_chat_raw(record):
    """RAW transcripts have no certified query/time-specific L1 value span.

    Do not infer time or entailment from text. Preserve them for manual RAW
    exploration; only the ordinary resolver may deliver their managed facts.
    """
    from eimemory.knowledge.chat_temporal import CHANNELS, KEY
    from eimemory.metadata import business_metadata
    meta = business_metadata(record.meta)
    return (KEY in meta
            or record.source in {channel + suffix for channel in CHANNELS for suffix in (".memory", ".l1")}
            or meta.get("capture_origin") in {"l1_extract", "turn_sync", "same_turn_context"}
            or bool(meta.get("source_event_binding"))
            or str(record.content.get("source_type") or "").lower() in {"conversation", "chat", "turn"}
            or meta.get("runtime_channel") in CHANNELS and bool(
                record.content.get("turn_id") or record.content.get("source_event_id")))


def collect_citation(memory, *, query, scope, context, limit=8, authorized_scopes=None):
    """One query-wide call, then canonical/current/temporal consumer admission.

    authorized_scopes, when supplied by the engine, is a subtractive equality
    check against its already-computed set, never an authority input to RAW.
    """
    from eimemory.raw.boundary import guarded_citation_search
    from eimemory.raw.retrieval import authoritative_raw_payload, _record_text
    from eimemory.retrieval.contracts import CandidateRequest
    from eimemory.storage.recall_deadline import recall_read_scope

    query = clean_user_query(str(query or "").strip())
    scope = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    context = dict(context)
    # Reuse the engine's existing helper, including when its private context
    # never reaches a normal/proactive caller or it returns early. Caller DTO
    # internal flags cannot erase or invent the structured task boundary.
    from eimemory.recall.task_queries import task_recall_mode
    context["_task_recall_mode"] = task_recall_mode(query)
    deadline = pin_deadline(context)
    context.update(evidence_intent="fact_citation", _recall_deadline_monotonic=deadline,
                   rerank_with_llm=False, enable_llm_rerank=False)
    lane = RawCitationLane(request={"query_digest": digest(query), "scope_digest": digest(asdict(scope)),
        "source_ids_digest": digest(context.get("source_ids")), "kinds_digest": digest(context.get("kinds")),
        "intent": "fact_citation", "task_mode": context["_task_recall_mode"],
        "scope_basis": RAW_SCOPE_BASIS if authorized_scopes is None else "requested_engine_union.v1"})
    owners, equivalent = _owners(scope, context)
    if not equivalent or authorized_scopes is not None and owners != set(authorized_scopes):
        return lane.withhold("authority_composition_required")
    if context.get("_task_recall_mode"):
        return lane.withhold("task_evidence_requires_structured_admission")
    if context.get("target_source_id"):
        return lane.withhold("unsupported_authority_filter")
    try:
        rows = guarded_citation_search(memory.store, query=query, scope=scope, task_context=context,
            source_ids=context.get("source_ids"), limit=max(1, min(8, int(limit))))
        lane.decision = rows.decision
        lane.admission = {"status": rows.decision.status, "reason": rows.decision.reason}
        if rows.decision.status != "related":
            return lane
        if set(rows.decision.owner_scopes) != owners:
            return lane.withhold("authority_composition_required")
        from eimemory.retrieval.caller_assistance import required as verification_required
        if getattr(memory.recall_engine, "relevance_admission", None) is not None or verification_required():
            return lane.withhold("structured_admission_required")
        bindings = dict(rows.decision.candidate_source_digests)
        records = []
        with recall_read_scope(memory.store, {"_recall_collection_deadline_monotonic": deadline}):
            for row in rows:
                payload = row["record"]
                ref = exact_ref(payload)
                record = memory.store.get_by_exact_ref(ref[0], scope=ScopeRef.from_dict(payload["scope"]),
                                                       source_id=ref[-1], fail_on_unavailable=True)
                if (record is None or ref not in bindings or exact_ref(record) != ref
                        or authoritative_raw_payload(record) != payload
                        or sha256(_record_text(record).encode()).hexdigest() != bindings[ref]):
                    return lane.withhold("source_binding_changed")
                records.append(record)
        # A RAW transcript cannot borrow the time-bound proof of its derived
        # L1. Withhold this lane, leaving independently admitted L1 untouched.
        if any(_unbound_chat_raw(record) for record in records):
            return lane.withhold("temporal_raw_unbound")
        request = CandidateRequest.create(query=query, scope=scope, source_ids=context.get("source_ids"),
                                           task_context=context, limit=max(1, min(8, int(limit))))
        from eimemory.knowledge.chat_temporal import resolve_candidates, exclude_managed_candidates
        from eimemory.identity_governance import filter_current_records
        admitted = (resolve_candidates(memory, request, records, expand=False)
                    if memory._ordinary_chat_temporal else exclude_managed_candidates(memory, request, records))
        admitted, _ = filter_current_records(memory.store, admitted, scope=scope, deadline_at=deadline)
        checked = memory._final_current_evidence_admission(
            RecallBundle(items=admitted, rules=[], reflections=[], confidence=0.0, next_action_hint=""),
            deadline_at=deadline)
        if len(checked.items) != len(records):
            return lane.withhold("current_evidence_unavailable")
        if perf_counter() >= deadline:
            return lane.withhold("deadline")
        from eimemory.raw.applicability import assess_applicability
        withheld = []
        for record in checked.items:
            applicability = assess_applicability(record, query=query, task_context=context)
            partial_range = (applicability.status == "unknown"
                             and applicability.reason == "version_range_not_covered"
                             and bool(applicability.spans))
            if applicability.status != "applicable" and not partial_range:
                withheld.append(applicability.reason)
                continue
            text = _record_text(record)
            spans = applicability.spans or ((0, len(text)),)
            # A version comparison may be useful in search, but a quotation
            # must not append another release's assertion to the matched span.
            start, end = spans[0]
            end = min(end, start + 1200)
            excerpt = text[start:end]
            entry = {"ref": list(exact_ref(record)), "authority_digest": authority_digest(record),
                "source_digest": bindings[exact_ref(record)],
                "excerpt_digest": sha256(excerpt.encode()).hexdigest(),
                "span": [start, end], "text": excerpt}
            if applicability.required:
                entry['applicability'] = asdict(applicability)
            lane.evidence.append(entry)
        if withheld and not lane.evidence:
            return lane.withhold("applicability_unavailable")
        if os.environ.get("EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED", "0") == "1":
            return _review_support(memory, lane, records=checked.items, query=query,
                                   scope=scope, context=context, collection_deadline=deadline,
                                   requested_limit=max(1, min(8, int(limit))))
        return lane
    except Exception:
        # No business-wide failure or fabricated kernel receipt on consumer errors.
        return lane.withhold("deadline" if perf_counter() >= deadline else "consumer_admission_unavailable")


def _same_request_snapshot(store, *, scope, context, decision, deadline):
    """Read the original RAW authority domain again without ranking or expansion."""
    from eimemory.raw.boundary import raw_request_boundary
    from eimemory.raw.citation_gate import _authority, _snapshot, _Budget
    context = {**context, "_recall_deadline_monotonic": deadline,
               "source_ids": decision.source_ids, "kinds": decision.kinds}
    receipt = {}
    @raw_request_boundary
    def read(bound_store, **kwargs):
        authority = _authority(scope, context)
        snapshot, _ = _snapshot(bound_store, authority, _Budget(authority.deadline))
        receipt["snapshot"] = snapshot
        return []
    read(store, query="", scope=scope, task_context=context, source_ids=decision.source_ids)
    snapshot = receipt.get("snapshot")
    return bool(snapshot and snapshot.digest == decision.snapshot_digest
                and snapshot.scope_digest == decision.scope_digest)


def _support_source_title(record):
    """Canonical identity context only; never caller DTOs or answer evidence."""
    from eimemory.raw.answer_support import MAX_SOURCE_TITLE_CHARS
    title = getattr(record, "title", "")
    return title if isinstance(title, str) and len(title) <= MAX_SOURCE_TITLE_CHARS else ""


def _review_support(memory, lane, *, records, query, scope, context, collection_deadline, requested_limit=8):
    from eimemory.raw.answer_support import (SupportPassage, assess_answer_support,
        MAX_CANDIDATES, MAX_PARENT_CHARS, MAX_TOTAL_CHARS, MAX_WINDOWS)
    from eimemory.raw.retrieval import _record_text
    from eimemory.raw.applicability import assess_applicability
    from eimemory.llm.command_client import bind_verifier_route, reset_verifier_route
    from eimemory.retrieval.caller_assistance import route_for_request, _HOST_DEADLINE
    from eimemory.retrieval.verification_budget import verification_budget_scope, final_authority_deadline
    from eimemory.storage.recall_deadline import recall_read_scope
    from eimemory.identity_governance import filter_current_records
    originals = {exact_ref(record): record for record in records}
    source_bindings = dict(lane.decision.candidate_source_digests)
    rows = {tuple(row["ref"]): row for row in lane.evidence}
    eligible, unresolved = [], False
    for ref, row in rows.items():
        if (row.get("applicability") or {}).get("status") == "unknown":
            unresolved = True
            continue
        record = originals[ref]
        spans = tuple(tuple(span) for span in (row.get("applicability") or {}).get("spans", ()))
        eligible.append(SupportPassage(ref, _record_text(record), authority_digest(record),
                                       row["source_digest"], spans, _support_source_title(record)))
    # Candidate recovery is a separate model-input lane. It cannot expand
    # ordinary related results or borrow the lexical gate's proof for a new ref.
    if 0 < len(eligible) < requested_limit and perf_counter() < collection_deadline:
        from eimemory.raw.support_candidates import recover_support_candidates, SupportRecoveryChanged
        try:
            extra = recover_support_candidates(memory.store, query=query, scope=scope, context=context,
                decision=lane.decision, excluded=set(originals), maximum=MAX_CANDIDATES-len(eligible), deadline=collection_deadline)
            extra = [record for record in extra if not _unbound_chat_raw(record)]
            extra, _ = filter_current_records(memory.store, extra, scope=scope, deadline_at=collection_deadline)
            extra = memory._final_current_evidence_admission(
                RecallBundle(items=extra, rules=[], reflections=[], confidence=0.0, next_action_hint=""),
                deadline_at=collection_deadline).items
            total_chars = sum(len(p.text) for p in eligible)
            total_windows = sum(len(p.allowed_spans) or 1 for p in eligible)
            for record in extra:
                applicability = assess_applicability(record, query=query, task_context=context)
                if applicability.status != "applicable":
                    continue
                ref = exact_ref(record); text = _record_text(record)
                windows = len(applicability.spans) or 1
                if (len(text) > MAX_PARENT_CHARS or total_chars + len(text) > MAX_TOTAL_CHARS
                        or total_windows + windows > MAX_WINDOWS):
                    continue  # Optional candidates must not invalidate a valid original packet.
                total_chars += len(text)
                total_windows += windows
                originals[ref] = record; source_bindings[ref] = sha256(text.encode()).hexdigest()
                eligible.append(SupportPassage(ref, text, authority_digest(record), source_bindings[ref],
                                               applicability.spans, _support_source_title(record)))
        except SupportRecoveryChanged:
            return lane.withhold("support_recovery_source_changed")
        except Exception:
            # Optional recovery has no authority to invalidate or replace the
            # original candidate receipt; the later snapshot check still runs.
            pass
    if not eligible:
        lane.answer_support = _support_unknown("applicability_unresolved" if unresolved else "no_candidates")
        return lane
    if perf_counter() >= collection_deadline:
        # Recovery may have waited for a concurrent write. An expired budget
        # cannot establish that the previously collected related text is current.
        return lane.withhold("collection_deadline_exhausted")
    # Route comes from this exact server-owned channel scope, never task DTOs.
    token = bind_verifier_route(route_for_request(context.get("runtime_channel"), scope=scope))
    try:
        with verification_budget_scope():
            decision = assess_answer_support(query=query, passages=eligible,
                scope_digest=lane.decision.scope_digest, snapshot_digest=lane.decision.snapshot_digest,
                deadline_at=_HOST_DEADLINE.get())
            lane.answer_support = asdict(decision)
            deadline = final_authority_deadline(collection_deadline)
            host_deadline = _HOST_DEADLINE.get()
            if (decision.status == "unknown" and decision.calls > 0
                    and host_deadline > perf_counter()):
                # A failed verifier cannot certify support, but a fresh read
                # can still authorize related material within the existing
                # host cap. This grants no second model call or retrieval.
                from eimemory.core.budgets import FINAL_AUTHORITY_SECONDS
                deadline = max(deadline, perf_counter() + FINAL_AUTHORITY_SECONDS)
            if host_deadline:
                deadline = min(deadline, host_deadline)
            # Even a failed/uncertain model call must not deliver source bytes
            # that changed during inference. Preserve related only if still current.
            if perf_counter() >= deadline:
                return lane.withhold("support_revalidation_deadline")
            if not _same_request_snapshot(memory.store, scope=scope, context=context,
                                          decision=lane.decision, deadline=deadline):
                return lane.withhold("support_snapshot_changed")
            fresh = {}
            with recall_read_scope(memory.store, {"_recall_collection_deadline_monotonic": deadline}):
                for ref, before in originals.items():
                    record = memory.store.get_by_exact_ref(ref[0], scope=before.scope,
                                                          source_id=ref[-1], fail_on_unavailable=True)
                    if (record is None or authority_digest(record) != authority_digest(before)
                            or sha256(_record_text(record).encode()).hexdigest() !=
                            source_bindings.get(ref)):
                        return lane.withhold("support_source_changed")
                    fresh[ref] = record
            admitted, _ = filter_current_records(memory.store, list(fresh.values()), scope=scope,
                                                 deadline_at=deadline)
            current = memory._final_current_evidence_admission(
                RecallBundle(items=admitted, rules=[], reflections=[], confidence=0.0, next_action_hint=""),
                deadline_at=deadline)
            if {exact_ref(record) for record in current.items} != set(fresh):
                return lane.withhold("support_current_evidence_changed")
            applicability = {ref: assess_applicability(record, query=query, task_context=context)
                             for ref, record in fresh.items()}
            if any(applicability[ref].status == "not_applicable" for ref in rows):
                return lane.withhold("support_applicability_changed")
            if any(applicability[p.ref].status != "applicable" for p in eligible):
                lane.answer_support.update(status="unknown", reason="support_applicability_changed", selected=[])
                return lane
            # Negative reviews also depend on every presented candidate being
            # current. Bind the whole reviewed set's expiry, including extras
            # that were not selected into either delivery section.
            cutoffs = [applicability[p.ref].valid_until for p in eligible if applicability[p.ref].valid_until]
            if cutoffs:
                lane.answer_support["valid_until"] = min(cutoffs)
            if perf_counter() >= deadline:
                return lane.withhold("support_revalidation_deadline")
            if decision.status == "unsupported":
                if unresolved:
                    lane.answer_support.update(status="unknown", reason="applicability_unresolved", selected=[])
                return lane
            if decision.status != "supported":
                return lane
            selected = []
            for proof in decision.selected:
                record, current = fresh.get(proof.ref), applicability.get(proof.ref)
                if (record is None or current.status != "applicable"
                        or authority_digest(record) != proof.authority_digest):
                    return lane.withhold("support_selected_source_changed")
                text = _record_text(record)
                allowed = current.spans or ((0, len(text)),)
                start, end = proof.span_start, proof.span_end
                if (not any(left <= start < end <= right for left, right in allowed)
                        or sha256(text.encode()).hexdigest() != proof.source_digest
                        or sha256(text[start:end].encode()).hexdigest() != proof.quote_digest):
                    return lane.withhold("support_selected_span_changed")
                entry = {"ref": list(proof.ref), "authority_digest": proof.authority_digest,
                    "source_digest": proof.source_digest, "excerpt_digest": proof.quote_digest,
                    "span": [start, end], "text": text[start:end]}
                title = _support_source_title(record)
                if title:
                    entry["source_title"] = title
                if current.required:
                    entry["applicability"] = asdict(current)
                selected.append(entry)
            lane.support_evidence = selected
            return lane
    except Exception:
        return lane.withhold("support_revalidation_unavailable")
    finally:
        reset_verifier_route(token)


def current_proactive_items(memory, items, *, query, scope, sources, deadline):
    """Subtract stale cached L1 before proactive rendering/receipt binding.

    No fresh item, text or citation is added to an existing decision.
    """
    from eimemory.knowledge.chat_temporal import resolve_candidates, exclude_managed_candidates
    from eimemory.knowledge.evidence_contracts import versioned_record_ref
    from eimemory.retrieval.contracts import CandidateRequest
    from eimemory.storage.recall_deadline import recall_read_scope
    try:
        with recall_read_scope(memory.store, {"_recall_collection_deadline_monotonic": deadline}):
            records = [memory.store.get_by_exact_ref(item["record_id"], scope=scope,
                       source_id=item["source_id"], fail_on_unavailable=True) for item in items]
        records = [record for item, record in zip(items, records) if record is not None
                   and (item.get("render_evidence") or {}).get("effect_record_ref") == versioned_record_ref(record)]
        request = CandidateRequest.create(query=clean_user_query(query), scope=ScopeRef.from_dict(scope),
            source_ids=sources, task_context={"exact_scope_only": True, "_recall_deadline_monotonic": deadline})
        records = (resolve_candidates(memory, request, records, expand=False)
                   if memory._ordinary_chat_temporal else exclude_managed_candidates(memory, request, records))
        checked = memory._final_current_evidence_admission(
            RecallBundle(items=records, rules=[], reflections=[], confidence=0, next_action_hint=""),
            deadline_at=deadline)
        refs = {(r.record_id, r.source_id) for r in checked.items}
        return [item for item in items if (item["record_id"], item["source_id"]) in refs]
    except Exception:
        return []


def attach_compact_lane(payload, lane, *, maximum_bytes):
    """Spend remaining bytes only; never refit or truncate the existing L1."""
    if not isinstance(lane, RawCitationLane):
        return payload
    public = lane.to_dict()
    related = list(public["evidence"])
    def fits():
        return len(json.dumps({**payload, "raw_citation": public}, ensure_ascii=False,
                              separators=(",", ":")).encode()) <= maximum_bytes - 192
    while public["evidence"] and not fits():
        public["evidence"].pop()
    while public["support_evidence"] and not fits():
        public["support_evidence"].pop()
    if (public.get("answer_support") or {}).get("status") == "supported" and not _complete_support_delivery(public):
        _drop_support(public, "support_context_incomplete")
        # If a complete support set cannot fit, reuse its released space for
        # the ordinary related prefix instead of leaving an avoidable blank.
        public["evidence"] = related
        while public["evidence"] and not fits():
            public["evidence"].pop()
    if not fits():
        # The full proof remains available in to_dict. Delivery must say that
        # compact transport could not carry it, rather than inventing a proof.
        public = {"status": "unavailable", "decision": None, "evidence": [], "support_evidence": [],
                  "admission": {"status": "unavailable", "reason": "compact_budget"},
                  "request": {"scope_basis": lane.request.get("scope_basis", RAW_SCOPE_BASIS)},
                  "answer_support": _support_unknown("compact_budget"),
                  "presentation": {"status": "not_delivered", "reason": "compact_budget"}}
    if fits():
        return {**payload, "raw_citation": public}
    lane.presentation = {"status": "not_delivered", "reason": "compact_budget"}
    # Runtime returns this presentation separately outside the compact bundle.
    return payload


def render_citation(base, lane, *, max_chars):
    """Append a bounded data fence; hashes/authority metadata stay off prompt."""
    from eimemory.core.untrusted import safe_untrusted_json, wrap_untrusted_block
    if not isinstance(lane, dict):
        return base
    status = (lane.get("admission") or lane.get("decision") or {}).get("status", "unavailable")
    support = lane.get("answer_support") or _support_unknown("not_reviewed")
    lane["answer_support"] = support
    if not _unexpired_support_review(lane):
        _drop_support(lane, "support_validity_expired")
    supported = support.get("status") == "supported" and _complete_support_delivery(lane)
    if support.get("status") == "supported" and not supported:
        _drop_support(lane, "support_context_incomplete")
    if not _unexpired_raw_evidence(lane):
        status = "unavailable"
        _drop_support(lane, "support_validity_expired")
        supported = False
    warning = {"related": "RAW citation: related material only; entailment is not established.",
               "reject": "RAW citation: withheld within this RAW retrieval scope only.",
               "unavailable": "RAW citation check unavailable for this RAW retrieval scope."}.get(status)
    warning = warning or "RAW citation check unavailable for this RAW retrieval scope."
    if support.get("status") == "unsupported":
        warning += (" Answer support: the reviewed candidate set does not support answering this query. "
                    "This is not a claim that no answer exists elsewhere in memory. "
                    "The related material below must not be presented as a supported answer.")
    elif supported and status == "related":
        warning = ("RAW answer support: only the support material section contains complete source windows "
                   "selected by a model review for this query. This is a model judgment, not a proof of truth. "
                   "The separate related material section does not inherit that support judgment.")
        if any(row.get("source_title") for row in lane.get("support_evidence") or []):
            warning += " Source titles identify documents but cannot supply facts absent from their excerpts."
    elif support.get("reason") != "feature_disabled":
        warning += " Answer support is unknown; related material is not a confirmed answer."
    basis = (lane.get("request") or {}).get("scope_basis")
    warning += (" Scope basis: frozen RAW request scope, not the broader L1 alias union."
                if basis == RAW_SCOPE_BASIS else " Scope basis: requested engine union; no smaller scope is substituted.")
    warning += " Independently authorized L1 and other memory remain usable."
    room = max(0, int(max_chars) - len(base) - (1 if base else 0))
    block = wrap_untrusted_block(warning, tag="eimemory_raw_context")
    if len(block) > room:
        if supported:
            _drop_support(lane, "support_context_incomplete")
            return render_citation(base, lane, max_chars=max_chars)
        lane["presentation"] = {"status": "not_delivered", "reason": "context_budget", "evidence_count": 0,
                                "support_evidence_count": 0}
        return base
    body, count, support_count = warning, 0, 0
    if status == "related":
        sections = []
        if supported:
            sections.append(("support_material", "Model-reviewed support material:", lane.get("support_evidence") or []))
        sections.append(("related_material", "Related material only; not a confirmed answer:", lane.get("evidence") or []))
        for role, heading, rows in sections:
            section_body = body + "\n" + heading
            section_count = 0
            for row in rows:
                # Role is runtime-owned; escaping prevents source instructions
                # or delimiters from promoting related material to support.
                material = {"record_id": row["ref"][0], "source_id": row["ref"][-1],
                            "excerpt": row["text"], "evidence_role": role}
                if role == "support_material" and row.get("source_title"):
                    material["source_title"] = row["source_title"]
                if (row.get("applicability") or {}).get("status") == "unknown":
                    material["limitation"] = ("Related material for the version stated in this excerpt only; "
                        "applicability to the requested version range is not established. "
                        "Do not use it as a confirmed answer for the requested range.")
                line = safe_untrusted_json(material)
                candidate = wrap_untrusted_block(section_body + "\n" + line, tag="eimemory_raw_context")
                if len(candidate) > room:
                    break
                section_body, block = section_body + "\n" + line, candidate
                section_count += 1
            if section_count:
                body = section_body
            if role == "support_material":
                support_count = section_count
            else:
                count = section_count
    lane["presentation"] = {"status": "delivered", "evidence_count": count,
                            "support_evidence_count": support_count,
                            "context_digest": digest(block)}
    if supported and support_count != len(lane.get("support_evidence") or []):
        # A whole-query verdict may depend on every selected passage. Do not
        # render a truncated subset under the model-reviewed support heading.
        _drop_support(lane, "support_context_incomplete")
        return render_citation(base, lane, max_chars=max_chars)
    return base + ("\n" if base else "") + block


def _drop_support(lane, reason):
    lane["answer_support"].update(status="unknown", reason=reason, selected=[])
    lane["support_evidence"] = []


def _complete_support_delivery(lane):
    support = lane.get("answer_support") or {}
    proofs, evidence = support.get("selected") or [], lane.get("support_evidence") or []
    if not proofs or len(proofs) != len(evidence):
        return False
    def proof_key(proof):
        return (tuple(proof.get("ref") or ()), proof.get("span_start"), proof.get("span_end"),
                proof.get("quote_digest"), proof.get("authority_digest"), proof.get("source_digest"),
                proof.get("source_title_digest", ""))
    def row_key(row):
        span = row.get("span") or []
        if len(span) != 2 or sha256(str(row.get("text") or "").encode()).hexdigest() != row.get("excerpt_digest"):
            return None
        return (tuple(row.get("ref") or ()), *span, row.get("excerpt_digest"),
                row.get("authority_digest"), row.get("source_digest"),
                sha256(str(row['source_title']).encode("utf-8")).hexdigest() if row.get("source_title") else "")
    return sorted(map(proof_key, proofs)) == sorted(row_key(row) for row in evidence if row_key(row) is not None)


def _unexpired_raw_evidence(lane):
    """A prepared receipt cannot extend a source's wall-clock validity.

    On the host this metadata is checked only after the authenticated sidecar
    byte binding. Historical as-of requests carry no wall-clock expiry.
    """
    now = datetime.now(timezone.utc)
    for evidence in [*(lane.get("evidence") or []), *(lane.get("support_evidence") or [])]:
        applicability = evidence.get("applicability") or {}
        until = applicability.get("valid_until")
        if until is None:
            continue
        try:
            if not isinstance(until, str) or len(until) > 64:
                return False
            expiry = datetime.fromisoformat(until.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.utcoffset() is None or now >= expiry:
                return False
        except (ValueError, TypeError):
            return False
    return True


def _unexpired_support_review(lane):
    support = lane.get("answer_support") or {}
    if support.get("status") not in {"supported", "unsupported"}:
        return True
    return _unexpired_raw_evidence({"evidence": [{"applicability": {
        "valid_until": support.get("valid_until")}}]})


def proactive_attempt_identity(*, query, scope, sources, session_id, turn_id, task_type):
    """Request values supplied by the host/service, never by a returned hash."""
    from eimemory.models.source_partitions import normalize_source_ids
    from eimemory.recall.task_queries import task_recall_mode
    return {"channel": "hermes", "session_id": session_id, "turn_id": turn_id,
            "host_query_digest": digest(str(query).strip()), "scope_digest": digest(scope),
            "source_ids_digest": digest(normalize_source_ids(sources)),
            "task_type": str(task_type or "proactive.recall").strip(),
            "task_mode": task_recall_mode(clean_user_query(str(query).strip())),
            "delivery_mode": PROACTIVE_DELIVERY_MODE, "scope_basis": RAW_SCOPE_BASIS}


def issue_proactive_authority(runtime, payload, *, query, scope, sources, session_id, turn_id,
                              task_type, deadline):
    """Read the committed decision through the existing trusted service boundary.

    Digests bind presentation bytes; they do not authorize themselves. Native
    ledger fields and the current request authorize this response. The original
    context commitment is insert-only decision diagnostics, never reconstructed
    from a retry's subtractively rendered text. No storage/kernel change.
    """
    from eimemory.retrieval.proactive import ProactiveRecallService, _bounded_text, _MAX_QUERY_CHARS
    from eimemory.storage.recall_deadline import recall_read_scope
    expected = proactive_attempt_identity(query=query, scope=scope, sources=sources,
        session_id=session_id, turn_id=turn_id, task_type=task_type)
    try:
        with recall_read_scope(runtime.store, {"_recall_collection_deadline_monotonic": deadline}):
            stored = runtime.store.load_proactive_decision(payload["decision_id"])
        native = {"channel": "hermes", "scope": scope, "source_ids": sources,
                  "source_key": ProactiveRecallService._source_digest(tuple(sources)),
                  "session_id": session_id, "turn_id": turn_id, "query_id": turn_id,
                  "query_digest": sha256(_bounded_text(query, _MAX_QUERY_CHARS).encode(
                      "utf-8", errors="replace")).hexdigest(),
                  "task_type": expected["task_type"], "policy_version": payload.get("policy_version"),
                  "release_identity": payload.get("release_identity"),
                  "release_bound": True, "control_cohort": False}
        if (not stored or any(stored.get(key) != value for key, value in native.items())
                or payload.get("query_id") != turn_id):
            return None, "proactive_request_binding_unavailable"
        stored_refs = {(item["citation"], item["record_id"], item["source_id"])
                       for item in stored.get("items") or []}
        if any((item["citation"], item["record_id"], item["source_id"]) not in stored_refs
               for item in payload.get("items") or []):
            return None, "proactive_projection_unavailable"
        origin = (stored.get("retrieval_diagnostics") or {}).get("original_context_commitment")
        valid_origin = (isinstance(origin, dict) and origin.get("schema") == "proactive_context_origin.v1"
            and origin.get("host_query_digest") == expected["host_query_digest"]
            and origin.get("task_mode") == expected["task_mode"]
            and origin.get("delivery_mode") == PROACTIVE_DELIVERY_MODE
            and isinstance(origin.get("context_digest"), str) and len(origin["context_digest"]) == 64)
        authority = {"schema": "proactive_raw_delivery_authority.v1", **expected,
            "decision_id": stored["decision_id"], "policy_version": stored["policy_version"],
            "release_identity": dict(stored["release_identity"]),
            "original_context_digest": origin["context_digest"] if valid_origin else None,
            "projection_context_digest": digest(payload.get("context") or ""),
            "projection": "subtractive_original_items", "acknowledgement": "separate_unacknowledged_raw_lane"}
        return authority, "" if valid_origin else "original_context_commitment_unavailable"
    except Exception:
        return None, "proactive_request_binding_unavailable"


def sidecar_binding(payload, *, authority, context):
    """Byte binding to separately service-issued authority, never a signature."""
    return {**authority, "lane_digest": digest((payload.get("bundle") or {}).get("raw_citation")),
            "context_digest": digest(context)}


def consume_sidecar(payload, *, query, scope, sources, session_id, turn_id, task_type,
                    max_chars, expected_authority=None):
    base = str(payload.get("context") or "")
    extra = payload.get("raw_citation_context")
    if (payload.get("ok") is not True or payload.get("bypassed") or payload.get("control_cohort")
            or not payload.get("release_bound") or not payload.get("decision_id") or not payload.get("items")
            or not isinstance(extra, str) or not extra):
        return base
    # This object is issued by the authenticated RPC service after native
    # decision lookup. User/source DTOs cannot submit it to that issuer. The
    # pending request supplies the expected attempt identity independently.
    authority = payload.get("raw_citation_authority")
    expected = proactive_attempt_identity(query=query, scope=scope, sources=sources,
        session_id=session_id, turn_id=turn_id, task_type=task_type)
    if (not isinstance(authority, dict) or authority.get("schema") != "proactive_raw_delivery_authority.v1"
            or any(authority.get(key) != value for key, value in expected.items())
            or payload.get("query_id") != turn_id
            or authority.get("decision_id") != payload.get("decision_id")
            or authority.get("policy_version") != payload.get("policy_version")
            or authority.get("release_identity") != payload.get("release_identity")
            or authority.get("projection_context_digest") != digest(base)):
        return base
    # A fresh metadata RPC, bound to the host's pending request, is mandatory
    # even on first delivery. A delivery response cannot authorize itself or
    # become a trust-on-first-use policy/release expectation.
    if (not isinstance(expected_authority, dict)
            or expected_authority.get("schema") != "proactive_raw_delivery_expectation.v1"
            or any(expected_authority.get(key) != value for key, value in expected.items())
            or any(authority.get(key) != expected_authority.get(key)
                   for key in ("decision_id", "policy_version", "release_identity", "original_context_digest"))):
        return base
    lane = (payload.get("bundle") or {}).get("raw_citation") or {}
    if authority.get("original_context_digest") is None and (
            lane.get("evidence") or lane.get("support_evidence")
            or (lane.get("admission") or {}).get("status") != "unavailable"):
        return base  # legacy decisions can carry only an honest unavailable warning
    if payload.get("raw_citation_binding") != sidecar_binding(payload, authority=authority, context=extra):
        return base
    reviewed_outcome = (lane.get("answer_support") or {}).get("status") in {"supported", "unsupported"}
    if (lane.get("evidence") or lane.get("support_evidence") or reviewed_outcome) and (not expected_authority.get("raw_snapshot_digest")
            or expected_authority["raw_snapshot_digest"] != (lane.get("decision") or {}).get("snapshot_digest")):
        return base
    if not _unexpired_support_review(lane):
        # The authenticated related source bytes are still current; regenerate
        # only the RAW addition with an unknown review instead of replaying a
        # stale negative or positive verdict. Preserve the independent L1 base.
        from copy import deepcopy
        return render_citation(base, deepcopy(lane), max_chars=max_chars)
    if not _unexpired_raw_evidence(lane):
        return base
    if len(base) + len(extra) + (1 if base else 0) > max_chars:
        return base
    return base + ("\n" if base else "") + extra
