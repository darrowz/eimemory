"""Bind terminal patch outcomes to their original capability hypothesis.

Transaction completion is never evidence of capability improvement. Only an
independently executed, receipt-bound test failure restricts the originating
hypothesis; unavailable verification and dry runs remain non-certifying traces.
The protected ledger is read again when feedback is consumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.code_evolution_store import CodeEvolutionStore, digest_json


SCHEMA = "capability_hypothesis_transaction_feedback.v1"
SOURCE = "eimemory.code_evolution.hypothesis_feedback"


class TransactionFeedbackError(ValueError):
    pass


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise TransactionFeedbackError(reason)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _terminal_feedback(store: Any, *, transaction_id: str, scope: ScopeRef,
                       candidate_id: str = "", hypothesis_id: str = "",
                       expected_item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    ledger = CodeEvolutionStore(store)
    tx = ledger.get_transaction(transaction_id)
    _require(bool(tx), "transaction_not_found")
    outer = _mapping(tx.get("payload"))
    proposal = _mapping(outer.get("payload"))
    context = _mapping(proposal.get("dynamic_capability_context"))
    _require(bool(context), "dynamic_context_missing")
    _require(outer.get("scope") == asdict(scope) == context.get("scope"), "transaction_scope_mismatch")
    _require(all(tx.get(key) == value for key, value in asdict(scope).items()), "transaction_scope_mismatch")
    _require(context.get("schema") == "dynamic_capability_patch_context.v1"
             and context.get("context_digest") == digest_json({k: v for k, v in context.items() if k != "context_digest"}),
             "dynamic_context_digest_mismatch")
    item = _mapping(context.get("item"))
    if expected_item is not None:
        for key in ("work_item_id", "capability_id", "capability_revision_id", "provider_binding_id", "capability_scope",
                    "hypothesis_id", "hypothesis_link_id", "hypothesis_link_digest"):
            _require(expected_item.get(key) == item.get(key), f"consumer_{key}_mismatch")
    bound_hypothesis = str(item.get("hypothesis_id") or "")
    _require(bool(bound_hypothesis) and (not hypothesis_id or hypothesis_id == bound_hypothesis), "hypothesis_mismatch")
    hypothesis = store.get_by_id(bound_hypothesis, scope=scope)
    _require(hypothesis is not None and hypothesis.kind == "capability_hypothesis"
             and asdict(hypothesis.scope) == asdict(scope), "hypothesis_unavailable")
    content = _mapping(hypothesis.content)
    for item_key, content_key in (("capability_id", "capability_id"), ("capability_revision_id", "capability_revision_id"),
                                  ("capability_scope", "capability_scope"), ("hypothesis_link_id", "link_id"),
                                  ("hypothesis_link_digest", "link_digest")):
        _require(bool(item.get(item_key)) and item.get(item_key) == content.get(content_key), f"hypothesis_{content_key}_mismatch")
    for item_key, tx_key in (("capability_id", "capability_id"), ("capability_revision_id", "revision_id"),
                            ("provider_binding_id", "binding_id")):
        _require(item.get(item_key) == tx.get(tx_key), f"transaction_{tx_key}_mismatch")
    _require(context.get("base_commit") == tx.get("base_commit") == _mapping(proposal.get("repository")).get("base_commit"),
             "transaction_base_mismatch")
    _require(proposal.get("transaction_id") == transaction_id
             and proposal.get("proposal_digest") == tx.get("proposal_digest")
             and proposal.get("patch_digest") == tx.get("patch_digest")
             and _mapping(proposal.get("incident")).get("incident_digest") == tx.get("incident_digest"),
             "transaction_proposal_mismatch")
    _require(not tx.get("policy_digest") or tx.get("policy_digest") == context.get("policy_digest"), "transaction_policy_mismatch")
    if candidate_id:
        candidate = store.get_by_id(candidate_id, scope=scope)
        patch = _mapping(_mapping(getattr(candidate, "content", {})).get("candidate_patch"))
        _require(candidate is not None and candidate.kind == "capability_candidate"
                 and asdict(candidate.scope) == asdict(scope)
                 and patch.get("code_evolution_v2") is True and patch.get("transaction_id") == transaction_id
                 and patch.get("dynamic_capability_context") == context
                 and patch.get("proposal_digest") == tx.get("proposal_digest"), "candidate_transaction_mismatch")
    _require(tx.get("terminal") is True or tx.get("terminal") == 1, "transaction_not_terminal")
    terminal = ledger.get_terminal_receipt(transaction_id)
    _require(bool(terminal), "terminal_receipt_missing")
    _require(terminal.get("receipt_digest") == tx.get("terminal_receipt_digest")
             and terminal.get("outcome", "").upper() == tx.get("current_state"), "terminal_identity_mismatch")
    for terminal_key, tx_key in (("incident_digest", "incident_digest"), ("provider_digest", "implementation_digest"),
                               ("base_commit", "base_commit"), ("candidate_commit", "candidate_commit"),
                               ("policy_digest", "policy_digest"), ("authorization_digest", "authorization_digest")):
        _require(terminal.get(terminal_key, "") == tx.get(tx_key, ""), f"terminal_{terminal_key}_mismatch")
    terminal_body = _mapping(terminal.get("payload"))
    # Preserve the original protected event time on retries/backfills. Never
    # make an old failure newer than subsequently collected independent proof.
    timestamp = str(terminal_body.get("hypothesis_feedback_at") or terminal.get("created_at") or "")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        _require(parsed.tzinfo is not None, "terminal_timestamp_invalid")
        timestamp = parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")
    except ValueError as exc:
        raise TransactionFeedbackError("terminal_timestamp_invalid") from exc

    restrictive = False
    receipt_digests: list[str] = []
    from eimemory.governance.evolution.code_evolution_transaction import effect_execution_authorized
    from eimemory.governance.evolution.code_evolution_test_plans import protected_test_plan_digest

    plan = _mapping(proposal.get("test_plan"))
    if tx.get("current_state") == "ABORTED_CANDIDATE_RESTORED" and effect_execution_authorized(tx):
        for receipt in ledger.list_verification_receipts(transaction_id):
            if terminal.get("evidence_digest") != receipt.get("receipt_digest"):
                continue
            phase = str(receipt.get("verification_kind") or "")
            _require(receipt.get("base_commit") == tx.get("base_commit")
                     and receipt.get("patch_digest") == tx.get("patch_digest")
                     and bool(tx.get("candidate_tree_digest"))
                     and receipt.get("candidate_tree_digest") == tx.get("candidate_tree_digest")
                     and receipt.get("test_plan_id") == plan.get("id")
                     and receipt.get("test_plan_digest") == plan.get("digest") == protected_test_plan_digest(str(plan.get("id") or ""))
                     and receipt.get("verifier_id") == "eimemory.protected-test-plan"
                     and receipt.get("verifier_revision") == "v1", "verification_identity_mismatch")
            log = ledger.get_artifact(transaction_id, f"verification_log_{phase}")
            _require(bool(log) and log.get("sha256") == receipt.get("log_artifact_digest"), "verification_log_missing")
            receipt_digests.append(str(receipt["receipt_digest"]))
            # Pytest exit 1 with actual failed assertions certifies a failure;
            # collection, sandbox and transport failures do not grade behavior.
            restrictive = (receipt.get("result") == "fail" and receipt.get("exit_status") == 1
                           and int(receipt.get("failed_count", 0)) > 0
                           and terminal_body.get("reason") == f"{phase}_verification_failed")
    return dict(schema=SCHEMA, hypothesis_id=bound_hypothesis,
                link_id=content["link_id"], link_digest=content["link_digest"],
                capability_id=item["capability_id"], capability_revision_id=item["capability_revision_id"],
                capability_scope=item["capability_scope"], provider_binding_id=item["provider_binding_id"],
                work_item_id=item["work_item_id"], transaction_id=transaction_id,
                context_digest=context["context_digest"], terminal_receipt_digest=terminal["receipt_digest"],
                verification_receipt_digests=receipt_digests, terminal_outcome=terminal["outcome"],
                occurred_at=timestamp, applicability_effect="restrictive" if restrictive else "trace_only",
                qualifies_behavior=False, certifies_improvement=False, qualifies_for_product_completion=False)


def record_transaction_hypothesis_feedback(runtime: Any, *, transaction_id: str,
                                           scope: ScopeRef | None = None, candidate_id: str = "",
                                           hypothesis_id: str = "", expected_item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Idempotently record typed terminal evidence, without executing effects."""
    store = getattr(runtime, "store", runtime)
    try:
        if scope is None:
            tx = CodeEvolutionStore(store).get_transaction(transaction_id) or {}
            scope = ScopeRef.from_dict(dict(_mapping(_mapping(tx.get("payload")).get("scope"))))
        payload = _terminal_feedback(store, transaction_id=transaction_id, scope=scope,
                                     candidate_id=candidate_id, hypothesis_id=hypothesis_id, expected_item=expected_item)
        record_id = "chtf_" + digest_json(payload)[:32]
        record = store.get_by_id(record_id, scope=scope)
        if record is not None:
            _require(record.content == payload and record.source == SOURCE and record.status == "active"
                     and asdict(record.scope) == asdict(scope), "transaction_feedback_conflict")
        else:
            record = RecordEnvelope.create(kind="feedback", scope=scope, source=SOURCE,
                title="Capability patch transaction feedback", content=payload,
                evidence=[transaction_id, payload["terminal_receipt_digest"]])
            record.record_id = record_id
            record.time = TimeRef(payload["occurred_at"], payload["occurred_at"], payload["occurred_at"])
            store.append(record)
        return dict(ok=True, status="recorded", feedback_id=record_id, transaction_id=transaction_id,
                    applicability_effect=payload["applicability_effect"], qualifies_behavior=False,
                    certifies_improvement=False, qualifies_for_product_completion=False)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, TransactionFeedbackError) else f"transaction_evidence_invalid:{type(exc).__name__}"
        return dict(ok=False, status="pending" if reason == "transaction_not_terminal" else "blocked",
                    reason=reason, transaction_id=transaction_id, qualifies_behavior=False)


def transaction_feedback_restriction(store: Any, *, feedback: RecordEnvelope,
                                     scope: ScopeRef, hypothesis_id: str) -> str:
    """Only intact ledger-bound negative proof can restrict this exact trace."""
    bound = False
    try:
        hypothesis = store.get_by_id(hypothesis_id, scope=scope)
        content = _mapping(getattr(hypothesis, "content", {}))
        bound = (feedback.content.get("hypothesis_id") == hypothesis_id
                 and asdict(feedback.scope) == asdict(scope)
                 and all(feedback.content.get(key) == content.get(key) for key in
                         ("capability_id", "capability_revision_id", "capability_scope", "link_id", "link_digest")))
        if not bound:
            return ""
        _require(feedback.source == SOURCE and feedback.status == "active"
                 and asdict(feedback.scope) == asdict(scope), "feedback_identity_invalid")
        payload = _terminal_feedback(store, transaction_id=str(feedback.content.get("transaction_id") or ""),
                                     scope=scope, hypothesis_id=hypothesis_id)
        _require(feedback.content == payload and feedback.record_id == "chtf_" + digest_json(payload)[:32]
                 and feedback.time.created_at == payload["occurred_at"], "feedback_content_mismatch")
        return "latest_transaction_feedback_not_pass" if payload["applicability_effect"] == "restrictive" else ""
    except Exception:
        # Corruption cannot silently erase an already bound negative result.
        # An unrelated record does not acquire transaction authority by merely
        # using the schema name.
        if bound and feedback.source == SOURCE and feedback.content.get("applicability_effect") == "restrictive":
            return "transaction_feedback_evidence_invalid"
        return ""
