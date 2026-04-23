from eimemory.api.runtime import Runtime
from eimemory.models.knowledge_pages import KnowledgePage
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_claim_card_projects_to_memory_and_dedupes(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-proj", "workspace_id": "ops"}
    try:
        claim = RecordEnvelope.create(
            kind="claim_card",
            title="OpenClaw recall policy",
            summary="OpenClaw memory recall must prioritize tenant-scoped verified operational decisions.",
            detail="Evaluation showed tenant-scoped memories reduced cross-project leakage.",
            content={
                "claim_text": "OpenClaw memory recall must prioritize tenant-scoped verified operational decisions.",
                "confidence": 0.92,
                "claim_type": "finding",
            },
            scope=ScopeRef.from_dict(scope),
            source="test",
            meta={"confidence": 0.92, "claim_type": "finding"},
            provenance={"paper_source_id": "paper_projection"},
        )
        runtime.store.append(claim)

        first = runtime.project_operational_knowledge(scope=scope)
        second = runtime.project_operational_knowledge(scope=scope)

        memories = runtime.store.list_records(kinds=["memory"], scope=scope, limit=10)
        assert first["projected_count"] == 1
        assert second["projected_count"] == 0
        assert len(memories) == 1
        assert memories[0].provenance["source_record_id"] == claim.record_id
        assert memories[0].meta["projection_type"] == "operational_knowledge"
        assert memories[0].meta["projection_reason"] == "high_confidence_operational_claim"
        assert memories[0].meta["projection_score"] >= 0.75
        assert memories[0].content["memory_type"] == "fact"
    finally:
        runtime.close()


def test_knowledge_page_projects_to_memory(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-proj", "workspace_id": "pages"}
    try:
        page = KnowledgePage(
            knowledge_page_id="page_projection_runtime",
            page_type="topic",
            title="EIBrain runtime policy",
            summary="EIBrain runtime recall should prefer verified memory records with explicit provenance.",
            sections=(
                {
                    "name": "runtime",
                    "text": "The runtime policy should keep compiled knowledge as memory-only recall hints.",
                },
            ),
            supporting_claim_ids=("claim_projection_runtime",),
            source_ids=("paper_projection_runtime",),
        ).to_record(scope=ScopeRef.from_dict(scope))
        runtime.store.append(page)

        report = runtime.project_operational_knowledge(scope=scope)

        memories = runtime.store.list_records(kinds=["memory"], scope=scope, limit=10)
        assert report["projected_count"] == 1
        assert len(memories) == 1
        assert memories[0].provenance["source_record_kind"] == "knowledge_page"
        assert memories[0].links[0].target_id == page.record_id
        assert memories[0].meta["projection_reason"] == "operational_knowledge_page"
    finally:
        runtime.close()


def test_projection_skips_low_quality_and_contradicted_content(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-proj", "workspace_id": "skip"}
    try:
        low_confidence = RecordEnvelope.create(
            kind="claim_card",
            title="Maybe useful",
            summary="Maybe useful.",
            content={"claim_text": "Maybe useful.", "confidence": 0.2},
            scope=ScopeRef.from_dict(scope),
            meta={"confidence": 0.2},
        )
        contradicted = RecordEnvelope.create(
            kind="claim_card",
            title="Contradicted policy",
            summary="OpenClaw memory recall must always use this contradicted policy.",
            content={
                "claim_text": "OpenClaw memory recall must always use this contradicted policy.",
                "confidence": 0.95,
                "contradiction_claim_ids": ["claim_other"],
            },
            scope=ScopeRef.from_dict(scope),
            status="conflicted",
            meta={"confidence": 0.95, "contradiction_claim_ids": ["claim_other"]},
        )
        deprecated_page = KnowledgePage(
            knowledge_page_id="page_deprecated_projection",
            page_type="topic",
            title="Deprecated page",
            summary="OpenClaw runtime should prefer this deprecated memory projection rule.",
            source_ids=("paper_deprecated",),
        ).to_record(scope=ScopeRef.from_dict(scope))
        deprecated_page.status = "deprecated"
        runtime.store.append(low_confidence)
        runtime.store.append(contradicted)
        runtime.store.append(deprecated_page)

        report = runtime.project_operational_knowledge(scope=scope)

        memories = runtime.store.list_records(kinds=["memory"], scope=scope, limit=10)
        assert report["projected_count"] == 0
        assert memories == []
        assert report["skipped_count"] == 3
    finally:
        runtime.close()
