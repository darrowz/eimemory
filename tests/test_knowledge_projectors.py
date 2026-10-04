from dataclasses import asdict
import hashlib
import json

from eimemory.storage.runtime_store import RuntimeStore
from eimemory.knowledge.projectors import SUPPORT_LINEAGE_SCHEMA, project_operational_knowledge, stable_projection_id
from eimemory.models.knowledge_pages import KnowledgePage
from eimemory.models.records import RecordEnvelope, ScopeRef


def _operational_page(*, page_id: str, scope: ScopeRef, store: RuntimeStore) -> RecordEnvelope:
    # Explicit synthetic new-contract producer; the native compiler does not
    # emit this lineage yet and its old-format pages are deliberately skipped.
    claim = RecordEnvelope.create(
        kind="claim_card", title="Verified runtime policy", scope=scope,
        summary="EIBrain runtime recall should prefer verified memory records with explicit provenance.",
        content={"claim_text": "EIBrain runtime recall should prefer verified memory records with explicit provenance.",
                 "confidence": 0.92}, meta={"confidence": 0.92},
    )
    claim.record_id = f"claim_{page_id}"
    store.append(claim)
    page = KnowledgePage(
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
    page.content["supporting_claim_refs_schema"] = SUPPORT_LINEAGE_SCHEMA
    page.content["supporting_claim_refs"] = [{
        "record_id": claim.record_id, "kind": claim.kind, "scope": asdict(claim.scope),
        "source_id": claim.source_id,
        "version_digest": hashlib.sha256(json.dumps(claim.to_dict(), ensure_ascii=False,
            sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest(),
    }]
    return page


def test_claim_card_projects_to_memory_and_dedupes(tmp_path) -> None:
    runtime = RuntimeStore(tmp_path)
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
        runtime.append(claim)

        first = project_operational_knowledge(runtime, scope=scope)
        second = project_operational_knowledge(runtime, scope=scope)

        memories = runtime.list_records(kinds=["memory"], scope=scope, limit=10)
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


def test_legacy_knowledge_page_without_explicit_lineage_is_skipped(tmp_path) -> None:
    runtime = RuntimeStore(tmp_path)
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
        runtime.append(page)

        report = project_operational_knowledge(runtime, scope=scope)

        memories = runtime.list_records(kinds=["memory"], scope=scope, limit=10)
        assert report["projected_count"] == 0
        assert memories == []
        assert report["skipped"] == [{"record_id": page.record_id, "reason": "missing_support_lineage"}]
    finally:
        runtime.close()


def test_projection_create_rechecks_source_version_inside_write_transaction(tmp_path) -> None:
    runtime = RuntimeStore(tmp_path)
    other = RuntimeStore(tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "agent-proj", "workspace_id": "create-race"})
    page = _operational_page(page_id="page_projection_create_race", scope=scope, store=runtime)
    project_operational_knowledge(runtime, scope=asdict(scope))
    runtime.append(page)
    original_mutate = runtime.mutate_records_atomically

    def race(mutation):
        changed = other.get_by_id(page.record_id, scope=scope)
        assert changed is not None
        changed.summary = "EIBrain runtime recall now uses a different verified operational policy version."
        changed.touch()
        other.rewrite(changed)
        return original_mutate(mutation)

    runtime.mutate_records_atomically = race
    try:
        report = project_operational_knowledge(runtime, scope=asdict(scope))
        projection = runtime.get_by_id(stable_projection_id(page), scope=scope)

        assert report["projected_count"] == 0
        assert {item["reason"] for item in report["skipped"]} == {"source_changed", "already_projected"}
        assert projection is None
    finally:
        other.close()
        runtime.close()


def test_projection_reactivation_rechecks_source_status_inside_write_transaction(tmp_path) -> None:
    runtime = RuntimeStore(tmp_path)
    other = RuntimeStore(tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "agent-proj", "workspace_id": "reactivation-race"})
    page = runtime.append(_operational_page(page_id="page_projection_reactivation_race", scope=scope, store=runtime))
    first = project_operational_knowledge(runtime, scope=asdict(scope))
    projection_id = stable_projection_id(page)
    projection = runtime.get_by_id(projection_id, scope=scope)
    assert first["projected_count"] == 2
    assert projection is not None
    projection.status = "deprecated"
    projection.touch()
    runtime.rewrite(projection)
    original_mutate = runtime.mutate_records_atomically

    def race(mutation):
        changed = other.get_by_id(page.record_id, scope=scope)
        assert changed is not None
        changed.status = "needs_refresh"
        changed.touch()
        other.rewrite(changed)
        return original_mutate(mutation)

    runtime.mutate_records_atomically = race
    try:
        report = project_operational_knowledge(runtime, scope=asdict(scope))
        final_projection = runtime.get_by_id(projection_id, scope=scope)

        assert report["projected_count"] == 0
        assert {item["reason"] for item in report["skipped"]} == {"source_changed", "already_projected"}
        assert final_projection is not None
        assert final_projection.status == "deprecated"
    finally:
        other.close()
        runtime.close()


def test_projection_skips_low_quality_and_contradicted_content(tmp_path) -> None:
    runtime = RuntimeStore(tmp_path)
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
        runtime.append(low_confidence)
        runtime.append(contradicted)
        runtime.append(deprecated_page)

        report = project_operational_knowledge(runtime, scope=scope)

        memories = runtime.list_records(kinds=["memory"], scope=scope, limit=10)
        assert report["projected_count"] == 0
        assert memories == []
        assert report["skipped_count"] == 3
    finally:
        runtime.close()
