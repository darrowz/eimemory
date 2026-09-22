from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.evaluation.auto_label_proposals import (
    PROPOSAL_LABELER,
    REVIEW_STATUS_NEEDS_REVIEW,
    REVIEW_STATUS_PROMOTED,
    list_auto_label_review_queue,
    promote_auto_label_proposal,
    propose_auto_labels_for_pending,
)
from eimemory.evaluation.production_query_dataset import PENDING_QUERY_SCHEMA, PENDING_SOURCE
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow", "tenant_id": "default"}


def _pending(runtime: Runtime, *, refs: list[str]) -> RecordEnvelope:
    memory = runtime.memory.ingest(
        text="Production recall candidate memory about Feishu channel.",
        memory_type="fact",
        title="Feishu channel fact",
        scope=SCOPE,
        source="openclaw.agent_end",
        source_id="src-1",
    )
    refs = refs or [memory.record_id]
    pending = RecordEnvelope.create(
        kind="evaluation_packet",
        title="Pending production recall label openclaw",
        summary="Digest-only real proactive query awaiting operator relevance labels.",
        content={
            "schema": PENDING_QUERY_SCHEMA,
            "case_id": "real-auto-label-case",
            "channel": "openclaw",
            "source_id": "src-1",
            "scope": SCOPE,
            "capture_query_digest": "a" * 64,
            "candidate_refs": refs,
            "capture_ref": "decision-1",
        },
        source=PENDING_SOURCE,
        source_id="src-1",
        scope=ScopeRef.from_dict(SCOPE),
        status="active",
        meta={"report_type": "production_recall_pending_case"},
    )
    return runtime.store.append(pending)


def test_auto_label_proposals_are_not_gold_and_enter_review_queue(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    pending = _pending(runtime, refs=[])
    report = propose_auto_labels_for_pending(runtime, scope=SCOPE, limit=10)
    assert report["ok"] is True
    assert report["created"] >= 1
    assert report["review_queue_status"] == REVIEW_STATUS_NEEDS_REVIEW

    queue = list_auto_label_review_queue(runtime, scope=SCOPE)
    assert queue["count"] >= 1
    item = queue["items"][0]
    assert item["review_status"] == REVIEW_STATUS_NEEDS_REVIEW
    assert item["pending_record_id"] == pending.record_id
    assert item["provenance"]["labeler"] == PROPOSAL_LABELER
    assert item["provenance"]["gold"] is False

    promoted = promote_auto_label_proposal(
        runtime,
        proposal_record_id=item["proposal_record_id"],
        operator_id="operator",
    )
    assert promoted["review_status"] == REVIEW_STATUS_PROMOTED
    assert "gold" not in str(promoted.get("labels"))
    assert "accept" in promoted["next_step"]
