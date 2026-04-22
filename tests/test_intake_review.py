from __future__ import annotations

import pytest

from eimemory.intake.review import (
    list_review_queue,
    merge_candidates,
    promote_candidate,
    review_candidate,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


def _store(tmp_path):
    return RuntimeStore(tmp_path)


def _candidate(
    store,
    *,
    title: str = "Candidate note",
    status: str = "candidate",
    scope: ScopeRef | None = None,
):
    record = RecordEnvelope.create(
        kind="knowledge_candidate",
        title=title,
        summary=f"{title} summary",
        detail=f"{title} detail",
        content={
            "text": f"{title} durable memory text",
            "content_excerpt": f"{title} excerpt",
            "summary": f"{title} content summary",
        },
        tags=["intake"],
        evidence=["source-a"],
        source="test.intake",
        scope=scope or ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a"),
        provenance={"source_id": "source-a"},
        meta={"fingerprint": f"fp-{title}", "quality": {"score": 0.8}},
        status=status,
    )
    return store.append(record)


def test_list_review_queue_returns_default_reviewable_statuses(tmp_path):
    store = _store(tmp_path)
    scope = {"tenant_id": "tenant-a", "agent_id": "agent-a", "workspace_id": "repo-a"}
    expected = [
        _candidate(store, title="Ready", status="candidate").record_id,
        _candidate(store, title="Unsafe", status="quarantined").record_id,
        _candidate(store, title="Rejected", status="rejected").record_id,
    ]
    _candidate(store, title="Promoted", status="promoted")

    queue = list_review_queue(store, scope, limit=10)

    assert {item["record_id"] for item in queue} == set(expected)
    assert all(item["kind"] == "knowledge_candidate" for item in queue)
    assert all("quality" in item for item in queue)


def test_approve_then_promote_creates_memory_and_marks_candidate_promoted(tmp_path):
    store = _store(tmp_path)
    scope = {"tenant_id": "tenant-a", "agent_id": "agent-a", "workspace_id": "repo-a"}
    candidate = _candidate(store, title="Promotable")

    reviewed = review_candidate(store, candidate.record_id, "approve", "alice", note="looks good", scope=scope)
    memory = promote_candidate(store, candidate.record_id, "alice", note="ship it", scope=scope)
    promoted_candidate = store.get_by_id(candidate.record_id)

    assert reviewed.status == "reviewed"
    assert reviewed.meta["review_history"][-1]["decision"] == "approve"
    assert memory.kind == "memory"
    assert memory.status == "active"
    assert memory.title == candidate.title
    assert memory.meta["promoted_from"] == candidate.record_id
    assert memory.provenance["promoted_from"] == candidate.record_id
    assert promoted_candidate.status == "promoted"
    assert promoted_candidate.meta["promoted_record_id"] == memory.record_id


def test_reject_updates_status_and_review_history(tmp_path):
    store = _store(tmp_path)
    candidate = _candidate(store, title="Reject me")

    rejected = review_candidate(store, candidate.record_id, "reject", "bob", note="not durable")

    assert rejected.status == "rejected"
    assert rejected.meta["review_history"][-1]["reviewer"] == "bob"
    assert rejected.meta["review_history"][-1]["note"] == "not durable"


def test_merge_marks_source_with_target_id(tmp_path):
    store = _store(tmp_path)
    source = _candidate(store, title="Duplicate source")
    target = _candidate(store, title="Duplicate target")

    merged = merge_candidates(store, source.record_id, target.record_id, "carol", note="same fact")

    assert merged.status == "merged"
    assert merged.meta["merged_into"] == target.record_id
    assert merged.meta["review_history"][-1]["decision"] == "merge"


def test_scope_mismatch_blocks_review_promote_and_merge(tmp_path):
    store = _store(tmp_path)
    tenant_a = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    tenant_b = ScopeRef(tenant_id="tenant-b", agent_id="agent-a", workspace_id="repo-a")
    candidate_a = _candidate(store, title="Tenant A", scope=tenant_a)
    candidate_b = _candidate(store, title="Tenant B", scope=tenant_b)
    wrong_scope = {"tenant_id": "tenant-b", "agent_id": "agent-a", "workspace_id": "repo-a"}

    with pytest.raises(ValueError, match="scope mismatch"):
        review_candidate(store, candidate_a.record_id, "reject", "dana", scope=wrong_scope)
    with pytest.raises(ValueError, match="scope mismatch"):
        promote_candidate(store, candidate_a.record_id, "dana", scope=wrong_scope)
    with pytest.raises(ValueError, match="scope mismatch"):
        merge_candidates(store, candidate_a.record_id, candidate_b.record_id, "dana")


def test_merge_candidates_requires_caller_scope_even_when_records_share_scope(tmp_path):
    store = _store(tmp_path)
    foreign_scope = ScopeRef(tenant_id="tenant-foreign", agent_id="agent-a", workspace_id="repo-a")
    source = _candidate(store, title="Foreign source", scope=foreign_scope)
    target = _candidate(store, title="Foreign target", scope=foreign_scope)
    caller_scope = {"tenant_id": "tenant-local", "agent_id": "agent-a", "workspace_id": "repo-a"}

    with pytest.raises(ValueError, match="scope mismatch"):
        merge_candidates(store, source.record_id, target.record_id, "dana", scope=caller_scope)
