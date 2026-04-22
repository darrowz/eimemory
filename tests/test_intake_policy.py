from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.intake.policy import build_source_quality_report, recommend_collection_policy
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_source_quality_report_groups_counts_scores_and_last_seen(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    other_scope = ScopeRef(tenant_id="tenant-b", agent_id="agent-a", workspace_id="repo-a")
    try:
        runtime.store.append(
            _with_time(
                _candidate(scope, "paper", "good-paper", "candidate", 0.86),
                "2026-04-23T01:00:00+00:00",
            )
        )
        runtime.store.append(
            _with_time(
                _memory(scope, "paper", "good-paper", 0.92),
                "2026-04-23T03:00:00+00:00",
            )
        )
        runtime.store.append(
            _with_time(
                _candidate(scope, "rss", "noisy-feed", "rejected", 0.11),
                "2026-04-23T02:00:00+00:00",
            )
        )
        runtime.store.append(
            _with_time(
                _candidate(scope, "rss", "unsafe-feed", "quarantined", 0.0),
                "2026-04-23T04:00:00+00:00",
            )
        )
        runtime.store.append(_candidate(other_scope, "paper", "foreign", "candidate", 0.99))

        report = build_source_quality_report(runtime, scope)

        good = report["by_source"]["paper"]["good-paper"]
        assert good["candidate_count"] == 1
        assert good["promoted_count"] == 1
        assert good["rejected_count"] == 0
        assert good["quarantined_count"] == 0
        assert good["avg_quality_score"] == 0.89
        assert good["last_seen"] == "2026-04-23T03:00:00+00:00"

        assert report["by_source"]["rss"]["noisy-feed"]["rejected_count"] == 1
        assert report["by_source"]["rss"]["unsafe-feed"]["quarantined_count"] == 1
        assert "foreign" not in report["by_source"].get("paper", {})
    finally:
        runtime.close()


def test_recommend_collection_policy_uses_quality_rules_and_gap_queries(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="repo-a")
    try:
        runtime.store.append(_candidate(scope, "paper", "strong-paper", "candidate", 0.87))
        runtime.store.append(_memory(scope, "paper", "strong-paper", 0.93))
        runtime.store.append(_candidate(scope, "rss", "unsafe-feed", "quarantined", 0.0))
        runtime.store.append(_candidate(scope, "rss", "unsafe-feed", "quarantined", 0.0))
        runtime.store.append(_candidate(scope, "url", "thin-site", "rejected", 0.18))
        runtime.store.append(_candidate(scope, "url", "thin-site", "rejected", 0.22))
        runtime.store.append(
            RecordEnvelope.create(
                kind="unknown",
                title="Need better transformer memory evidence",
                summary="Recall missed transformer memory evidence.",
                scope=scope,
            )
        )

        policy = recommend_collection_policy(
            runtime,
            scope,
            topic_gaps=["low latency retrieval benchmarks"],
        )

        assert policy["run_now"] == ["strong-paper"]
        assert policy["pause"] == ["unsafe-feed"]
        assert policy["lower_frequency"] == ["thin-site"]
        assert "low latency retrieval benchmarks" in policy["gap_queries"]
        assert "Need better transformer memory evidence" in policy["gap_queries"]
    finally:
        runtime.close()


def _candidate(
    scope: ScopeRef,
    source_kind: str,
    source_id: str,
    status: str,
    score: float,
) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="knowledge_candidate",
        title=f"{source_id} {status}",
        summary=f"{source_id} {status} summary",
        detail=f"{source_id} {status} detail",
        scope=scope,
        status=status,
        meta={
            "source_kind": source_kind,
            "source_id": source_id,
            "quality": {"score": score},
        },
        content={
            "source_kind": source_kind,
            "source_id": source_id,
            "quality": {"score": score},
        },
    )


def _memory(scope: ScopeRef, source_kind: str, source_id: str, score: float) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title=f"{source_id} promoted memory",
        summary=f"{source_id} promoted memory summary",
        detail=f"{source_id} promoted memory detail",
        scope=scope,
        meta={
            "source_kind": source_kind,
            "source_id": source_id,
            "quality": {"score": score},
        },
        content={
            "source_kind": source_kind,
            "source_id": source_id,
            "text": f"{source_id} promoted durable knowledge",
        },
    )


def _with_time(record: RecordEnvelope, timestamp: str) -> RecordEnvelope:
    record.time.created_at = timestamp
    record.time.updated_at = timestamp
    record.time.occurred_at = timestamp
    return record
