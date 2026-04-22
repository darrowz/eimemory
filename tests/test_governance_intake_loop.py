from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.governance.snapshot import build_governance_snapshot
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_governance_snapshot_reports_knowledge_intake_candidates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(tenant_id="tenant-a", agent_id="main", workspace_id="repo-x", user_id="alice")
    foreign_scope = ScopeRef(tenant_id="tenant-b", agent_id="main", workspace_id="repo-x", user_id="bob")

    runtime.store.append(
        _with_time(
            RecordEnvelope.create(
                kind="source_candidate",
                title="Paper candidate",
                summary="Potential reusable paper knowledge",
                scope=scope,
                status="candidate",
                meta={
                    "intake_decision": "accept",
                    "source_kind": "paper",
                    "source_id": "paper-1",
                    "fingerprint": "fp-1",
                    "quality": {"score": 0.91},
                    "provenance": {"url": "https://example.test/paper"},
                },
            ),
            "2026-04-23T09:00:00+00:00",
        )
    )
    runtime.store.append(
        _with_time(
            RecordEnvelope.create(
                kind="source_candidate",
                title="Web candidate",
                summary="Potential reusable web knowledge",
                scope=scope,
                status="candidate",
                meta={
                    "intake_decision": "accept",
                    "source_kind": "web",
                    "source_id": "web-1",
                    "fingerprint": "fp-2",
                    "quality": {"score": 0.82},
                    "provenance": {"url": "https://example.test/web"},
                },
            ),
            "2026-04-23T10:00:00+00:00",
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="source_candidate",
            title="Quarantined candidate",
            summary="Needs review before ingest",
            scope=scope,
            status="quarantined",
            meta={
                "intake_decision": "quarantine",
                "source_kind": "web",
                "source_id": "web-2",
                "fingerprint": "fp-3",
                "quality": {"score": 0.34},
                "provenance": {"url": "https://example.test/quarantine"},
            },
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="source_candidate",
            title="Rejected candidate",
            summary="Rejected during intake",
            scope=scope,
            status="rejected",
            meta={
                "intake_decision": "reject",
                "source_kind": "manual",
                "source_id": "manual-1",
                "fingerprint": "fp-4",
                "quality": {"score": 0.12},
                "provenance": {"note": "low quality"},
            },
        )
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="source_candidate",
            title="Foreign candidate",
            summary="Should not leak across scopes",
            scope=foreign_scope,
            status="candidate",
            meta={
                "intake_decision": "accept",
                "source_kind": "paper",
                "source_id": "paper-foreign",
                "fingerprint": "fp-foreign",
                "quality": {"score": 0.88},
                "provenance": {"url": "https://example.test/foreign"},
            },
        )
    )

    snapshot = build_governance_snapshot(runtime, scope)

    assert "knowledge_intake" in snapshot
    assert snapshot["knowledge_intake"]["count"] == 4
    assert snapshot["knowledge_intake"]["candidate_count"] == 2
    assert snapshot["knowledge_intake"]["quarantined_count"] == 1
    assert snapshot["knowledge_intake"]["rejected_count"] == 1
    assert snapshot["knowledge_intake"]["by_source_kind"] == {
        "manual": 1,
        "paper": 1,
        "web": 2,
    }
    assert [item["title"] for item in snapshot["knowledge_intake"]["recent_candidates"]] == [
        "Web candidate",
        "Paper candidate",
    ]


def _with_time(record: RecordEnvelope, timestamp: str) -> RecordEnvelope:
    record.time.created_at = timestamp
    record.time.updated_at = timestamp
    record.time.occurred_at = timestamp
    return record
