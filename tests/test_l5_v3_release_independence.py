from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityObservation,
    CapabilityRevision,
)
from eimemory.capabilities.observations import CapabilityObservations
from eimemory.governance.capability_release_evidence import (
    CAPABILITY_DEPLOYMENT_APPLICABILITY_SCHEMA,
    CAPABILITY_DEPLOYMENT_AUTHORITY_SCHEMA,
    build_capability_deployment_assurance,
    compatible_evidence_inheritance,
    environment_constraint_digest,
)
from eimemory.governance.evidence_contract import (
    current_release_identity,
    verified_deployment_receipt_identity,
)
from eimemory.governance.l5_reader import _v3_readiness_envelope
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = ScopeRef(
    tenant_id="tenant-release-v3",
    agent_id="agent-release-v3",
    workspace_id="workspace-release-v3",
    user_id="user-release-v3",
)
CAPABILITY_SCOPE = "global"
COMMIT = "a" * 40
VERSION = "9.9.9"
# Capability registration rejects future descriptors.  Keep temporal fixtures
# historical so this release-independence contract is stable across calendar
# time and tests portability rather than the wall clock.
STAMP = "2024-08-20T00:00:00+00:00"
IMPLEMENTATION_A = "a" * 64
IMPLEMENTATION_B = "b" * 64
POLICY_DIGEST = "c" * 64


def _definition() -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id="memory.release_independence",
        display_name="Release independent memory",
        description="A test capability with explicit deployment applicability.",
        owner="governance",
        risk_tier="low",
        tags=("memory", "release"),
        provenance={"source": "release-independence-test"},
        created_at=STAMP,
    )


def _revision(*, revision_id: str = "memory.release-independence:v1", compatibility: str = "incompatible", supersedes: str = "", policy_digest: str = "", affected: tuple[str, ...] = ()) -> CapabilityRevision:
    declaration = {
        "schema": CAPABILITY_DEPLOYMENT_APPLICABILITY_SCHEMA,
        "implementation_domains": ["memory.retrieval", "memory.ranking"],
    }
    if affected:
        declaration["affected_implementation_domains"] = list(affected)
    return CapabilityRevision(
        revision_id=revision_id,
        capability_id="memory.release_independence",
        contract={
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "success_invariants": ["result_is_attributed"],
            "failure_invariants": ["invalid_request_is_blocked"],
            "evidence_requirements": {"deployment_applicability": declaration},
            "dependencies": [],
            "composition": [],
            "risk_tier": "low",
            "side_effect_class": "none",
        },
        compatibility=compatibility,
        supersedes_revision_id=supersedes,
        compatibility_policy_id="policy.release-independence" if compatibility == "compatible" else "",
        compatibility_policy_digest=policy_digest if compatibility == "compatible" else "",
        provenance={"source": "release-independence-test"},
        created_at=STAMP,
    )


def _binding(
    revision: CapabilityRevision,
    *,
    binding_id: str,
    implementation_digest: str,
    machine: str,
) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id=binding_id,
        capability_id=revision.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id=f"runtime-{machine}",
        implementation_digest=implementation_digest,
        operations=("recall",),
        limits={"max_results": 8},
        environment_fingerprint={"machine": machine, "runtime": "test"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=(f"artifact://{binding_id}.json",),
        provenance={"source": "release-independence-test"},
        created_at=STAMP,
    )


def _deployment_authority(
    *,
    release,
    implementation_digest: str,
    environment_dependent: bool = False,
    machine: str = "machine-a",
    descriptive_version: str = "descriptive-only",
) -> dict[str, object]:
    authority: dict[str, object] = {
        "schema": CAPABILITY_DEPLOYMENT_AUTHORITY_SCHEMA,
        "deployment_dependent": True,
        "release": {
            "commit": release.commit,
            "receipt_id": release.receipt_id,
            "session_id": release.session_id,
            # This deliberately differs from the receipt and must not change
            # deployment applicability.
            "version": descriptive_version,
        },
        "implementation": {
            "domains": ["memory.retrieval"],
            "digest": implementation_digest,
        },
        "environment": {"dependent": environment_dependent},
    }
    if environment_dependent:
        authority["environment"] = {
            "dependent": True,
            "constraint_digest": environment_constraint_digest(
                {"machine": machine, "runtime": "test"}
            ),
        }
    return authority


def _observation(
    *,
    observation_id: str,
    revision: CapabilityRevision,
    binding: CapabilityBinding,
    authority: dict[str, object],
    machine: str,
) -> CapabilityObservation:
    return CapabilityObservation(
        observation_id=observation_id,
        capability_id=revision.capability_id,
        capability_revision_id=revision.revision_id,
        provider_binding_id=binding.binding_id,
        idempotency_key=observation_id,
        verdict="pass",
        source="release_independence_test",
        executor_id="release-independence-executor",
        executor_contract_digest="1" * 64,
        grader_id="release-independence-grader",
        grader_revision="v1",
        input_digest="2" * 64,
        output_digest="3" * 64,
        evidence_digest="4" * 64,
        evidence_refs=(f"artifact://{observation_id}.json",),
        environment_fingerprint={"machine": machine, "runtime": "test", "version": "untrusted"},
        provenance={"source": "release-independence-test"},
        metrics={"pass_rate": 1.0},
        error_taxonomy={},
        observed_at="2024-08-20T00:00:01+00:00",
        scope=CAPABILITY_SCOPE,
        deployment_authority=authority,
    )


def _receipt(*, commit: str = COMMIT, health_version: str = VERSION) -> RecordEnvelope:
    release_path = f"/opt/eimemory/releases/{commit}"
    payload = {
        "report_type": "deployment_receipt",
        "promotion_target": "code_patch",
        "action": "code_patch",
        "gate": {"ok": True, "receipt_verified": True},
        "side_effect": {
            "ok": True,
            "production_applied": True,
            "deployment_executed": True,
            "verification": {"ok": True, "skipped": False},
            "deployment": {"ok": True, "skipped": False, "release_path": release_path},
            "post_deploy_health": {
                "ok": True,
                "skipped": False,
                "commit": commit,
                "version": health_version,
                "release_path": release_path,
            },
            "commit": {"commit_sha": commit},
            "release": {"version": VERSION, "release_path": release_path},
            "rollback_evidence": {
                "prior_commit_sha": "b" * 40,
                "rollback_command": "verified rollback",
            },
        },
    }
    return RecordEnvelope.create(
        kind="promotion_request",
        title="Deployment receipt",
        scope=SCOPE,
        source="eimemory.deployment_receipt",
        status="deployed",
        content=payload,
        meta={"report_type": "deployment_receipt", "commit_sha": commit},
    )


def _registered_runtime(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    runtime._test_runtime_commit = COMMIT
    runtime.store.append(_receipt())
    definition = _definition()
    revision = _revision()
    binding = _binding(
        revision,
        # Binding identity is opaque and does not encode a machine label; the
        # test varies the declared environment fingerprint independently.
        binding_id="binding.release.primary",
        implementation_digest=IMPLEMENTATION_A,
        machine="machine-a",
    )
    runtime.capabilities.register_definition(definition, runtime_scope=SCOPE)
    runtime.capabilities.register_revision(revision, runtime_scope=SCOPE)
    runtime.capabilities.bind(binding, runtime_scope=SCOPE)
    release = current_release_identity(runtime, SCOPE)
    assert release is not None
    return runtime, revision, binding, release


def _replace_machine(runtime: Runtime, binding: CapabilityBinding, revision: CapabilityRevision, *, implementation_digest: str) -> CapabilityBinding:
    context = runtime.capabilities.binding_context(
        binding.binding_id,
        runtime_scope=SCOPE,
        capability_scope=CAPABILITY_SCOPE,
    )
    assert context is not None
    runtime.capabilities.transition_status(
        entity_type="binding",
        entity_id=binding.binding_id,
        entity_digest=context["entity_digest"],
        target_status="stale",
        runtime_scope=SCOPE,
        capability_scope=CAPABILITY_SCOPE,
        expected_state_version=context["state_version"],
        expected_state_digest=context["state_digest"],
        effective_at="2024-08-20T00:00:10+00:00",
        reason="machine replacement",
        provenance={"source": "release-independence-test"},
    )
    replacement = _binding(
        revision,
        binding_id="binding.release.replacement",
        implementation_digest=implementation_digest,
        machine="machine-b",
    )
    runtime.capabilities.bind(replacement, runtime_scope=SCOPE)
    return replacement


def test_version_is_descriptive_not_deployment_authority() -> None:
    identity = verified_deployment_receipt_identity(_receipt(health_version="different-version"))

    assert identity is not None
    assert identity.commit == COMMIT
    assert identity.version == VERSION


def test_absent_deployment_declaration_is_explicitly_non_blocking_not_green(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = build_capability_deployment_assurance(
            runtime,
            scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
        )
    finally:
        runtime.close()

    assert report["status"] == "not_evaluated"
    assert report["ok"] is None
    assert report["required"] is False
    assert report["blocking"] is False


def test_v3_reader_preserves_non_blocking_deployment_axis_without_claiming_it_is_ready() -> None:
    envelope = _v3_readiness_envelope(
        {
            "ok": True,
            "status": "ready",
            "loop_maturity": "experimenting",
            "adapter_readiness": {"adapter.test": "ready"},
            "deployment_assurance": {
                "ok": None,
                "required": False,
                "blocking": False,
                "status": "not_evaluated",
            },
        },
        scope=SCOPE,
        profile_key="profile.release-independence",
        capability_scope=CAPABILITY_SCOPE,
    )

    assert envelope["ok"] is True
    assert envelope["deployment_ready"] is None
    assert envelope["deployment_required"] is False
    assert envelope["deployment_blocking"] is False


def test_v3_reader_blocks_a_failed_declared_deployment_requirement() -> None:
    envelope = _v3_readiness_envelope(
        {
            "ok": True,
            "status": "ready",
            "loop_maturity": "experimenting",
            "adapter_readiness": {"adapter.test": "ready"},
            "deployment_assurance": {
                "ok": False,
                "required": True,
                "blocking": True,
                "status": "degraded",
            },
        },
        scope=SCOPE,
        profile_key="profile.release-independence",
        capability_scope=CAPABILITY_SCOPE,
    )

    assert envelope["ok"] is False
    assert envelope["status"] == "blocked"
    assert envelope["deployment_ready"] is False
    assert envelope["deployment_required"] is True
    assert envelope["deployment_blocking"] is True


def test_runtime_v4_reader_exposes_explicit_product_completion_evidence_refs() -> None:
    envelope = _v3_readiness_envelope(
        {
            "ok": True,
            "status": "ready",
            "loop_maturity": "experimenting",
            "adapter_readiness": {"adapter.test": "ready"},
            "deployment_assurance": {"ok": None, "required": False, "blocking": False},
        },
        scope=SCOPE,
        profile_key="profile.release-independence",
        capability_scope=CAPABILITY_SCOPE,
        runtime=object(),
        runtime_scope=SCOPE,
    )

    assert envelope["schema_version"] == "l5_readiness.v4"
    assert envelope["product_l5_complete"] is False
    assert envelope["completion_status"] == "incomplete"
    assert envelope["completion_evidence_refs"] == {
        "provider": [],
        "catalog": [],
        "transaction": [],
        "lineage": [],
    }


def test_same_revision_on_new_machine_retains_portable_evidence_and_invalidates_only_environment_specific(tmp_path) -> None:
    runtime, revision, binding, release = _registered_runtime(tmp_path)
    portable = _observation(
        observation_id="observation.portable",
        revision=revision,
        binding=binding,
        authority=_deployment_authority(
            release=release,
            implementation_digest=IMPLEMENTATION_A,
            environment_dependent=False,
        ),
        machine="machine-a",
    )
    environment_specific = _observation(
        observation_id="observation.environment",
        revision=revision,
        binding=binding,
        authority=_deployment_authority(
            release=release,
            implementation_digest=IMPLEMENTATION_A,
            environment_dependent=True,
            machine="machine-a",
        ),
        machine="machine-a",
    )
    observations = CapabilityObservations(runtime.store)
    observations.append(portable, runtime_scope=SCOPE)
    observations.append(environment_specific, runtime_scope=SCOPE)
    _replace_machine(runtime, binding, revision, implementation_digest=IMPLEMENTATION_A)
    try:
        report = build_capability_deployment_assurance(
            runtime,
            scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["status"] == "degraded"
    assert report["verified_evidence_count"] == 1
    assert report["invalidated_evidence_count"] == 1
    assert report["invalidations"][0]["evidence_id"] == "observation.environment"
    assert report["invalidations"][0]["reason"] == "environment_constraint_changed"
    assert "version" not in report["current_release"]
    assert report["diagnostics"]["machine_identity_used"] is False
    assert "machine-a" not in json.dumps(report, sort_keys=True)


def test_changed_implementation_contract_invalidates_affected_evidence_without_version_change(tmp_path) -> None:
    runtime, revision, binding, release = _registered_runtime(tmp_path)
    observation = _observation(
        observation_id="observation.implementation",
        revision=revision,
        binding=binding,
        authority=_deployment_authority(
            release=release,
            implementation_digest=IMPLEMENTATION_A,
        ),
        machine="machine-a",
    )
    CapabilityObservations(runtime.store).append(observation, runtime_scope=SCOPE)
    _replace_machine(runtime, binding, revision, implementation_digest=IMPLEMENTATION_B)
    try:
        report = build_capability_deployment_assurance(
            runtime,
            scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
        )
    finally:
        runtime.close()

    assert report["ok"] is False
    assert report["current_release"]["commit"] == COMMIT
    assert report["invalidations"][0]["reason"] == "implementation_contract_changed"


def test_explicit_compatible_revision_can_inherit_environment_independent_evidence_across_release(
    tmp_path,
) -> None:
    runtime, source_revision, source_binding, source_release = _registered_runtime(tmp_path)
    observation = _observation(
        observation_id="observation.compatible-release",
        revision=source_revision,
        binding=source_binding,
        authority=_deployment_authority(
            release=source_release,
            implementation_digest=IMPLEMENTATION_A,
            environment_dependent=False,
        ),
        machine="machine-a",
    )
    target_revision = _revision(
        revision_id="memory.release-independence:v2",
        compatibility="compatible",
        supersedes=source_revision.revision_id,
        policy_digest=POLICY_DIGEST,
        affected=("memory.ranking",),
    )
    target_binding = _binding(
        target_revision,
        binding_id="binding.release.compatible",
        implementation_digest=IMPLEMENTATION_B,
        machine="machine-b",
    )
    target_commit = "d" * 40
    try:
        CapabilityObservations(runtime.store).append(observation, runtime_scope=SCOPE)
        runtime.capabilities.register_revision(target_revision, runtime_scope=SCOPE)
        runtime.capabilities.bind(target_binding, runtime_scope=SCOPE)
        runtime.store.append(_receipt(commit=target_commit))
        runtime._test_runtime_commit = target_commit
        report = build_capability_deployment_assurance(
            runtime,
            scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["status"] == "ready"
    assert report["current_release"]["commit"] == target_commit
    assert report["verified_evidence_count"] == 1
    assert report["invalidated_evidence_count"] == 0


def test_compatible_inheritance_is_digest_and_implementation_domain_bound() -> None:
    source = _revision()
    target = _revision(
        revision_id="memory.release-independence:v2",
        compatibility="compatible",
        supersedes=source.revision_id,
        policy_digest=POLICY_DIGEST,
        affected=("memory.ranking",),
    )
    source_descriptor = source.to_dict()
    target_descriptor = target.to_dict()
    source_binding = _binding(
        source,
        binding_id="binding.release.source",
        implementation_digest=IMPLEMENTATION_A,
        machine="machine-a",
    ).to_dict()
    target_binding = _binding(
        target,
        binding_id="binding.release.target",
        implementation_digest=IMPLEMENTATION_B,
        machine="machine-b",
    ).to_dict()

    unaffected = compatible_evidence_inheritance(
        source_revision=source_descriptor,
        target_revision=target_descriptor,
        source_binding=source_binding,
        target_binding=target_binding,
        implementation_domains=("memory.retrieval",),
    )
    affected = compatible_evidence_inheritance(
        source_revision=source_descriptor,
        target_revision=target_descriptor,
        source_binding=source_binding,
        target_binding=target_binding,
        implementation_domains=("memory.ranking",),
    )

    assert unaffected["ok"] is True
    assert unaffected["policy_digest"] == POLICY_DIGEST
    assert affected["ok"] is False
    assert affected["reason"] == "affected_implementation_domain_changed"
