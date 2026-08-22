from dataclasses import asdict

from eimemory.api.runtime import Runtime
from eimemory.knowledge.projectors import stable_projection_id
from eimemory.models.knowledge_pages import KnowledgePage
from eimemory.models.records import RecordEnvelope, ScopeRef


def _operational_page(*, page_id: str, scope: ScopeRef) -> RecordEnvelope:
    return KnowledgePage(
        knowledge_page_id=page_id,
        page_type="topic",
        title="EIBrain runtime policy",
        summary="EIBrain runtime recall should prefer verified memory records with explicit provenance.",
        sections=(
            {
                "name": "runtime",
                "text": "The runtime policy should keep compiled knowledge as memory-only recall hints.",
            },
        ),
        supporting_claim_ids=(f"claim_{page_id}",),
        source_ids=(f"paper_{page_id}",),
    ).to_record(scope=scope)


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


def test_projection_create_rechecks_source_version_inside_write_transaction(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    other = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "agent-proj", "workspace_id": "create-race"})
    page = runtime.store.append(_operational_page(page_id="page_projection_create_race", scope=scope))
    original_mutate = runtime.store.mutate_records_atomically

    def race(mutation):
        changed = other.store.get_by_id(page.record_id, scope=scope)
        assert changed is not None
        changed.summary = "EIBrain runtime recall now uses a different verified operational policy version."
        changed.touch()
        other.store.rewrite(changed)
        return original_mutate(mutation)

    runtime.store.mutate_records_atomically = race
    try:
        report = runtime.project_operational_knowledge(scope=asdict(scope))
        projection = runtime.store.get_by_id(stable_projection_id(page), scope=scope)

        assert report["projected_count"] == 0
        assert {item["reason"] for item in report["skipped"]} == {"source_changed"}
        assert projection is None
    finally:
        other.close()
        runtime.close()


def test_projection_reactivation_rechecks_source_status_inside_write_transaction(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    other = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "agent-proj", "workspace_id": "reactivation-race"})
    page = runtime.store.append(_operational_page(page_id="page_projection_reactivation_race", scope=scope))
    first = runtime.project_operational_knowledge(scope=asdict(scope))
    projection_id = stable_projection_id(page)
    projection = runtime.store.get_by_id(projection_id, scope=scope)
    assert first["projected_count"] == 1
    assert projection is not None
    projection.status = "deprecated"
    projection.touch()
    runtime.store.rewrite(projection)
    original_mutate = runtime.store.mutate_records_atomically

    def race(mutation):
        changed = other.store.get_by_id(page.record_id, scope=scope)
        assert changed is not None
        changed.status = "needs_refresh"
        changed.touch()
        other.store.rewrite(changed)
        return original_mutate(mutation)

    runtime.store.mutate_records_atomically = race
    try:
        report = runtime.project_operational_knowledge(scope=asdict(scope))
        final_projection = runtime.store.get_by_id(projection_id, scope=scope)

        assert report["projected_count"] == 0
        assert {item["reason"] for item in report["skipped"]} == {"source_changed"}
        assert final_projection is not None
        assert final_projection.status == "deprecated"
    finally:
        other.close()
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
