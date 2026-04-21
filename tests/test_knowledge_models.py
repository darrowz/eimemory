import pytest

from eimemory.api.runtime import Runtime
from eimemory.knowledge.extract import extract_paper_memory
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.entity_records import EntityRecord
from eimemory.models.paper_extracts import PaperExtract
from eimemory.models.relation_records import RelationRecord
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef, VALID_KINDS


def test_knowledge_memory_kinds_are_registered() -> None:
    assert "paper_source" in VALID_KINDS
    assert "paper_extract" in VALID_KINDS
    assert "claim_card" in VALID_KINDS
    assert "entity_record" in VALID_KINDS
    assert "relation_record" in VALID_KINDS
    assert "knowledge_page" in VALID_KINDS
    assert "recall_view" in VALID_KINDS
    assert "incident" in VALID_KINDS


def test_runtime_incident_kind_is_still_allowed() -> None:
    record = RecordEnvelope.create(
        kind="incident",
        title="Runtime incident",
        summary="Existing runtime behavior should remain valid",
        scope=ScopeRef(),
    )

    assert record.kind == "incident"


def test_record_envelope_create_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError, match="invalid record kind"):
        RecordEnvelope.create(
            kind="not_a_kind",
            title="bad record",
            scope=ScopeRef(),
        )


def test_record_envelope_from_dict_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError, match="invalid record kind"):
        RecordEnvelope.from_dict(
            {
                "record_id": "bad_1",
                "kind": "not_a_kind",
                "title": "bad record",
                "scope": {},
            }
        )


def test_record_envelope_direct_construction_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError, match="invalid record kind"):
        RecordEnvelope(
            record_id="bad_2",
            kind="not_a_kind",
            status="active",
            title="bad record",
            summary="",
            detail="",
            content={},
            tags=[],
            links=[],
            evidence=[],
            source="eimemory",
            scope=ScopeRef(),
            time=TimeRef(created_at="t", updated_at="t", occurred_at="t"),
            provenance={},
            meta={},
        )


def test_record_envelope_from_dict_fills_missing_time() -> None:
    record = RecordEnvelope.from_dict(
        {
            "record_id": "mem_123",
            "kind": "memory",
            "title": "timeless record",
            "scope": {},
        }
    )

    assert record.time.created_at
    assert record.time.updated_at
    assert record.time.occurred_at


def test_record_envelope_from_dict_ignores_unknown_time_keys_and_nulls() -> None:
    record = RecordEnvelope.from_dict(
        {
            "record_id": "mem_456",
            "kind": "memory",
            "title": "timeless record",
            "scope": {},
            "time": {
                "created_at": None,
                "updated_at": "2025-01-01T00:00:00Z",
                "timezone": "UTC",
            },
        }
    )

    assert record.time.created_at
    assert record.time.updated_at == "2025-01-01T00:00:00Z"
    assert record.time.occurred_at


def test_extract_paper_memory_returns_claims_entities_and_relations() -> None:
    result = extract_paper_memory(
        paper_source_id="paper_source_test",
        title="Embodied Retrieval",
        abstract="This paper shows compact retrieval improves embodied response quality.",
        body="Method: compact retrieval. Limitation: tested only on one robot.",
    )

    assert isinstance(result.extract, PaperExtract)
    assert result.extract.paper_source_id == "paper_source_test"
    assert result.claims
    assert all(isinstance(claim, ClaimCard) for claim in result.claims)
    assert result.entities
    assert all(isinstance(entity, EntityRecord) for entity in result.entities)
    assert result.relations
    assert all(isinstance(relation, RelationRecord) for relation in result.relations)


def test_structured_memory_records_keep_source_links_and_provenance() -> None:
    result = extract_paper_memory(
        paper_source_id="paper_source_linked",
        title="Retrieval-Augmented Robotics",
        abstract="Retrieval improves robot planning under uncertainty.",
        body="Limitation: requires curated memories.",
    )
    scope = ScopeRef(agent_id="agent-a", workspace_id="lab")
    records = result.to_records(scope=scope)

    assert {record.kind for record in records} >= {
        "paper_extract",
        "claim_card",
        "entity_record",
        "relation_record",
    }
    assert all(record.scope.agent_id == "agent-a" for record in records)
    assert all(record.provenance["paper_source_id"] == "paper_source_linked" for record in records)
    assert all(
        any(link.target_id == "paper_source_linked" for link in record.links)
        for record in records
    )


def test_runtime_extract_paper_memory_persists_structured_records(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        result = runtime.extract_paper_memory(
            {
                "paper_source_id": "paper_source_runtime",
                "title": "Memory Retrieval",
                "abstract": "Hybrid retrieval improves knowledge reuse.",
                "body": "Method: lexical and vector memory. Limitation: small benchmark.",
            },
            scope={"agent_id": "agent-runtime", "workspace_id": "workspace-runtime"},
        )

        assert result.claims
        stored_claims = runtime.store.list_records(
            kinds=["claim_card"],
            scope={"agent_id": "agent-runtime", "workspace_id": "workspace-runtime"},
        )
        stored_entities = runtime.store.list_records(
            kinds=["entity_record"],
            scope={"agent_id": "agent-runtime", "workspace_id": "workspace-runtime"},
        )
        stored_relations = runtime.store.list_records(
            kinds=["relation_record"],
            scope={"agent_id": "agent-runtime", "workspace_id": "workspace-runtime"},
        )

        assert stored_claims
        assert stored_entities
        assert stored_relations
    finally:
        runtime.close()


def test_structured_memory_provenance_cannot_override_canonical_source_id() -> None:
    result = extract_paper_memory(
        paper_source_id="paper_source_canonical",
        title="Source Identity",
        abstract="This paper shows source identity improves migration safety.",
        provenance={"paper_source_id": "paper_source_wrong"},
    )

    records = result.to_records(scope=ScopeRef())

    assert all(record.provenance["paper_source_id"] == "paper_source_canonical" for record in records)
