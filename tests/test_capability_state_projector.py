from __future__ import annotations

from pathlib import Path

from eimemory.capabilities.applicability import evaluate_applicability
from eimemory.capabilities.models import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityKnowledgeLink,
    CapabilityObservation,
    CapabilityProfile,
    CapabilityRelation,
    CapabilityRevision,
)
from eimemory.capabilities.observations import CapabilityObservations
from eimemory.capabilities.profiles import CapabilityProfiles
from eimemory.capabilities.projector import CapabilityStateProjector
from eimemory.capabilities.registry import CapabilityRegistry
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(
    tenant_id="projector-tenant",
    agent_id="projector-agent",
    workspace_id="projector-workspace",
    user_id="projector-user",
)
# Keep durable-registration fixtures safely in the past.  Capability writes
# intentionally reject future descriptors, so a date tied to the calendar
# eventually turns this projection test into a clock-bound false failure.
STAMP = "2024-08-20T00:00:00+00:00"


def test_stale_knowledge_context_caps_applicability_fail_closed() -> None:
    decision = evaluate_applicability(
        capability_scope="global",
        binding_descriptor={
            "binding_id": "binding.applicability",
            "binding_digest": "a" * 64,
            "applicability": {"scope": "global"},
        },
        binding_status="active",
        observations=(
            {
                "observation_id": "observation.applicability",
                "observed_at": "2024-08-20T00:00:01+00:00",
                "payload": {"environment_fingerprint": {"target": "portable"}},
            },
        ),
        knowledge_links=(
            {
                "link_id": "knowledge.stale-context",
                "link_digest": "b" * 64,
                "payload": {
                    "link_id": "knowledge.stale-context",
                    "link_digest": "b" * 64,
                    "relation_type": "supports",
                    "source_status": "needs_refresh",
                    "applicability": "rejected",
                    "review_state": "reviewed",
                    "contradiction_state": "none",
                    "temporal_validity": {"valid_from": STAMP},
                    "environment_constraints": {"scope": "global"},
                    "created_at": STAMP,
                    "evidence_refs": ["knowledge.stale-context"],
                    "applicability_evidence_refs": ["knowledge.stale-context"],
                },
            },
        ),
        requirement={"minimum_maturity": "reliable"},
        at_time="2024-08-20T00:01:00+00:00",
    )

    assert decision["status"] == "stale"
    assert decision["maturity_ceiling"] == "observed"
    assert "knowledge_source_stale" in decision["reason_codes"]


def _definition(capability_id: str) -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=capability_id,
        display_name=capability_id.replace(".", " ").title(),
        description=f"Evidence-backed {capability_id} capability.",
        owner="projector-test",
        created_at=STAMP,
        risk_tier="low",
        tags=("projector",),
        provenance={"source": "projector-test"},
    )


def _revision(
    definition: CapabilityDefinition,
    *,
    dependencies: tuple[str, ...] = (),
    name: str = "v1",
) -> CapabilityRevision:
    return CapabilityRevision(
        revision_id=f"{definition.capability_id}:{name}",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "success_invariants": ["deterministic_success"],
            "failure_invariants": ["deterministic_failure"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": list(dependencies),
            "composition": [],
            "risk_tier": "low",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        created_at=STAMP,
        provenance={"source": "projector-test"},
    )


def _binding(
    definition: CapabilityDefinition,
    revision: CapabilityRevision,
    *,
    name: str,
    implementation: str = "a" * 64,
    environment: str = "one",
) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=f"binding.{name}.{definition.capability_id}:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id=f"module-{name}",
        implementation_digest=implementation,
        operations=("execute",),
        limits={"max_items": 16},
        environment_fingerprint={"environment": environment},
        applicability={"scope": "global", "environment_dependent": False},
        advertisement_evidence_refs=(f"artifact://advertisements/{name}.json",),
        provenance={"source": "projector-test"},
        created_at=STAMP,
    )


def _observation(
    definition: CapabilityDefinition,
    revision: CapabilityRevision,
    binding: CapabilityBinding,
    *,
    name: str,
    verdict: str = "pass",
    observed_at: str = "2024-08-20T00:00:01+00:00",
) -> CapabilityObservation:
    return CapabilityObservation(
        observation_id=f"observation.{name}",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_binding_id=binding.binding_id,
        idempotency_key=f"projector:{name}",
        verdict=verdict,
        source="projector-test",
        executor_id="projector-executor",
        executor_contract_digest="1" * 64,
        grader_id="projector-grader",
        grader_revision="projector-grader:v1",
        input_digest="2" * 64,
        output_digest="3" * 64,
        evidence_digest="4" * 64,
        evidence_refs=(f"artifact://evidence/{name}.json",),
        environment_fingerprint={"environment": "portable"},
        provenance={"source": "projector-test", "environment_dependent": False},
        metrics={"success": 1.0 if verdict == "pass" else 0.0},
        error_taxonomy={} if verdict == "pass" else {"failure": "independent_failure"},
        observed_at=observed_at,
        scope="global",
    )


def _profile(*capability_ids: str) -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="profile.projector:v1",
        profile_key="projector-profile",
        requirements={
            capability_id: {
                "minimum_maturity": "reliable",
                "min_evidence_count": 1,
                "min_sample_count": 1,
                "min_pass_rate": 1.0,
                "min_consecutive_passes": 1,
                "require_dependencies": True,
            }
            for capability_id in capability_ids
        },
        created_at=STAMP,
        provenance={"source": "projector-test"},
    )


def _register(store: RuntimeStore, *entities: object) -> None:
    registry = CapabilityRegistry(store)
    profiles = CapabilityProfiles(store)
    for entity in entities:
        if isinstance(entity, CapabilityDefinition):
            registry.register_definition(entity, runtime_scope=SCOPE, request_key=f"definition:{entity.capability_id}")
        elif isinstance(entity, CapabilityRevision):
            registry.register_revision(entity, runtime_scope=SCOPE, request_key=f"revision:{entity.revision_id}")
        elif isinstance(entity, CapabilityBinding):
            registry.bind(entity, runtime_scope=SCOPE, request_key=f"binding:{entity.binding_id}")
        elif isinstance(entity, CapabilityRelation):
            registry.relate(entity, runtime_scope=SCOPE, request_key=f"relation:{entity.relation_id}")
        elif isinstance(entity, CapabilityProfile):
            profiles.register(entity, runtime_scope=SCOPE, request_key=f"profile:{entity.profile_id}")
        elif isinstance(entity, CapabilityKnowledgeLink):
            store.mutate_capabilities_atomically(
                lambda repository, entity=entity: repository.register_knowledge_link(
                    entity,
                    scope=SCOPE,
                    request_key=f"knowledge:{entity.link_id}",
                )
            )
        else:  # pragma: no cover - test construction guard
            raise TypeError(type(entity))


def test_incremental_projection_propagates_a_late_component_failure_to_composites(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        component = _definition("planning.component")
        composite = _definition("planning.composite")
        component_revision = _revision(component)
        composite_revision = _revision(composite, dependencies=(component.capability_id,))
        component_binding = _binding(component, component_revision, name="component")
        composite_binding = _binding(composite, composite_revision, name="composite")
        relation = CapabilityRelation(
            source_capability_id=composite.capability_id,
            target_capability_id=component.capability_id,
            relation_type="composes",
            relation_policy={"minimum_maturity": "reliable", "on_dependency_failure": "blocked"},
            created_at=STAMP,
            provenance={"source": "projector-test"},
        )
        profile = _profile(component.capability_id, composite.capability_id)
        _register(
            store,
            component,
            composite,
            component_revision,
            composite_revision,
            component_binding,
            composite_binding,
            relation,
            profile,
        )
        observations = CapabilityObservations(store)
        observations.append(
            _observation(component, component_revision, component_binding, name="component-pass"),
            runtime_scope=SCOPE,
        )
        observations.append(
            _observation(composite, composite_revision, composite_binding, name="composite-pass"),
            runtime_scope=SCOPE,
        )
        projector = CapabilityStateProjector(store)
        initial = projector.project(
            profile.profile_key,
            runtime_scope=SCOPE,
            capability_scope="global",
            at_time="2024-08-20T00:01:00+00:00",
            persist=False,
        )
        assert {item["maturity"] for item in initial.snapshots} == {"reliable"}
        assert initial.to_dict()["ok"] is True

        # It arrives after the initial projection but is intentionally older
        # than the latest success.  Incremental projection must still include
        # it in the watermark and invalidate the dependent composite.
        observations.append(
            _observation(
                component,
                component_revision,
                component_binding,
                name="component-late-failure",
                verdict="fail",
                observed_at="2024-08-20T00:00:00+00:00",
            ),
            runtime_scope=SCOPE,
        )
        incremental = projector.project_affected(
            profile.profile_key,
            runtime_scope=SCOPE,
            capability_scope="global",
            affected_capability_ids=(component.capability_id,),
            at_time="2024-08-20T00:01:00+00:00",
            persist=False,
        )
        projected = {item["capability_id"]: item for item in incremental.snapshots}
        initial_projected = {item["capability_id"]: item for item in initial.snapshots}
        assert projected[component.capability_id]["input_watermark"] != initial_projected[component.capability_id]["input_watermark"]
        assert projected[composite.capability_id]["input_watermark"] != initial_projected[composite.capability_id]["input_watermark"]
        assert projected[composite.capability_id]["maturity"] == "observed"
        assert "dependency_or_composite_not_ready" in projected[composite.capability_id]["reason_codes"]
        assert incremental.to_dict()["ok"] is False
    finally:
        store.close()


def test_portable_evidence_requires_exact_provider_implementation_and_preserves_knowledge_contradiction(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        definition = _definition("memory.portable_recall")
        revision = _revision(definition)
        source = _binding(definition, revision, name="source", environment="machine-a")
        portable_target = _binding(definition, revision, name="target", environment="machine-b")
        incompatible_target = _binding(
            definition,
            revision,
            name="incompatible",
            implementation="b" * 64,
            environment="machine-c",
        )
        profile = _profile(definition.capability_id)
        contradiction = CapabilityKnowledgeLink(
            link_id="knowledge.portable-refutation",
            capability_id=definition.capability_id,
            capability_revision_id=revision.revision_id,
            knowledge_record_id="knowledge.record.portable-refutation",
            relation_type="refutes",
            source_status="active",
            applicability="rejected",
            source_trust="high",
            review_state="reviewed",
            temporal_validity={"valid_from": STAMP},
            environment_constraints={"scope": "global"},
            contradiction_state="contradicted",
            applicability_score=1.0,
            applicability_evidence_refs=("knowledge.record.portable-refutation",),
            evidence_refs=("knowledge.record.portable-refutation",),
            created_at=STAMP,
            provenance={"source": "projector-test"},
        )
        _register(store, definition, revision, source, portable_target, incompatible_target, profile, contradiction)
        CapabilityObservations(store).append(
            _observation(definition, revision, source, name="source-pass"),
            runtime_scope=SCOPE,
        )
        result = CapabilityStateProjector(store).project_affected(
            profile.profile_key,
            runtime_scope=SCOPE,
            capability_scope="global",
            affected_capability_ids=(definition.capability_id,),
            at_time="2024-08-20T00:01:00+00:00",
            persist=False,
        )
        projected = {item["provider_binding_id"]: item for item in result.snapshots}
        assert "portable_evidence_inherited_from_compatible_binding" in projected[portable_target.binding_id]["reason_codes"]
        assert incompatible_target.binding_id not in projected
        assert "knowledge_contradiction" in projected[source.binding_id]["reason_codes"]
        assert result.to_dict()["ok"] is False
    finally:
        store.close()


def test_explicit_capability_supersession_retires_the_predecessor_without_evidence_transfer(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        predecessor = _definition("memory.legacy_recall")
        successor = _definition("memory.current_recall")
        revision = _revision(predecessor)
        binding = _binding(predecessor, revision, name="predecessor")
        supersession = CapabilityRelation(
            source_capability_id=successor.capability_id,
            target_capability_id=predecessor.capability_id,
            relation_type="supersedes",
            created_at=STAMP,
            provenance={"source": "projector-test"},
            evidence_refs=("artifact://relations/current-recall.json",),
        )
        profile = _profile(predecessor.capability_id)
        _register(store, predecessor, successor, revision, binding, supersession, profile)
        CapabilityObservations(store).append(
            _observation(predecessor, revision, binding, name="predecessor-pass"),
            runtime_scope=SCOPE,
        )

        result = CapabilityStateProjector(store).project(
            profile.profile_key,
            runtime_scope=SCOPE,
            capability_scope="global",
            at_time="2024-08-20T00:01:00+00:00",
            persist=False,
        )

        assert len(result.snapshots) == 1
        snapshot = result.snapshots[0]
        assert snapshot["maturity"] == "retired"
        assert "capability_superseded_by_active_relation" in snapshot["reason_codes"]
        assert "artifact://relations/current-recall.json" in snapshot["evidence_refs"]
        assert result.to_dict()["ok"] is False
    finally:
        store.close()
