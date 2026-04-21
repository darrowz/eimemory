from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.compatibility.migration_helpers import backup_create
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_governance_snapshot_keeps_scope_isolation_and_surfaces_audit_signals(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    local_scope = {"tenant_id": "tenant-a", "agent_id": "main", "workspace_id": "repo-x", "user_id": "alice"}
    foreign_scope = {"tenant_id": "tenant-b", "agent_id": "main", "workspace_id": "repo-x", "user_id": "bob"}

    runtime.memory.ingest(
        text="Remember concise replies for the operator",
        memory_type="fact",
        title="Local memory",
        scope=local_scope,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="source_candidate",
            title="Local source candidate",
            summary="Scan result for audit review",
            scope=ScopeRef.from_dict(local_scope),
            status="candidate",
            meta={"source_kind": "manual"},
        )
    )
    runtime.evolution.store_rule(
        title="Local active rule",
        summary="Keep responses concise",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=local_scope,
        status="active",
    )
    runtime.evolution.store_rule(
        title="Local accepted rule",
        summary="Accepted after review",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=local_scope,
        status="accepted",
    )
    runtime.evolution.store_rule(
        title="Local candidate rule",
        summary="Still under review",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=local_scope,
        status="candidate",
    )
    runtime.evolution.store_rule(
        title="Local rejected rule",
        summary="Rejected after review",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=local_scope,
        status="rejected",
    )
    runtime.evolution.capture_recall_gap(
        query="local recall gap",
        task_context={"task_type": "chat.reply"},
        scope=local_scope,
        policy={"open_unknown_on_low_confidence": True},
    )

    runtime.memory.ingest(
        text="Foreign memory should stay isolated",
        memory_type="fact",
        title="Foreign memory",
        scope=foreign_scope,
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="source_candidate",
            title="Foreign source candidate",
            summary="Foreign scan result",
            scope=ScopeRef.from_dict(foreign_scope),
            status="candidate",
            meta={"source_kind": "manual"},
        )
    )
    runtime.evolution.store_rule(
        title="Foreign active rule",
        summary="Should not leak",
        task_type="chat.reply",
        retrieval_policy={"route_hint": "task_context_first"},
        scope=foreign_scope,
        status="active",
    )
    runtime.evolution.capture_recall_gap(
        query="foreign recall gap",
        task_context={"task_type": "chat.reply"},
        scope=foreign_scope,
        policy={"open_unknown_on_low_confidence": True},
    )

    from eimemory.governance.snapshot import build_governance_snapshot

    snapshot = build_governance_snapshot(runtime, local_scope)

    assert snapshot["scope"] == local_scope
    assert snapshot["memory_quality"]["memory_count"] == 1
    assert snapshot["memory_quality"]["accepted_count"] == 1
    assert snapshot["reflection_stats"]["reflection_count"] == 1
    assert snapshot["reflection_stats"]["unknown_count"] == 1
    assert snapshot["recall_gaps"]["unknown_count"] == 1
    assert snapshot["recall_gaps"]["latest"]["meta"]["query"] == "local recall gap"
    assert snapshot["rules"] == {
        "active_count": 1,
        "accepted_count": 1,
        "candidate_count": 1,
        "rejected_count": 1,
        "total_count": 4,
    }
    assert snapshot["source_candidates"]["count"] == 1
    assert snapshot["source_candidates"]["latest"]["title"] == "Local source candidate"
    assert [item["title"] for item in snapshot["source_candidates"]["list"]] == ["Local source candidate"]


def test_governance_snapshot_reports_backups_and_health(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "main", "workspace_id": "repo-x"}

    runtime.memory.ingest(
        text="Backup-aware memory",
        memory_type="fact",
        title="Backup memory",
        scope=scope,
    )
    backup_create(runtime, tmp_path / "backups")

    from eimemory.governance.snapshot import build_governance_snapshot

    snapshot = build_governance_snapshot(runtime, scope)

    assert snapshot["backups"]["count"] == 1
    assert snapshot["backups"]["latest"]["verified"] is True
    assert snapshot["health"]["ok"] is True
    assert snapshot["health"]["warnings"] == []
