from types import SimpleNamespace
import pytest
from eimemory.knowledge.ingest import ingest_knowledge_source
from eimemory.knowledge.evidence_gate import grade_research_evidence


@pytest.mark.parametrize("carrier", ["meta", "content", "provenance"])
def test_conflicting_nested_kind_rejected_at_actual_ingress(carrier):
    payload = {"source_kind": "docs", "text": "fixture documentation", carrier: {"source_kind": "custom"}}
    with pytest.raises(ValueError, match="conflicting source_kind"):
        ingest_knowledge_source(SimpleNamespace(), payload, persist=False)


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), True, 10**1000])
def test_invalid_confidence_cannot_pass_research_gate(confidence):
    result = grade_research_evidence({"kind": "claim_card", "status": "active",
        "content": {"source_url": "https://fixture.invalid/", "published_at": "2026-10-03", "confidence": confidence}})
    assert result["ok"] is False
