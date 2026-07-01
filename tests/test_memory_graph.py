from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.memory_graph import build_incremental_memory_edges
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_explicit_causal_reference_survives_reference_window_limit(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="hongtu", workspace_id="graph-window")
    cause = runtime.store.append(
        RecordEnvelope.create(
            kind="memory",
            title="Old root cause",
            summary="Old root cause that should remain linkable by explicit id.",
            scope=scope,
            source="test",
        )
    )
    for index in range(170):
        runtime.store.append(
            RecordEnvelope.create(
                kind="memory",
                title=f"Filler {index}",
                summary=f"Filler record {index}",
                scope=scope,
                source="test",
            )
        )
    build_incremental_memory_edges(runtime, scope=scope, limit=200, dry_run=False)
    symptom = runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="New symptom",
            summary="New symptom explicitly points at the old root cause.",
            scope=scope,
            source="test",
            content={"cause_record_id": cause.record_id},
        )
    )

    report = build_incremental_memory_edges(runtime, scope=scope, limit=1, dry_run=False)

    edges = runtime.store.list_memory_edges(scope=scope, edge_types=["causal"], record_ids=[symptom.record_id], limit=20)
    assert report["scanned_count"] == 1
    assert any(edge.from_id == cause.record_id and edge.to_id == symptom.record_id for edge in edges)
