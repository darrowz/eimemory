from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_scope
from eimemory.intake.autonomous_sources import run_autonomous_source_expansion
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_autonomous_source_expansion_adds_llm_approved_chatpaper_categories(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = hongtu_scope({})
    runtime.sources.add_source(
        {
            "source_kind": "url",
            "title": "ChatPaper arXiv cs.AI",
            "uri": "https://www.chatpaper.ai/zh/dashboard/arxiv/cs/AI",
            "enabled": True,
            "tags": ["chatpaper", "arxiv", "paper"],
            "metadata": {"categories": ["cs.AI"], "max_items": 10},
        }
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="unknown",
            title="Need better embodied robotics and visual grounding papers",
            summary="Recall missed embodied robotics and visual grounding papers.",
            detail="Recall missed embodied robotics and visual grounding papers.",
            scope=ScopeRef.from_dict(scope),
        )
    )

    def approving_evaluator(proposal: dict, context: dict) -> dict:
        assert context["gap_queries"]
        return {"decision": "approve", "score": 0.91, "reason": "covers current embodied AI gaps"}

    report = run_autonomous_source_expansion(
        runtime,
        scope=scope,
        apply=True,
        evaluator=approving_evaluator,
        max_apply=2,
    )
    source = runtime.sources.list_sources()[0]
    audit_records = runtime.store.list_records(kinds=["source_candidate"], scope=scope, limit=10)
    rerun = run_autonomous_source_expansion(
        runtime,
        scope=scope,
        apply=True,
        evaluator=approving_evaluator,
        max_apply=2,
    )
    runtime.close()

    assert report["ok"] is True
    assert report["proposal_count"] >= 2
    assert report["approved_count"] >= 2
    assert report["applied_count"] == 2
    assert {"cs.AI", "cs.CV", "cs.RO"}.issubset(set(source.metadata["categories"]))
    assert source.metadata["autonomous_expansion"]["applied_count"] == 2
    assert len(report["audit_record_ids"]) == 2
    assert all(record.source == "eimemory.autonomous_source_expansion" for record in audit_records)
    assert rerun["applied_count"] == 0
    assert rerun["duplicate_count"] >= 2


def test_autonomous_source_expansion_rejects_low_score_llm_proposals_without_applying(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = hongtu_scope({})
    runtime.sources.add_source(
        {
            "source_kind": "url",
            "title": "ChatPaper arXiv cs.AI",
            "uri": "https://www.chatpaper.ai/zh/dashboard/arxiv/cs/AI",
            "enabled": True,
            "tags": ["chatpaper", "arxiv", "paper"],
            "metadata": {"categories": ["cs.AI"], "max_items": 10},
        }
    )
    runtime.store.append(
        RecordEnvelope.create(
            kind="unknown",
            title="Need robotics source expansion",
            summary="Recall missed robotics source expansion.",
            detail="Recall missed robotics source expansion.",
            scope=ScopeRef.from_dict(scope),
        )
    )

    def rejecting_evaluator(_proposal: dict, _context: dict) -> dict:
        return {"decision": "reject", "score": 0.2, "reason": "not trustworthy enough"}

    report = run_autonomous_source_expansion(
        runtime,
        scope=scope,
        apply=True,
        evaluator=rejecting_evaluator,
        max_apply=2,
    )
    source = runtime.sources.list_sources()[0]
    audit_records = runtime.store.list_records(kinds=["source_candidate"], scope=scope, status="rejected", limit=10)
    runtime.close()

    assert report["proposal_count"] >= 1
    assert report["approved_count"] == 0
    assert report["applied_count"] == 0
    assert report["rejected_count"] >= 1
    assert source.metadata["categories"] == ["cs.AI"]
    assert audit_records
    assert audit_records[0].meta["evaluation"]["decision"] == "reject"
