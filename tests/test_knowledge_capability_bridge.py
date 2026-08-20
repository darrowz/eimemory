from __future__ import annotations

import pytest

from eimemory.capabilities.models import CapabilityDefinition, CapabilityRevision
from eimemory.capabilities.registry import CapabilityRegistry
from eimemory.governance.capability_hypotheses import (
    create_capability_hypothesis,
    hypothesis_behavior_gate,
    list_capability_hypotheses,
    record_hypothesis_experiment_feedback,
    resolve_capability_hypothesis,
)
from eimemory.intake.papers import artifacts as paper_artifacts
from eimemory.knowledge.capabilities import (
    KNOWLEDGE_CAPABILITY_MARKER_KEY,
    assess_knowledge_capability_eligibility,
    list_registered_knowledge_links,
    refresh_capability_applicability_marker,
    register_knowledge_capability_link,
)
from eimemory.knowledge.extract import extract_paper_memory
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import payload_digest
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(
    tenant_id="tenant-knowledge-bridge",
    agent_id="agent-knowledge-bridge",
    workspace_id="workspace-knowledge-bridge",
    user_id="user-knowledge-bridge",
)
STAMP = "2020-08-20T00:00:00+00:00"
CAPABILITY_ID = "planning.evidence_bridge"
REVISION_ID = "planning.evidence_bridge:v1"


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=CAPABILITY_ID,
        display_name="Evidence bridge planning",
        description="A bounded capability used only by the knowledge bridge regression fixture.",
        owner="knowledge-bridge-test",
        risk_tier="bounded_read",
        tags=("knowledge", "planning"),
        provenance={"source": "test"},
        created_at=STAMP,
    )


def _revision() -> CapabilityRevision:
    return CapabilityRevision(
        revision_id=REVISION_ID,
        capability_id=CAPABILITY_ID,
        contract={
            "input_schema": {"type": "object", "required": ["evidence"]},
            "output_schema": {"type": "object", "required": ["decision"]},
            "success_invariants": ["decision_traceable"],
            "failure_invariants": ["unsafe_input_blocked"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "bounded_read",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        provenance={"source": "test"},
        created_at=STAMP,
    )


def _register_capability(store: RuntimeStore) -> None:
    registry = CapabilityRegistry(store)
    registry.register_definition(_definition(), runtime_scope=SCOPE, request_key="knowledge-bridge-definition")
    registry.register_revision(_revision(), runtime_scope=SCOPE, request_key="knowledge-bridge-revision")


def _source_record(*, source_id: str = "paper_knowledge_bridge", artifact: bool = True) -> RecordEnvelope:
    metadata = {
        "artifact": {
            "schema_version": "paper_artifact.v2",
            "status": "ready",
            "text_sha256": "a" * 64,
            "manifest_ref": "artifacts/papers/manifests/fixture.json",
        }
    }
    if not artifact:
        metadata = {}
    return PaperSource(
        paper_source_id=source_id,
        source_kind="pdf",
        title="Knowledge capability bridge fixture",
        pdf_blob_ref="artifacts/papers/pdfs/fixture.pdf" if artifact else "",
        normalized_text_ref="artifacts/papers/text/fixture.txt" if artifact else "",
        metadata=metadata,
        provenance={"source": "test"},
    ).to_record(scope=SCOPE)


def _claim(source_id: str, *, contradicted: bool = False) -> RecordEnvelope:
    content = {
        "paper_source_id": source_id,
        "claim_text": "Canonical evidence supports a bounded planning check.",
    }
    if contradicted:
        content["contradiction_ids"] = ["relation_conflict_1"]
    return RecordEnvelope.create(
        kind="claim_card",
        title="Evidence bridge claim",
        summary=content["claim_text"],
        content=content,
        provenance={"paper_source_id": source_id},
        scope=SCOPE,
        source="test.knowledge_bridge",
    )


def _applicable_link(store: RuntimeStore, claim: RecordEnvelope) -> object:
    return register_knowledge_capability_link(
        store,
        knowledge_record_id=claim.record_id,
        capability_id=CAPABILITY_ID,
        capability_revision_id=REVISION_ID,
        capability_scope="global",
        runtime_scope=SCOPE,
        source_trust="high",
        review_state="approved",
        temporal_validity={"state": "current"},
        environment_constraints={"required": {"runtime": "test"}},
        environment_context={"supported": True, "runtime": "test"},
    )


@pytest.fixture()
def bridge_store(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    monkeypatch.setattr(
        paper_artifacts,
        "load_verified_canonical_text",
        lambda *_args, **_kwargs: "canonical fixture text",
    )
    _register_capability(store)
    try:
        yield store
    finally:
        store.close()


def test_reviewed_canonical_knowledge_links_to_candidate_hypothesis_without_maturity_change(bridge_store) -> None:
    source = _source_record()
    claim = _claim(source.record_id)
    bridge_store.append(source)
    bridge_store.append(claim)
    claim_digest = payload_digest(claim.to_dict())

    result = _applicable_link(bridge_store, claim)

    assert result.link.applicability == "applicable"
    assert result.link.source_status == "active"
    assert result.link.contradiction_state == "none"
    assert len(
        list_registered_knowledge_links(
            bridge_store,
            runtime_scope=SCOPE,
            capability_scope="global",
            capability_id=CAPABILITY_ID,
            capability_revision_id=REVISION_ID,
        )
    ) == 1
    hypothesis = create_capability_hypothesis(
        bridge_store,
        runtime_scope=SCOPE,
        capability_scope="global",
        link_id=result.link.link_id,
        link_digest=result.link.link_digest,
        statement="Use the reviewed source only to create a bounded evaluation candidate.",
        expected_metric={"metric": "pass_rate", "minimum": 0.9},
        environment_context={"supported": True, "runtime": "test"},
        candidate_bounds={"side_effect_class": "none", "max_changes": 0},
    )

    assert hypothesis.status == "candidate"
    assert hypothesis_behavior_gate(
        bridge_store,
        runtime_scope=SCOPE,
        hypothesis_id=hypothesis.record_id,
    )["allowed"] is False
    assert payload_digest(bridge_store.get_by_id(claim.record_id, scope=SCOPE).to_dict()) == claim_digest
    assert bridge_store.read_capabilities(
        lambda repo: repo.list_observations(
            scope=SCOPE,
            capability_scope="global",
            capability_id=CAPABILITY_ID,
            capability_revision_id=REVISION_ID,
        )
    ) == []
    assert bridge_store.list_records(kinds=["capability_score"], scope=SCOPE, limit=10) == []


def test_knowledge_volume_alone_never_emits_observations_or_capability_maturity(bridge_store) -> None:
    for index in range(3):
        source = _source_record(source_id=f"paper_knowledge_volume_{index}")
        claim = _claim(source.record_id)
        bridge_store.append(source)
        bridge_store.append(claim)
        link = _applicable_link(bridge_store, claim).link
        hypothesis = create_capability_hypothesis(
            bridge_store,
            runtime_scope=SCOPE,
            capability_scope="global",
            link_id=link.link_id,
            link_digest=link.link_digest,
            statement=f"Volume fixture {index} remains a non-promoting candidate.",
            expected_metric={"metric": "pass_rate", "minimum": 0.9},
            environment_context={"supported": True, "runtime": "test"},
        )
        assert hypothesis.status == "candidate"

    assert len(
        list_registered_knowledge_links(
            bridge_store,
            runtime_scope=SCOPE,
            capability_scope="global",
            capability_id=CAPABILITY_ID,
            capability_revision_id=REVISION_ID,
        )
    ) == 3
    assert bridge_store.read_capabilities(
        lambda repo: repo.list_observations(
            scope=SCOPE,
            capability_scope="global",
            capability_id=CAPABILITY_ID,
            capability_revision_id=REVISION_ID,
        )
    ) == []
    assert bridge_store.list_records(kinds=["capability_score"], scope=SCOPE, limit=10) == []


@pytest.mark.parametrize(
    ("source_status", "artifact", "contradicted", "review_state", "environment", "expected_applicability"),
    [
        ("rejected", True, False, "approved", {"supported": True, "runtime": "test"}, "rejected"),
        ("stale", True, False, "approved", {"supported": True, "runtime": "test"}, "blocked"),
        ("needs_refresh", True, False, "approved", {"supported": True, "runtime": "test"}, "blocked"),
        ("deprecated", True, False, "approved", {"supported": True, "runtime": "test"}, "blocked"),
        ("conflicted", True, False, "approved", {"supported": True, "runtime": "test"}, "blocked"),
        ("active", False, False, "approved", {"supported": True, "runtime": "test"}, "blocked"),
        ("active", True, True, "approved", {"supported": True, "runtime": "test"}, "blocked"),
        ("active", True, False, "unreviewed", {"supported": True, "runtime": "test"}, "candidate"),
        ("active", True, False, "approved", {"supported": False, "runtime": "test"}, "blocked"),
    ],
)
def test_rejected_stale_conflicted_missing_or_unreviewed_knowledge_never_creates_active_hypothesis(
    bridge_store,
    source_status,
    artifact,
    contradicted,
    review_state,
    environment,
    expected_applicability,
) -> None:
    source = _source_record(source_id=f"paper_{source_status}_{review_state}_{artifact}_{contradicted}", artifact=artifact)
    source.status = source_status
    claim = _claim(source.record_id, contradicted=contradicted)
    bridge_store.append(source)
    bridge_store.append(claim)

    result = register_knowledge_capability_link(
        bridge_store,
        knowledge_record_id=claim.record_id,
        capability_id=CAPABILITY_ID,
        capability_revision_id=REVISION_ID,
        capability_scope="global",
        runtime_scope=SCOPE,
        source_trust="high",
        review_state=review_state,
        environment_constraints={"required": {"runtime": "test"}},
        environment_context=environment,
    )
    hypothesis = create_capability_hypothesis(
        bridge_store,
        runtime_scope=SCOPE,
        capability_scope="global",
        link_id=result.link.link_id,
        link_digest=result.link.link_digest,
        statement="This must remain blocked until the evidence is safe.",
        expected_metric={"metric": "pass_rate", "minimum": 0.9},
        environment_context=environment,
    )

    assert result.link.applicability == expected_applicability
    if not artifact:
        assert result.link.source_status == "unverified"
    assert hypothesis.status == "blocked"
    assert hypothesis_behavior_gate(
        bridge_store,
        runtime_scope=SCOPE,
        hypothesis_id=hypothesis.record_id,
    )["allowed"] is False


def test_expired_temporal_validity_propagates_as_stale_link_evidence(bridge_store) -> None:
    source = _source_record(source_id="paper_expired_knowledge")
    claim = _claim(source.record_id)
    bridge_store.append(source)
    bridge_store.append(claim)

    result = register_knowledge_capability_link(
        bridge_store,
        knowledge_record_id=claim.record_id,
        capability_id=CAPABILITY_ID,
        capability_revision_id=REVISION_ID,
        capability_scope="global",
        runtime_scope=SCOPE,
        source_trust="high",
        review_state="approved",
        temporal_validity={"expires_at": "2000-01-01T00:00:00Z"},
        environment_constraints={"required": {"runtime": "test"}},
        environment_context={"supported": True, "runtime": "test"},
    )

    assert result.link.source_status == "stale"
    assert result.link.applicability == "blocked"
    assert "temporal_expired" in result.assessment.reasons


def test_independent_feedback_is_additive_and_reopens_only_the_verified_trace(bridge_store) -> None:
    source = _source_record()
    claim = _claim(source.record_id)
    bridge_store.append(source)
    bridge_store.append(claim)
    claim_digest = payload_digest(claim.to_dict())
    result = _applicable_link(bridge_store, claim)
    hypothesis = create_capability_hypothesis(
        bridge_store,
        runtime_scope=SCOPE,
        capability_scope="global",
        link_id=result.link.link_id,
        link_digest=result.link.link_digest,
        statement="An independently verified evaluation may authorize the bounded candidate.",
        expected_metric={"metric": "pass_rate", "minimum": 0.9},
        environment_context={"supported": True, "runtime": "test"},
    )
    eval_record = RecordEnvelope.create(
        kind="learning_eval",
        status="passed",
        title="Bridge evaluation",
        summary="pass",
        content={
            "verdict": "pass",
            # Feedback must not be able to attach an unrelated evaluator
            # record after the fact.  The artifact binds itself to this exact
            # knowledge/revision trace before it is eligible evidence.
            "capability_hypothesis": {
                "hypothesis_id": hypothesis.record_id,
                "link_id": result.link.link_id,
                "link_digest": result.link.link_digest,
                "capability_id": CAPABILITY_ID,
                "capability_revision_id": REVISION_ID,
                "capability_scope": "global",
            },
        },
        scope=SCOPE,
        source="test.knowledge_bridge",
    )
    bridge_store.append(eval_record)
    failed = record_hypothesis_experiment_feedback(
        bridge_store,
        runtime_scope=SCOPE,
        hypothesis_id=hypothesis.record_id,
        artifact_type="evaluation",
        artifact_id=eval_record.record_id,
        verdict="fail",
        verifier={"id": "independent_eval", "revision": "v1", "contract_digest": "b" * 64, "independent": True},
    )
    assert failed.content["qualifies_behavior"] is False
    assert hypothesis_behavior_gate(
        bridge_store,
        runtime_scope=SCOPE,
        hypothesis_id=hypothesis.record_id,
    )["allowed"] is False
    passed = record_hypothesis_experiment_feedback(
        bridge_store,
        runtime_scope=SCOPE,
        hypothesis_id=hypothesis.record_id,
        artifact_type="evaluation",
        artifact_id=eval_record.record_id,
        verdict="pass",
        verifier={"id": "independent_eval", "revision": "v1", "contract_digest": "b" * 64, "independent": True},
        details={"result": "independent replayable evaluator pass"},
    )

    gate = hypothesis_behavior_gate(bridge_store, runtime_scope=SCOPE, hypothesis_id=hypothesis.record_id)
    assert passed.content["qualifies_behavior"] is True
    assert gate["allowed"] is True
    assert gate["qualifying_feedback_id"] == passed.record_id
    later_failure = record_hypothesis_experiment_feedback(
        bridge_store,
        runtime_scope=SCOPE,
        hypothesis_id=hypothesis.record_id,
        artifact_type="evaluation",
        artifact_id=eval_record.record_id,
        verdict="fail",
        verifier={"id": "independent_eval", "revision": "v1", "contract_digest": "b" * 64, "independent": True},
        details={"result": "later independent negative result"},
    )
    closed_gate = hypothesis_behavior_gate(bridge_store, runtime_scope=SCOPE, hypothesis_id=hypothesis.record_id)
    assert later_failure.content["qualifies_behavior"] is False
    assert later_failure.content["applicability_feedback"]["effect"] == "restrictive"
    assert closed_gate["allowed"] is False
    assert closed_gate["reason"] == "latest_independent_feedback_not_pass"
    assert payload_digest(bridge_store.get_by_id(claim.record_id, scope=SCOPE).to_dict()) == claim_digest
    resolved = resolve_capability_hypothesis(
        bridge_store,
        runtime_scope=SCOPE,
        hypothesis_id=hypothesis.record_id,
        capability_id=CAPABILITY_ID,
        capability_revision_id=REVISION_ID,
        capability_scope="global",
    )
    assert resolved.record_id == hypothesis.record_id
    assert len(
        list_capability_hypotheses(
            bridge_store,
            runtime_scope=SCOPE,
            capability_id=CAPABILITY_ID,
            capability_revision_id=REVISION_ID,
            capability_scope="global",
        )
    ) == 1


def test_refresh_markers_preserve_resolved_audit_without_direct_contradiction_gate(bridge_store) -> None:
    source = _source_record()
    bridge_store.append(source)
    page = RecordEnvelope.create(
        kind="knowledge_page",
        title="Recompiled knowledge",
        summary="Safe page rebuilt from active claims.",
        content={
            "paper_source_id": source.record_id,
            KNOWLEDGE_CAPABILITY_MARKER_KEY: refresh_capability_applicability_marker(
                "recompiled",
                reason="claim_contradiction",
                resolved_contradiction_ids=["relation_conflict_1"],
            ),
            "resolved_contradiction_ids": ["relation_conflict_1"],
        },
        scope=SCOPE,
        source="test.knowledge_bridge",
    )
    bridge_store.append(page)

    assessment = assess_knowledge_capability_eligibility(
        bridge_store,
        knowledge_record=page.record_id,
        runtime_scope=SCOPE,
        source_trust="high",
        review_state="approved",
        environment_constraints={"required": {"runtime": "test"}},
        environment_context={"supported": True, "runtime": "test"},
    )

    assert "contradiction_ids" not in page.content
    assert assessment.source_status == "active"
    assert assessment.contradiction_state == "resolved"
    assert assessment.applicability == "applicable"


def test_claim_and_relation_context_requires_explicit_capability_identity() -> None:
    context = {
        "capability_id": CAPABILITY_ID,
        "capability_revision_id": REVISION_ID,
        "capability_scope": "global",
        "relation_type": "informs_eval",
    }
    extraction = extract_paper_memory(
        paper_source_id="paper_explicit_context",
        title="Planning Evidence",
        abstract="The method improves planning evidence traceability.",
        body="The method improves planning evidence traceability under bounded tests.",
        capability_context=context,
    )

    assert extraction.claims
    assert all(claim.metadata["capability_context"]["capability_revision_id"] == REVISION_ID for claim in extraction.claims)
    assert all(claim.provenance["capability_context"]["capability_id"] == CAPABILITY_ID for claim in extraction.claims)
    assert extraction.relations
    assert all(relation.metadata["capability_context"]["relation_type"] == "informs_eval" for relation in extraction.relations)
