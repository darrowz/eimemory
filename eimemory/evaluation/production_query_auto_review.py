"""Deterministic automated review of pending production-query recall labels.

This reuses the existing natural-query pipeline (pending capture -> signed
label evidence -> accepted case -> dataset/gate). It adds one distinct label
authority, ``auto_review``, whose labels are admitted only when independent
signals already recorded by the system agree:

* S_sem: a validated ``semantic-relevance.v1`` observation (tool-free judge,
  run by the existing delivery monitor) marks the delivered item ``relevant``;
* S_proof: the item was delivered with internally verified, record-bound answer
  evidence (``verified-parent-span.v1``) whose record digest is still current;
* S_used: the host explicitly reported the delivered item as ``used``.

A candidate becomes a relevance label with S_sem AND (S_proof OR S_used OR
a second independent blind semantic confirmation of the same delivered item).
Grade 3 still needs proof AND host usage; confirmation alone grants grade 2.
Relevance labels do not attest host usage or answer correctness. Query features are redacted terms
derived from the private original-query vault and must pass the same redaction
and signal checks as operator packets. Nothing else (rank position, retrieval
membership, task type, empty results) is evidence. Cases that do not meet the
criteria receive a non-passing conclusion with recorded reasons; transient
provider or execution failures remain explicitly incomplete and retryable;
contradicted cases are recorded as rejected. Collection records are unchanged,
and later evidence can trigger another review. Every auto label is signed,
carries the criteria version and an inputs digest, and is revocable.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import hmac
import re
from typing import Any

from eimemory.adapters.runtime.channel import SUPPORTED_RUNTIME_CHANNELS, resolve_channel_scope
from eimemory.core.clock import now_iso
from eimemory.governance.evidence_contract import same_scope
from eimemory.models.records import RecordEnvelope, ScopeRef

from .production_query_dataset import (
    ACCEPTED_SOURCE,
    PENDING_QUERY_SCHEMA,
    PENDING_SOURCE,
    accept_auto_reviewed_production_query,
    accepted_production_query_validation_error,
    pending_production_query_capture_validation_error,
    resolve_bound_logical_scope,
    _empty_decision_reason,
)
from .real_query_schema import (
    PRODUCTION_RECALL_AUTO_REVIEW_FLAG,
    _LOW_SIGNAL_QUERY_TOKENS,
    _bounded_query_features,
    _query_feature_tokens,
    _stable_digest,
    production_real_query_feature_quality_reasons,
    production_real_query_label_authority,
    production_recall_auto_review_enabled,
)

CRITERIA_VERSION = "production-recall-auto-review.v2"
RECEIPT_SCHEMA = "production_recall_auto_review.v1"
RUN_SCHEMA = "production_recall_auto_review_run.v1"
REVIEW_CONTRACT = "production_recall_review_conclusion.v1"
RECEIPT_SOURCE = "eimemory.production_recall.auto_review"
REVIEWER = "eimemory.auto_review"
QUERY_FEATURES_ORIGIN = "vault_original_query_terms.v2"
_MAX_TERMS = 8
_MAX_CJK_TERM_CHARS = 8
_STOPWORDS = frozenset({
    "a", "about", "after", "all", "also", "am", "an", "and", "any", "are", "as", "at", "be",
    "been", "before", "but", "by", "can", "could", "did", "do", "does", "for", "from", "had",
    "has", "have", "he", "her", "his", "how", "i", "if", "in", "into", "is", "it", "its", "just",
    "me", "my", "no", "not", "now", "of", "on", "or", "our", "please", "she", "should", "so",
    "some", "than", "that", "the", "their", "them", "then", "there", "these", "they", "this",
    "to", "up", "us", "was", "we", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "would", "yes", "you", "your",
})
_PENDING_REASONS_TRANSIENT = frozenset({"pending_capture_authority_unavailable"})
# Evidence that no longer exists cannot support or contradict a label.
_EVIDENCE_LOSS_REASONS = frozenset({"pending_capture_decision_missing"})


def _signed_content(content: dict[str, Any], *, required: bool = True) -> dict[str, Any]:
    """Attach key_id and an HMAC over the whole receipt content."""
    from eimemory.governance.tool_receipts import receipt_key_set

    keys = receipt_key_set()
    if keys is None:
        if required:
            raise ValueError("auto_review_attestation_key_unavailable")
        return {**content, "key_id": "", "signature": ""}
    payload = {**content, "key_id": keys.active_id}
    payload.pop("signature", None)
    signature = hmac.new(keys.active_key.encode(),
                         (RECEIPT_SCHEMA + ":" + _stable_digest(payload)).encode(), sha256).hexdigest()
    return {**payload, "signature": signature}


def auto_review_receipt_error(record: RecordEnvelope | None) -> str:
    """Verify a stored auto-review receipt (decision record) signature."""
    from eimemory.governance.tool_receipts import receipt_key_set

    if record is None or record.source != RECEIPT_SOURCE or not isinstance(record.content, dict):
        return "auto_review_receipt_missing"
    body = dict(record.content)
    signature = str(body.pop("signature", "") or "")
    keys = receipt_key_set()
    key = keys.verification_keys.get(body.get("key_id"), "") if keys else ""
    if not key or not signature or not hmac.compare_digest(signature, hmac.new(
            key.encode(), (RECEIPT_SCHEMA + ":" + _stable_digest(body)).encode(), sha256).hexdigest()):
        return "auto_review_receipt_signature_invalid"
    if body.get("schema") != RECEIPT_SCHEMA:
        return "auto_review_receipt_schema_invalid"
    return ""


def auto_review_revocation_reason(runtime: Any, *, pending_id: str, scope: ScopeRef) -> str:
    """Any revocation record for this pending case withdraws its auto labels.

    Revocation only removes authority, so it fails closed: an unsigned or
    malformed revocation still revokes.
    """

    if not pending_id:
        return ""
    lister = getattr(runtime.store, "list_records_by_meta_value", None)
    if not callable(lister):
        return "auto_review_revocation_state_unavailable"
    records = lister(kinds=["evaluation_packet"], scope=scope, meta_key="auto_review_revoked_pending",
                     meta_value=pending_id, status="active", limit=5)
    if records is None:
        return "auto_review_revocation_state_unavailable"
    for record in records:
        if record.source == RECEIPT_SOURCE and same_scope(record.scope, scope):
            return "auto_review_revoked"
    return ""


def derive_query_features(query: str) -> tuple[dict[str, Any], str]:
    """Redacted keyword features from the private original query.

    Keeps at most eight distinct informative tokens, with bounded Chinese
    keyword anchors. Digits, ID-like tokens and low-signal words are dropped.
    Original privacy markers are checked before tokenization. Results pass the same
    redaction and signal checks as an operator packet.
    """

    if not isinstance(query, str) or not query.strip():
        return {}, "original_query_input_unavailable"
    from .query_features import private_query_tokens
    tokens, private_reason = private_query_tokens(query)
    if private_reason:
        return {}, private_reason
    terms: list[str] = []
    for token in tokens:
        cjk = re.fullmatch(r"[\u4e00-\u9fff]+", token) is not None
        if (token in _LOW_SIGNAL_QUERY_TOKENS or token in _STOPWORDS or token.isdigit()
                or len(token) < 2 or (cjk and len(token) > _MAX_CJK_TERM_CHARS)
                or (not cjk and len(token) > 24)
                or (any(ch.isdigit() for ch in token) and len(token) > 6)):
            continue
        if token not in terms:
            terms.append(token)
        if len(terms) >= _MAX_TERMS:
            break
    if len(terms) < 2:
        return {}, "query_features_low_signal"
    features, reason = _bounded_query_features({"terms": terms})
    if reason:
        return {}, "query_features_" + reason.removeprefix("query_features_")
    quality = production_real_query_feature_quality_reasons(features)
    if quality:
        return {}, str(quality[0])
    return features, ""


def _semantic_observation(runtime: Any, decision: dict[str, Any], scope: ScopeRef) -> dict[str, Any]:
    """Return the validated semantic judgment for the exact delivered items."""
    import json
    from eimemory.adapters.runtime.channel import runtime_channel_from_scope
    from .semantic_relevance_monitor import (
        ELIGIBLE_TASK_TYPES, LEGACY_SURFACE, SOURCE, VERSION, _digest, _parse_result,
    )

    delivered = [item for item in decision.get("items") or [] if item.get("ever_injected")]
    if not delivered:
        return {"status": "not_delivered"}
    identity = _digest(dict(
        version=VERSION, decision_id=decision["decision_id"], scope=decision["scope"],
        source_ids=decision["source_ids"], query_digest=decision["query_digest"],
        release_identity=decision["release_identity"],
        delivered=[(i["record_id"], i.get("source_id"), i.get("render_digest")) for i in delivered]))
    latest = runtime.store.latest_record_by_meta_value_exact_scope(
        kind="evaluation_packet", source=SOURCE, scope=scope, status=None,
        meta_key="semantic_monitor_identity", meta_value=identity)
    records = [latest] if latest is not None else []
    unknown = None
    for record in records:
        report = record.content if isinstance(record.content, dict) else {}
        fields = ("relevance", "off_topic", "duplicates", "unanswered")
        if (record.source != SOURCE or not same_scope(record.scope, scope)
                or record.status != "active" or record.source_id != "default"
                or record.meta.get("report_type") != VERSION
                or record.meta.get("semantic_monitor_digest") != _digest(report)
                or report.get("evaluation_identity") != identity
                or report.get("decision_digest") != _digest(decision["decision_id"])
                or report.get("record_digests") != [_digest(i["record_id"]) for i in delivered[:32]]):
            continue
        surface = str(decision.get("task_type") or "")
        if surface not in ELIGIBLE_TASK_TYPES:
            continue
        # Surface provenance must match the judged decision; only legacy
        # memory.recall observations may predate the provenance fields.
        if "decision_surface" in report or surface != LEGACY_SURFACE:
            if (report.get("decision_surface") != surface
                    or report.get("channel") != (runtime_channel_from_scope(scope) or "openclaw")):
                continue
        if report.get("reason") != "evaluated":
            if unknown is None:
                reason = str(report.get("reason") or "")[:64]
                unknown = {"status": "not_evaluable" if reason in _STRUCTURAL_CLOSE else "unknown",
                           "reason": reason, "record_id": record.record_id}
            continue
        parsed = _parse_result(json.dumps({key: report.get(key) for key in fields}), len(delivered))
        if parsed.get("reason") != "evaluated" or parsed.get("verdict") != report.get("verdict"):
            continue
        from .semantic_quality_confirmation import cached_confirmation
        confirmation = cached_confirmation(runtime, record, scope)
        confirmed_refs = []
        if (confirmation is not None and parsed['verdict'] != 'unknown'
                and parsed['unanswered'] is False
                and confirmation.content['result'].get('unanswered') is False
                and confirmation.content['result'].get('verdict') != 'unknown'):
            from eimemory.retrieval.proactive import ProactiveRecallService
            hydrated = ProactiveRecallService(runtime)._rehydrate_persisted_items(
                dict(decision, items=delivered), cache_key='')
            if len(hydrated) == len(delivered):
                confirmed_refs = [item['record_id'] for item, first, second in zip(
                    delivered, parsed['relevance'], confirmation.content['result']['relevance'])
                    if first == second == 'relevant']
        return {"confirmed_refs": confirmed_refs,
                "confirmation_ref": confirmation.record_id if confirmation else '',
                "confirmation_digest": confirmation.meta.get('confirmation_digest') if confirmation else '',
                "status": "evaluated", "verdict": parsed["verdict"], "record_id": record.record_id,
                "decision_surface": surface, "unanswered": parsed["unanswered"],
                "digest": record.meta.get("semantic_monitor_digest"),
                "relevant_refs": [item["record_id"] for item, label in zip(delivered, parsed["relevance"])
                                  if label == "relevant"]}
    return unknown or {"status": "missing"}


def assess_pending_case(runtime: Any, pending: RecordEnvelope, *, exact_scope: ScopeRef,
                        channel: str, load_query: bool = True) -> dict[str, Any]:
    """Apply the v1 criteria to one pending case without writing anything."""
    from eimemory.retrieval.relevance import record_digest

    payload = pending.content if isinstance(pending.content, dict) else {}
    result: dict[str, Any] = {"pending_record_id": pending.record_id, "case_id": str(payload.get("case_id") or ""),
                              "channel": channel, "labels": [], "signals": {}, "query_features": {},
                              "candidate_refs_checked": False, "signal_checks_complete": False,
                              "recall_status": "not_evaluable",
                              "semantic_quality": {"status": "not_evaluable", "certified": False},
                              "signal_counts": {"candidates": 0, "delivered": 0, "semantic_relevant": 0,
                                                "verified_proof": 0, "host_used": 0, "host_rejected": 0, "semantic_confirmed": 0}}
    inputs: dict[str, Any] = {"criteria_version": CRITERIA_VERSION, "pending": _stable_digest(pending.to_dict())}

    def finish(disposition: str, reasons: list[str]) -> dict[str, Any]:
        result["disposition"] = disposition
        result["reasons"] = sorted(set(reasons))
        result["inputs_digest"] = _stable_digest(inputs)
        return result

    capture_reason = pending_production_query_capture_validation_error(
        runtime, pending, exact_scope=exact_scope, channel=channel)
    if capture_reason:
        # A pruned/missing decision is lost evidence, not a negative verdict:
        # close it as not evaluable (never accepted, never a pass).
        return finish("pending" if capture_reason in _PENDING_REASONS_TRANSIENT | _EVIDENCE_LOSS_REASONS
                      else "rejected", [capture_reason])
    refs = [str(item) for item in payload.get("candidate_refs") or []]
    result["candidate_refs_checked"] = True
    result["signal_counts"]["candidates"] = len(refs)
    result["recall_status"] = "recalled" if refs else "no_recall"
    if not refs:
        # Prefer the collector's bounded empty_reason; fall back to re-reading
        # decision diagnostics so already-collected session-dedupe pendings are
        # not mis-closed as no_recall.
        empty_reason = str(payload.get("empty_reason") or "")
        if empty_reason != "session_deduped_prior_delivery":
            with runtime.store.locked() as db:
                decision = db.load_proactive_decision(str(payload.get("capture_ref") or ""))
            if decision is not None:
                import json as _json
                empty_reason = _empty_decision_reason(
                    _json.dumps(decision.get("retrieval_diagnostics") or {}, ensure_ascii=True))
        if empty_reason == "session_deduped_prior_delivery":
            result["recall_status"] = "not_evaluable"
            return finish("pending", ["session_deduped_prior_delivery"])
        # An empty result is never certified as a true no-answer automatically.
        return finish("pending", ["no_candidate_refs"])
    with runtime.store.locked() as db:
        decision = db.load_proactive_decision(str(payload.get("capture_ref") or ""))
    if decision is None:
        return finish("pending", ["pending_capture_decision_missing"])
    items = {str(item["record_id"]): item for item in decision.get("items") or []}
    inputs["decision"] = {
        "decision_id": decision["decision_id"], "query_digest": decision["query_digest"],
        "release_identity": decision["release_identity"],
        "items": [[i["record_id"], i["state"], bool(i["ever_injected"]), i.get("render_digest"),
                   _stable_digest(i.get("render_evidence") or {})] for i in decision.get("items") or []],
    }
    semantic = _semantic_observation(runtime, decision, exact_scope)
    result["semantic_quality"] = {**semantic, "certified": False}
    inputs["semantic"] = {key: semantic.get(key)
                          for key in ("status", "record_id", "digest", "verdict", "decision_surface",
                                      "confirmation_ref", "confirmation_digest", "confirmed_refs")}
    if semantic.get("status") == "evaluated" and semantic.get("verdict") == "off_topic":
        return finish("rejected", ["semantic_off_topic"])
    if refs and all(str((items.get(ref) or {}).get("state") or "") == "rejected" for ref in refs):
        return finish("rejected", ["host_rejected_all_candidates"])
    relevant = set(semantic.get("relevant_refs") or [])
    reasons: list[str] = []
    candidates: dict[str, str] = {}
    counts = result["signal_counts"]
    for ref in refs:
        record = runtime.store.get_by_id(ref, scope=exact_scope)
        item = items.get(ref)
        from eimemory.subject_projection_consumption import projection_is_current
        if (record is None or item is None or record.status != "active"
                or record.source_id != str(payload.get("source_id") or "")
                or not same_scope(record.scope, exact_scope)
                or not projection_is_current(runtime.store, record)):
            reasons.append("candidate_unavailable")
            continue
        current_digest = record_digest(record)
        candidates[ref] = current_digest
        state = str(item.get("state") or "")
        delivered = bool(item.get("ever_injected"))
        counts["delivered"] += int(delivered)
        if state == "rejected":
            counts["host_rejected"] += 1
            reasons.append("host_rejected_candidate")
            continue
        evidence = item.get("render_evidence") if isinstance(item.get("render_evidence"), dict) else {}
        signal = {
            "semantic_relevant": delivered and ref in relevant and semantic.get("verdict") != "unknown"
                                 and semantic.get("unanswered") is False,
            "semantic_confirmed": delivered and ref in set(semantic.get("confirmed_refs") or []),
            "verified_proof": (evidence.get("format") == "verified-parent-span.v1"
                               and evidence.get("record_id") == ref
                               and evidence.get("record_digest") == current_digest),
            "host_used": delivered and state == "used",
        }
        for key, value in signal.items():
            counts[key] += int(value)
        if signal["semantic_relevant"] and (signal["verified_proof"] or signal["host_used"]
                                             or signal["semantic_confirmed"]):
            result["signals"][ref] = signal
            result["labels"].append({"record_ref": ref,
                                     "grade": 3 if signal["verified_proof"] and signal["host_used"] else 2})
    result["signal_checks_complete"] = True
    inputs["candidates"] = candidates
    if not result["labels"]:
        if not counts["delivered"]:
            reasons.append("no_candidate_delivered")
        if semantic.get("status") != "evaluated":
            reasons.append("semantic_judgment_" + str(semantic.get("status") or "missing"))
            if semantic.get("reason"):
                reasons.append(semantic["reason"])
        reasons.append("independent_signal_agreement_missing")
        return finish("pending", reasons)
    if not load_query:
        return finish("pending", ["query_features_not_loaded"])
    from .query_input_vault import load_query_input
    try:
        original = load_query_input(runtime, decision_id=decision["decision_id"], scope=exact_scope,
                                    channel=channel, source_id=str(payload.get("source_id") or ""))
    except ValueError as exc:
        code = str(exc)
        return finish("pending", [code if re.fullmatch(r"original_[a-z_]{1,80}", code)
                                  else "original_query_input_invalid"])
    inputs["query_input_digest"] = original.get("input_digest")
    features, feature_reason = derive_query_features(str(original.get("query") or ""))
    if feature_reason:
        return finish("pending", [feature_reason])
    result["query_features"] = features
    inputs["query_features"] = features
    return finish("accepted", [])


def current_auto_review_evidence_error(runtime, *, pending, scope, channel, packet, labels, query_features):
    """Revalidate v2 labels at signing, accepted readback and dataset activation."""
    if packet.get('criteria_version') != 'production-recall-auto-review.v2':
        return ''
    if auto_review_revocation_reason(runtime, pending_id=pending.record_id, scope=scope):
        return 'auto_review_label_revoked'
    current = assess_pending_case(runtime, pending, exact_scope=scope, channel=channel)
    expected_labels = [{'record_ref': label.get('record_ref'), 'grade': label.get('grade')} for label in labels]
    if (current['disposition'] != 'accepted'
            or current['inputs_digest'] != packet.get('inputs_digest')
            or current['signals'] != packet.get('signals')
            or current['labels'] != expected_labels or current['query_features'] != query_features):
        return 'auto_review_current_evidence_mismatch'
    return ''


_STRUCTURAL_CLOSE = {
    "unverified_delivery": "not_evaluable",
    "query_unavailable": "not_evaluable",
    "query_unverified": "not_evaluable",
    "empty_delivery": "not_delivered",
    "delivery_too_large": "not_evaluable",
    "no_candidate_refs": "no_recall",
    "session_deduped_prior_delivery": "session_deduped_prior_delivery",
    "no_candidate_delivered": "not_delivered",
    "original_query_input_unavailable": "not_evaluable",
    "original_query_input_boundary_mismatch": "not_evaluable",
    "original_query_input_digest_mismatch": "not_evaluable",
    "original_host_query_digest_mismatch": "not_evaluable",
    "pending_capture_decision_missing": "not_evaluable",
}
_RETRY_REASONS = frozenset({
    "completion_unavailable", "tool_free_transport_unavailable", "malformed_verdict",
    "pending_capture_authority_unavailable", "auto_review_attestation_key_unavailable",
    "auto_review_execution_failed",
})


TERMINAL_SOURCE = "eimemory.production_recall.evaluation_terminal"


def _structural_close_reason(reasons: list[str]) -> str:
    for reason in reasons:
        mapped = _STRUCTURAL_CLOSE.get(str(reason))
        if mapped:
            return mapped
    return ""


def _write_not_evaluable_receipt(
    runtime: Any, *, scope: ScopeRef, source_id: str, pending: RecordEnvelope,
    assessment: dict[str, Any], close_reason: str,
) -> str:
    """Record closure when the collected facts cannot support a label.

    The collection record stays active. This receipt is not an accepted label
    and does not certify that the answer was right or wrong.
    """
    body = {
        "schema": "production_recall_evaluation_terminal.v1",
        "criteria_version": CRITERIA_VERSION,
        "pending_record_id": pending.record_id,
        "case_id": assessment.get("case_id") or "",
        "channel": assessment.get("channel") or "",
        "disposition": "closed_without_label" if close_reason == "independent_signal_agreement_missing" else "not_evaluable",
        "inputs_digest": assessment["inputs_digest"],
        "close_reason": close_reason,
        "answer_quality": "not_evaluable",
        "recall_status": assessment.get("recall_status", "not_evaluable"),
        "semantic_quality": assessment.get("semantic_quality", {"status": "not_evaluable", "certified": False}),
        "does_not_certify_answer": True,
        "collection_record_status": "active",
        "review_contract": REVIEW_CONTRACT,
        "review_verdict": "fail",
        "review_complete": True,
        "reasons": list(assessment["reasons"]),
    }
    if not _structural_close_reason(assessment["reasons"]):
        body["disposition"] = "closed_without_label"
    record_id = "pret_" + _stable_digest(body)[:32]
    if runtime.store.get_by_id(record_id, scope=scope) is not None:
        return record_id
    record = RecordEnvelope.create(
        kind="evaluation_packet",
        title="Production recall review closed without a label",
        summary="Collected facts cannot certify an answer. The collection record stays active.",
        content=_signed_content({**body, "closed_at": now_iso()}, required=False),
        source=TERMINAL_SOURCE,
        source_id=source_id,
        scope=scope,
        status="active",
        evidence=[pending.record_id],
        meta={"report_type": "production_recall_evaluation_terminal",
              "pending_record_id": pending.record_id,
              "disposition": body["disposition"], "close_reason": close_reason,
              "criteria_version": CRITERIA_VERSION},
    )
    record.record_id = record_id
    runtime.store.append(record)
    return record_id


def _write_receipt(runtime: Any, *, scope: ScopeRef, source_id: str, assessment: dict[str, Any],
                   accepted_record_id: str = "") -> str:
    body = {
        "schema": RECEIPT_SCHEMA,
        "criteria_version": CRITERIA_VERSION,
        "reviewer": REVIEWER,
        "pending_record_id": assessment["pending_record_id"],
        "case_id": assessment["case_id"],
        "channel": assessment["channel"],
        "disposition": assessment["disposition"],
        "reasons": list(assessment["reasons"]),
        "inputs_digest": assessment["inputs_digest"],
        "signal_counts": dict(assessment["signal_counts"]),
        "recall_status": assessment.get("recall_status", "not_evaluable"),
        "semantic_quality": assessment.get("semantic_quality", {"status": "not_evaluable", "certified": False}),
        "accepted_record_id": accepted_record_id,
        "review_contract": REVIEW_CONTRACT,
        "review_verdict": ("pending" if assessment["disposition"] == "pending" else
                           "pass" if assessment["disposition"] == "accepted" else "fail"),
        "review_complete": assessment["disposition"] != "pending",
    }
    record_id = "prar_" + _stable_digest(body)[:32]
    if runtime.store.get_by_id(record_id, scope=scope) is not None:
        return record_id
    record = RecordEnvelope.create(
        kind="evaluation_packet",
        title=f"Production recall auto-review {assessment['disposition']}",
        summary="Deterministic auto-review verdict for one pending production-query case.",
        content=_signed_content({**body, "reviewed_at": now_iso()},
                                required=assessment["disposition"] != "pending"),
        source=RECEIPT_SOURCE,
        source_id=source_id,
        scope=scope,
        status="active",
        evidence=[assessment["pending_record_id"], *([accepted_record_id] if accepted_record_id else [])],
        meta={"report_type": "production_recall_auto_review", "pending_record_id": assessment["pending_record_id"],
              "disposition": assessment["disposition"], "criteria_version": CRITERIA_VERSION},
    )
    record.record_id = record_id
    runtime.store.append(record)
    return record_id


def _channel_records(runtime: Any, *, scope: ScopeRef, report_type: str, source: str, limit: int) -> list[RecordEnvelope]:
    records = runtime.store.list_records_by_meta_value(
        kinds=["evaluation_packet"], scope=scope, meta_key="report_type", meta_value=report_type,
        status="active", limit=limit) or []
    return [record for record in records if record.source == source and same_scope(record.scope, scope)]


def _unavailable_assessment(pending: RecordEnvelope, channel: str, reason: str) -> dict[str, Any]:
    return {"pending_record_id": pending.record_id, "case_id": str(pending.content.get("case_id") or ""),
            "channel": channel, "labels": [], "signals": {}, "query_features": {},
            "signal_counts": {}, "disposition": "pending", "reasons": [reason],
            "inputs_digest": _stable_digest({"criteria_version": CRITERIA_VERSION,
                                             "pending": _stable_digest(pending.to_dict()), "reason": reason})}


def auto_review_pending_production_queries(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    dry_run: bool = False,
    limit: int = 500,
    channel: str | None = None,
) -> dict[str, Any]:
    """Review every active pending case in each exact channel scope."""
    from eimemory.governance.tool_receipts import receipt_key_set
    from .review_progress import load_review_batch, complete_review_batch

    requested = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    resolved_scope, scope_resolution = resolve_bound_logical_scope(requested)
    base = ScopeRef.from_dict(resolved_scope)
    if channel is not None and channel not in SUPPORTED_RUNTIME_CHANNELS:
        raise ValueError("supported exact channel required")
    enabled = production_recall_auto_review_enabled()
    report: dict[str, Any] = {
        "ok": True, "schema": RUN_SCHEMA, "criteria_version": CRITERIA_VERSION,
        "status": "completed" if enabled else "disabled", "dry_run": bool(dry_run),
        "policy": {"flag": PRODUCTION_RECALL_AUTO_REVIEW_FLAG, "enabled": enabled},
        "scanned_count": 0, "accepted_count": 0, "pending_count": 0, "rejected_count": 0,
        "open_review_count": 0, "closed_not_evaluable_count": 0, "closed_without_label_count": 0,
        "already_accepted_count": 0, "accepted_record_ids": [], "would_accept_pending_ids": [],
        "receipt_ids": [], "reason_counts": {"not_passed": {}, "rejected": {}, "pending": {}},
        "signal_counts": {}, "existing_accepted_by_authority": {"operator": 0, "auto_review": 0, "unknown": 0},
        "by_channel": {}, "scope_resolution": scope_resolution,
        "review_contract": REVIEW_CONTRACT, "reviewed_count": 0,
        "passed_count": 0, "not_passed_count": 0, "review_results": [],
        "coverage": {"schema": "production_recall_review_progress.v1",
                     "boundary": "fixed_high_water_snapshot", "count_scope": "reviewed_batch", "by_channel": {}},
    }
    authority_reason = "auto_review_disabled" if not enabled else ""
    if enabled and receipt_key_set() is None:
        authority_reason = "auto_review_attestation_key_unavailable"
        report.update(ok=bool(dry_run), status="blocked", blocked_reason=authority_reason)
    bounded = max(1, min(500, int(limit)))
    channels = [channel] if channel else sorted(SUPPORTED_RUNTIME_CHANNELS)
    for selected in channels:
        exact = ScopeRef.from_dict(resolve_channel_scope(selected, asdict(base)))
        accepted_by_pending: dict[str, str] = {}
        for record in _channel_records(runtime, scope=exact, report_type="production_recall_accepted_case",
                                       source=ACCEPTED_SOURCE, limit=bounded):
            if accepted_production_query_validation_error(runtime, record, exact_scope=exact, channel=selected):
                continue
            case = record.content.get("case") if isinstance(record.content.get("case"), dict) else {}
            authorities = {production_real_query_label_authority((label.get("provenance") or {}).get("labeler"))
                           for label in case.get("labels") or [] if isinstance(label, dict)}
            authority = "unknown" if not authorities or "unknown" in authorities else ("auto_review" if "auto_review" in authorities else "operator")
            report["existing_accepted_by_authority"][authority] += 1
            if record.evidence:
                accepted_by_pending.setdefault(str(record.evidence[0]), record.record_id)
        channel_counts = {"scanned": 0, "accepted": 0, "pending": 0, "rejected": 0,
                          "already_accepted": 0, "not_passed": 0}
        batch = load_review_batch(runtime, scope=exact, limit=bounded)
        for pending in batch["records"]:
            report["scanned_count"] += 1
            channel_counts["scanned"] += 1
            if pending.record_id in accepted_by_pending:
                report["already_accepted_count"] += 1
                report["passed_count"] += 1
                channel_counts["already_accepted"] += 1
                report["review_results"].append({"pending_record_id": pending.record_id, "channel": selected,
                                                 "review_verdict": "pass", "review_complete": True,
                                                 "accepted_record_id": accepted_by_pending[pending.record_id],
                                                 "reused_accepted": True,
                                                 "reasons": []})
                continue
            report["reviewed_count"] += 1
            revoked = auto_review_revocation_reason(runtime, pending_id=pending.record_id, scope=exact)
            if authority_reason or revoked:
                assessment = _unavailable_assessment(pending, selected, authority_reason or revoked)
            else:
                try:
                    assessment = assess_pending_case(runtime, pending, exact_scope=exact, channel=selected)
                except Exception:
                    # One unavailable case still gets a durable non-passing
                    # conclusion; private exceptions never enter the receipt.
                    assessment = _unavailable_assessment(pending, selected, "auto_review_execution_failed")
                    report.update(ok=False, status="blocked", blocked_reason="auto_review_execution_failed")
            for key, value in assessment.get("signal_counts", {}).items():
                report["signal_counts"][key] = int(report["signal_counts"].get(key) or 0) + int(value)
            disposition = assessment["disposition"]
            accepted_record_id = ""
            review_receipt_id = ""
            if disposition == "accepted":
                packet = {
                    "schema": "production_recall_auto_review_packet.v1",
                    "criteria_version": CRITERIA_VERSION,
                    "reviewer": REVIEWER,
                    "pending_record_id": pending.record_id,
                    "inputs_digest": assessment["inputs_digest"],
                    "signals": assessment["signals"],
                    "query_features_origin": QUERY_FEATURES_ORIGIN,
                    "reviewed_at": now_iso(),
                }
                if dry_run:
                    report["would_accept_pending_ids"].append(pending.record_id)
                else:
                    try:
                        accepted = accept_auto_reviewed_production_query(
                            runtime, pending_record_id=pending.record_id,
                            query_features=assessment["query_features"], labels=assessment["labels"],
                            auto_review_packet=packet)
                        saved = runtime.store.get_by_id(str(accepted["record_id"]), scope=exact)
                        if saved is None or accepted_production_query_validation_error(
                                runtime, saved, exact_scope=exact, channel=selected):
                            raise ValueError("accepted_record_invalid")
                        accepted_record_id = saved.record_id
                        report["accepted_record_ids"].append(accepted_record_id)
                    except ValueError as exc:
                        code = str(exc)
                        disposition = "pending"
                        assessment = {**assessment, "disposition": "pending",
                                      "reasons": ["auto_accept_failed:" + (code if re.fullmatch(r"[a-z_:.0-9 ]{1,80}", code)
                                                                             else "invalid")]}
                    except Exception:
                        disposition = "pending"
                        assessment = {**assessment, "disposition": "pending",
                                      "reasons": ["auto_review_execution_failed"]}
                        report.update(ok=False, status="blocked", blocked_reason="auto_review_execution_failed")
            if disposition != "accepted":
                report["not_passed_count"] += 1
                channel_counts["not_passed"] += 1
                bucket = report["reason_counts"]["not_passed"]
                for reason in assessment["reasons"]:
                    bucket[reason] = int(bucket.get(reason) or 0) + 1
                if disposition == "rejected":
                    for reason in assessment["reasons"]:
                        bucket = report["reason_counts"]["rejected"]
                        bucket[reason] = int(bucket.get(reason) or 0) + 1
            else:
                report["passed_count"] += 1
            retryable = disposition == "pending" and any(
                reason in _RETRY_REASONS or reason.startswith("auto_accept_failed:")
                for reason in assessment["reasons"])
            close_reason = _structural_close_reason(assessment.get("reasons") or [])
            if retryable:
                report["open_review_count"] += 1
                report["pending_count"] += 1
                channel_counts["pending"] += 1
                for reason in assessment["reasons"]:
                    bucket = report["reason_counts"]["pending"]
                    bucket[reason] = int(bucket.get(reason) or 0) + 1
                if not dry_run:
                    review_receipt_id = _write_receipt(
                        runtime, scope=exact, source_id=pending.source_id, assessment=assessment)
                    report["receipt_ids"].append(review_receipt_id)
            elif disposition == "pending":
                report["closed_not_evaluable_count" if close_reason else "closed_without_label_count"] += 1
                close_reason = close_reason or str(assessment["reasons"][0])
                if not dry_run:
                    review_receipt_id = _write_not_evaluable_receipt(
                        runtime, scope=exact, source_id=pending.source_id, pending=pending,
                        assessment=assessment, close_reason=close_reason)
                    report["receipt_ids"].append(review_receipt_id)
            else:
                report[f"{disposition}_count"] += 1
                channel_counts[disposition] += 1
            if not dry_run and disposition != "pending":
                review_receipt_id = _write_receipt(
                    runtime, scope=exact, source_id=pending.source_id, assessment=assessment,
                    accepted_record_id=accepted_record_id)
                report["receipt_ids"].append(review_receipt_id)
            report["review_results"].append({"pending_record_id": pending.record_id, "channel": selected,
                                             "review_verdict": "pending" if retryable else "pass" if disposition == "accepted" else "fail",
                                             "review_complete": not retryable, "reasons": list(assessment["reasons"]),
                                             "inputs_digest": assessment["inputs_digest"],
                                             "review_receipt_id": review_receipt_id,
                                             "accepted_record_id": accepted_record_id,
                                             "signal_counts": dict(assessment.get("signal_counts") or {}),
                                             "candidate_refs_checked": assessment.get("candidate_refs_checked"),
                                             "signal_checks_complete": assessment.get("signal_checks_complete"),
                                             "recall_status": assessment.get("recall_status"),
                                             "semantic_quality": assessment.get("semantic_quality")})
        report["by_channel"][selected] = channel_counts
        coverage = batch["coverage"]
        prior_open = batch["next"]["open_review_count"]
        batch["next"]["open_review_count"] = (
            prior_open + channel_counts["pending"] if prior_open is not None else None)
        coverage["open_review_count"] = batch["next"]["open_review_count"]
        if not dry_run:
            coverage["cursor_persisted"] = complete_review_batch(runtime, scope=exact, batch=batch)
            coverage["cursor_conflict"] = not coverage["cursor_persisted"]
        report["coverage"]["by_channel"][selected] = coverage
    covered = list(report["coverage"]["by_channel"].values())
    complete = all(item["cycle_complete"] and not item["cursor_conflict"] for item in covered)
    report["coverage"].update(
        cycle_complete=complete,
        total_case_count=sum(item["total_case_count"] for item in covered),
        batch_uncovered_count=sum(item["batch_uncovered_count"] for item in covered),
        cycle_remaining_count=sum(item["cycle_remaining_count"] for item in covered),
        newer_case_count=sum(item["newer_case_count"] for item in covered),
    )
    if not complete or dry_run:
        # These counters described the scanned batch historically. A partial
        # cycle cannot certify that the entire queue has no open reviews.
        report["open_review_count"] = None
        report["pending_count"] = None
        if report["status"] == "completed":
            report["status"] = "dry_run" if dry_run else "partial"
    elif all(item["open_review_count"] is not None for item in covered):
        report["open_review_count"] = report["pending_count"] = sum(item["open_review_count"] for item in covered)
    else:
        report["open_review_count"] = report["pending_count"] = None
    for disposition in ("not_passed", "rejected", "pending"):
        report["reason_counts"][disposition] = dict(sorted(report["reason_counts"][disposition].items()))
    report["receipt_count"] = len(report.pop("receipt_ids"))
    return report


def revoke_auto_reviewed_production_query(
    runtime: Any,
    *,
    pending_record_id: str,
    scope: dict[str, Any] | ScopeRef | None,
    reason: str,
    revoked_by: str = "operator",
) -> dict[str, Any]:
    """Withdraw auto-review authority for one pending case (append-only)."""

    code = str(reason or "").strip()
    actor = str(revoked_by or "").strip()
    if re.fullmatch(r"[a-z0-9_.:-]{1,80}", code) is None:
        raise ValueError("revocation reason code required")
    if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", actor) is None:
        raise ValueError("revocation actor required")
    pending = runtime.store.get_by_id(str(pending_record_id or ""))
    if pending is None or pending.source != PENDING_SOURCE or not isinstance(pending.content, dict):
        raise ValueError("trusted pending production query required")
    channel = str(pending.content.get("channel") or "")
    base = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    exact = ScopeRef.from_dict(resolve_channel_scope(channel, asdict(base)))
    if not same_scope(pending.scope, exact):
        raise ValueError("pending query boundary mismatch")
    accepted_ids = sorted(
        record.record_id for record in _channel_records(
            runtime, scope=exact, report_type="production_recall_accepted_case", source=ACCEPTED_SOURCE, limit=500)
        if record.evidence and str(record.evidence[0]) == pending.record_id
        and record.meta.get("label_authority") == "auto_review")
    body = {"schema": RECEIPT_SCHEMA, "criteria_version": CRITERIA_VERSION, "reviewer": REVIEWER,
            "pending_record_id": pending.record_id, "channel": channel, "disposition": "revoked",
            "reasons": [code], "revoked_by": actor, "revoked_accepted_record_ids": accepted_ids}
    record_id = "prar_" + _stable_digest(body)[:32]
    existing = runtime.store.get_by_id(record_id, scope=exact)
    if existing is None:
        # Revocation only withdraws authority; it stays possible without a key.
        content = _signed_content({**body, "revoked_at": now_iso()}, required=False)
        record = RecordEnvelope.create(
            kind="evaluation_packet", title="Production recall auto-review revoked",
            summary="Auto-reviewed labels for this pending case no longer count.",
            content=content, source=RECEIPT_SOURCE, source_id=pending.source_id, scope=exact,
            status="active", evidence=[pending.record_id, *accepted_ids],
            meta={"report_type": "production_recall_auto_review", "pending_record_id": pending.record_id,
                  "disposition": "revoked", "auto_review_revoked_pending": pending.record_id,
                  "criteria_version": CRITERIA_VERSION})
        record.record_id = record_id
        runtime.store.append(record)
    return {"ok": True, "record_id": record_id, "pending_record_id": pending.record_id,
            "revoked_accepted_record_ids": accepted_ids, "already_revoked": existing is not None}


__all__ = [
    "CRITERIA_VERSION", "RECEIPT_SOURCE", "assess_pending_case", "auto_review_pending_production_queries",
    "auto_review_receipt_error", "auto_review_revocation_reason", "derive_query_features",
    "revoke_auto_reviewed_production_query",
]
