"""Deterministic auto-label proposals for production real-query backlog.

Does **not** mint trusted gold labels. Proposals carry provenance
``labeler=auto_heuristic`` and ``review_status=needs_review`` so release
acceptance is not blocked forever on missing human labels, while promotion
still requires an explicit operator accept path.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation.production_query_dataset import (
    PENDING_QUERY_SCHEMA,
    PENDING_SOURCE,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


PROPOSAL_SCHEMA = "production_recall_auto_label_proposal.v1"
PROPOSAL_SOURCE = "eimemory.production_recall.auto_label_proposal"
PROPOSAL_LABELER = "auto_heuristic"
REVIEW_STATUS_NEEDS_REVIEW = "needs_review"
REVIEW_STATUS_PROMOTED = "promoted"
REVIEW_STATUS_REJECTED = "rejected"


def propose_auto_labels_for_pending(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    limit: int = 100,
) -> dict[str, Any]:
    """Create proposed labels for unlabeled pending production queries."""

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope or {})
    bounded = max(1, min(500, int(limit)))
    pending_rows = runtime.store.list_records(
        kinds=["evaluation_packet"],
        scope=scope_ref,
        status="active",
        limit=bounded * 4,
    )
    pending = [
        record
        for record in pending_rows
        if record.source == PENDING_SOURCE
        and isinstance(record.content, Mapping)
        and record.content.get("schema") == PENDING_QUERY_SCHEMA
        and not _has_active_proposal(runtime, pending_id=record.record_id, scope=scope_ref)
    ][:bounded]

    created: list[str] = []
    proposals: list[dict[str, Any]] = []
    for item in pending:
        proposal = _propose_for_pending(runtime, pending=item)
        if proposal is None:
            continue
        stored = runtime.store.append(proposal)
        created.append(stored.record_id)
        proposals.append(
            {
                "proposal_record_id": stored.record_id,
                "pending_record_id": item.record_id,
                "review_status": REVIEW_STATUS_NEEDS_REVIEW,
                "proposed_label_count": len(proposal.content.get("labels") or []),
                "heuristic": proposal.content.get("heuristic"),
            }
        )

    return {
        "ok": True,
        "schema": PROPOSAL_SCHEMA,
        "created": len(created),
        "proposal_record_ids": created,
        "proposals": proposals,
        "review_queue_status": REVIEW_STATUS_NEEDS_REVIEW,
        "note": "Proposals are not gold labels; promote via promote_auto_label_proposal after review.",
    }


def list_auto_label_review_queue(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    limit: int = 100,
    review_status: str = REVIEW_STATUS_NEEDS_REVIEW,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope or {})
    bounded = max(1, min(500, int(limit)))
    rows = runtime.store.list_records(
        kinds=["evaluation_packet"],
        scope=scope_ref,
        status="active",
        limit=bounded * 4,
    )
    items = []
    for record in rows:
        if record.source != PROPOSAL_SOURCE:
            continue
        content = record.content if isinstance(record.content, Mapping) else {}
        if content.get("schema") != PROPOSAL_SCHEMA:
            continue
        status = str(content.get("review_status") or "")
        if review_status and status != review_status:
            continue
        items.append(
            {
                "proposal_record_id": record.record_id,
                "pending_record_id": content.get("pending_record_id"),
                "review_status": status,
                "heuristic": content.get("heuristic"),
                "labels": list(content.get("labels") or []),
                "provenance": dict(content.get("provenance") or {}),
            }
        )
        if len(items) >= bounded:
            break
    return {
        "ok": True,
        "schema": PROPOSAL_SCHEMA,
        "count": len(items),
        "items": items,
        "review_status": review_status,
    }


def promote_auto_label_proposal(
    runtime: Any,
    *,
    proposal_record_id: str,
    operator_id: str,
) -> dict[str, Any]:
    """Mark a proposal reviewed/promoted. Does not itself write gold labels.

    Operators must still call the trusted accept path with an operator label
    packet; this only moves review_queue status and records provenance of the
    promotion decision so the backlog is no longer unlabeled forever.
    """

    operator = str(operator_id or "").strip()
    if not operator:
        raise ValueError("operator_id required to promote proposed labels")
    proposal = runtime.store.get_by_id(str(proposal_record_id or ""))
    if (
        proposal is None
        or proposal.kind != "evaluation_packet"
        or proposal.source != PROPOSAL_SOURCE
        or proposal.status != "active"
    ):
        raise ValueError("trusted auto-label proposal required")
    content = dict(proposal.content or {})
    if content.get("schema") != PROPOSAL_SCHEMA:
        raise ValueError("auto-label proposal schema mismatch")
    if content.get("review_status") == REVIEW_STATUS_PROMOTED:
        return {
            "ok": True,
            "proposal_record_id": proposal.record_id,
            "review_status": REVIEW_STATUS_PROMOTED,
            "already_promoted": True,
        }
    content["review_status"] = REVIEW_STATUS_PROMOTED
    content["promoted_at"] = now_iso()
    content["promoted_by"] = operator
    content["provenance"] = {
        **dict(content.get("provenance") or {}),
        "promoted_by": operator,
        "promotion_path": "operator_review_queue",
    }
    proposal.content = content
    proposal.touch()
    runtime.store.rewrite(proposal, previous_scope=proposal.scope)
    return {
        "ok": True,
        "proposal_record_id": proposal.record_id,
        "pending_record_id": content.get("pending_record_id"),
        "review_status": REVIEW_STATUS_PROMOTED,
        "labels": list(content.get("labels") or []),
        "next_step": "submit operator accept packet via eval production-query accept to mint gold labels",
    }


def _has_active_proposal(runtime: Any, *, pending_id: str, scope: ScopeRef) -> bool:
    rows = runtime.store.list_records(kinds=["evaluation_packet"], scope=scope, status="active", limit=200)
    for record in rows:
        if record.source != PROPOSAL_SOURCE:
            continue
        content = record.content if isinstance(record.content, Mapping) else {}
        if content.get("pending_record_id") == pending_id and content.get("review_status") in {
            REVIEW_STATUS_NEEDS_REVIEW,
            REVIEW_STATUS_PROMOTED,
        }:
            return True
    return False


def _propose_for_pending(runtime: Any, *, pending: RecordEnvelope) -> RecordEnvelope | None:
    payload = pending.content if isinstance(pending.content, Mapping) else {}
    refs = [str(item) for item in list(payload.get("candidate_refs") or []) if str(item).strip()]
    exact_scope = ScopeRef.from_dict(payload.get("scope") or asdict(pending.scope))
    if not refs:
        # Empty result pending: propose explicit no-answer for review.
        labels = []
        heuristic = "expected_empty_no_candidates"
    else:
        labels = []
        heuristic = "top_candidate_grade2_tail_grade1"
        for index, ref in enumerate(refs[:5]):
            record = runtime.store.get_by_id(ref, scope=exact_scope)
            if record is None or record.status != "active":
                continue
            # Deterministic, conservative grades — never claim gold relevance.
            grade = 2 if index == 0 else 1
            labels.append(
                {
                    "record_ref": ref,
                    "grade": grade,
                    "proposed": True,
                    "rationale": "rank_position_heuristic",
                }
            )
        if not labels:
            return None

    content = {
        "schema": PROPOSAL_SCHEMA,
        "pending_record_id": pending.record_id,
        "case_id": payload.get("case_id"),
        "channel": payload.get("channel"),
        "source_id": payload.get("source_id"),
        "scope": asdict(exact_scope),
        "labels": labels,
        "heuristic": heuristic,
        "review_status": REVIEW_STATUS_NEEDS_REVIEW,
        "provenance": {
            "labeler": PROPOSAL_LABELER,
            "origin": "deterministic_heuristic",
            "gold": False,
            "created_at": now_iso(),
        },
    }
    return RecordEnvelope.create(
        kind="evaluation_packet",
        title=f"Auto-label proposal {payload.get('channel') or 'unknown'}",
        summary="Proposed production-query labels awaiting operator review (not gold).",
        content=content,
        source=PROPOSAL_SOURCE,
        source_id=str(payload.get("source_id") or "auto-label"),
        scope=exact_scope,
        status="active",
        meta={
            "report_type": "production_recall_auto_label_proposal",
            "review_status": REVIEW_STATUS_NEEDS_REVIEW,
            "pending_record_id": pending.record_id,
        },
        provenance={
            "origin": "auto_heuristic",
            "labeler": PROPOSAL_LABELER,
            "gold": False,
        },
    )


__all__ = [
    "PROPOSAL_LABELER",
    "PROPOSAL_SCHEMA",
    "PROPOSAL_SOURCE",
    "REVIEW_STATUS_NEEDS_REVIEW",
    "REVIEW_STATUS_PROMOTED",
    "REVIEW_STATUS_REJECTED",
    "list_auto_label_review_queue",
    "promote_auto_label_proposal",
    "propose_auto_labels_for_pending",
]
