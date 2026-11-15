"""Consumer-only delivery of the frozen RAW gate; no retrieval/ranking policy.

The kernel proof stays immutable. Consumer admission and presentation are
separate, subtractive decisions, and are never represented as kernel proof.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from time import perf_counter

from eimemory.contracts.recall_boundary import authority_digest, bounded_deadline, exact_ref, scope_tuple
from eimemory.models.records import RecallBundle, ScopeRef
from eimemory.recall.query_clean import clean_user_query

RAW_SCOPE_BASIS = "frozen_raw_request_scope.v1"
PROACTIVE_DELIVERY_MODE = "hermes_proactive_raw_sidecar.v1"


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

    def withhold(self, reason):
        return RawCitationLane(self.decision, [], {"status": "unavailable", "reason": reason},
                               dict(self.request))

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
                "request": dict(self.request), "presentation": dict(self.presentation)}


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
        lane.evidence = [{"ref": list(exact_ref(record)), "authority_digest": authority_digest(record),
                          "source_digest": bindings[exact_ref(record)],
                          "excerpt_digest": sha256(_record_text(record)[:1200].encode()).hexdigest(),
                          "span": [0, len(_record_text(record)[:1200])],
                          "text": _record_text(record)[:1200]} for record in checked.items]
        return lane
    except Exception:
        # No business-wide failure or fabricated kernel receipt on consumer errors.
        return lane.withhold("deadline" if perf_counter() >= deadline else "consumer_admission_unavailable")


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
    def fits():
        return len(json.dumps({**payload, "raw_citation": public}, ensure_ascii=False,
                              separators=(",", ":")).encode()) <= maximum_bytes - 192
    while public["evidence"] and not fits():
        public["evidence"].pop()
    if not fits():
        # The full proof remains available in to_dict. Delivery must say that
        # compact transport could not carry it, rather than inventing a proof.
        public = {"status": "unavailable", "decision": None, "evidence": [],
                  "admission": {"status": "unavailable", "reason": "compact_budget"},
                  "request": {"scope_basis": lane.request.get("scope_basis", RAW_SCOPE_BASIS)},
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
    warning = {"related": "RAW citation: related material only; entailment is not established.",
               "reject": "RAW citation: withheld within this RAW retrieval scope only.",
               "unavailable": "RAW citation check unavailable for this RAW retrieval scope."}.get(status)
    warning = warning or "RAW citation check unavailable for this RAW retrieval scope."
    basis = (lane.get("request") or {}).get("scope_basis")
    warning += (" Scope basis: frozen RAW request scope, not the broader L1 alias union."
                if basis == RAW_SCOPE_BASIS else " Scope basis: requested engine union; no smaller scope is substituted.")
    warning += " Independently authorized L1 and other memory remain usable."
    room = max(0, int(max_chars) - len(base) - (1 if base else 0))
    block = wrap_untrusted_block(warning, tag="eimemory_raw_context")
    if len(block) > room:
        lane["presentation"] = {"status": "not_delivered", "reason": "context_budget", "evidence_count": 0}
        return base
    body, count = warning, 0
    if status == "related":
        for row in lane.get("evidence") or []:
            # JSON escaping prevents body/delimiter text acquiring authority.
            line = safe_untrusted_json({"record_id": row["ref"][0], "source_id": row["ref"][-1],
                                        "excerpt": row["text"]})
            candidate = wrap_untrusted_block(body + "\n" + line, tag="eimemory_raw_context")
            if len(candidate) > room:
                break
            body, block, count = body + "\n" + line, candidate, count + 1
    lane["presentation"] = {"status": "delivered", "evidence_count": count,
                            "context_digest": digest(block)}
    return base + ("\n" if base else "") + block


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
            lane.get("evidence") or (lane.get("admission") or {}).get("status") != "unavailable"):
        return base  # legacy decisions can carry only an honest unavailable warning
    if payload.get("raw_citation_binding") != sidecar_binding(payload, authority=authority, context=extra):
        return base
    if len(base) + len(extra) + (1 if base else 0) > max_chars:
        return base
    return base + ("\n" if base else "") + extra
