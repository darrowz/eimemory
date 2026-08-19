from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import sqlite3

import pytest

from eimemory.capabilities import (
    CapabilityBinding,
    CapabilityDefinition,
    CapabilityKnowledgeLink,
    CapabilityObservation,
    CapabilityProfile,
    CapabilityRelation,
    CapabilityRevision,
    CapabilityStateSnapshot,
    EvaluationRun,
    EvaluationSpec,
    L5AssessmentV3,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.capability_store import (
    CapabilityConflict,
    CapabilityIdempotencyConflict,
    CapabilityStore,
    CapabilityStoreError,
)
from eimemory.storage.migrations.capability_v3 import (
    CAPABILITY_V3_BACKFILL_MIGRATION,
    apply_capability_v3_backfill_batch,
    capability_v3_backfill_state,
)
from eimemory.storage.runtime_store import RuntimeStore, _capability_audit_from_record


SCOPE = ScopeRef(tenant_id="tenant-a", agent_id="agent-a", workspace_id="workspace-a", user_id="user-a")
STAMP = "2026-08-20T00:00:00+00:00"


def _definition(capability_id: str = "planning.constraint_resolution") -> CapabilityDefinition:
    return CapabilityDefinition(
        capability_id=capability_id,
        display_name=capability_id.replace(".", " ").title(),
        description=f"A bounded, auditable capability for {capability_id}.",
        owner="governance",
        risk_tier="bounded_write",
        tags=("planning",),
        provenance={"source": "storage-test"},
        created_at=STAMP,
    )


def _revision(definition: CapabilityDefinition) -> CapabilityRevision:
    return CapabilityRevision(
        revision_id=f"{definition.capability_id}:v1",
        capability_id=definition.capability_id,
        contract={
            "input_schema": {"type": "object", "required": ["constraints"]},
            "output_schema": {"type": "object", "required": ["decision"]},
            "success_invariants": ["decision_is_traceable"],
            "failure_invariants": ["unsupported_input_is_blocked"],
            "evidence_requirements": {"minimum_refs": 1},
            "dependencies": [],
            "composition": [],
            "risk_tier": "low",
            "side_effect_class": "none",
        },
        compatibility="incompatible",
        provenance={"source": "storage-test"},
        created_at=STAMP,
    )


def _binding(definition: CapabilityDefinition, revision: CapabilityRevision) -> CapabilityBinding:
    return CapabilityBinding(
        binding_id="binding.constraint.local:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_kind="module",
        provider_instance_id="local-runtime",
        implementation_digest="a" * 64,
        operations=("plan",),
        limits={"max_constraints": 32},
        environment_fingerprint={"runtime": "isolated"},
        applicability={"scope": "global"},
        advertisement_evidence_refs=("artifact://advertisements/local-v1.json",),
        provenance={"source": "storage-test"},
        created_at=STAMP,
    )


def _profile(definition: CapabilityDefinition) -> CapabilityProfile:
    return CapabilityProfile(
        profile_id="profile.governance:v1",
        requirements={definition.capability_id: {"minimum_maturity": "evaluated", "min_pass_rate": 0.8}},
        provenance={"source": "storage-test"},
        created_at=STAMP,
    )


def _spec(definition: CapabilityDefinition, revision: CapabilityRevision) -> EvaluationSpec:
    return EvaluationSpec(
        eval_spec_id="eval.constraint:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        grader_type="code",
        executor_id="capability_probe_executor.v3",
        executor_contract_digest="1" * 64,
        fixture_refs=("artifact://fixtures/constraint-v1.json",),
        checks=("decision_is_traceable",),
        required_metrics=("pass_rate", "latency_ms"),
        retry_policy={"max_attempts": 2},
        stability_policy={"min_consecutive_passes": 2},
        applicability={"scope": "global"},
        resource_budget={"timeout_seconds": 30, "max_memory_mb": 128},
        provenance={"source": "storage-test"},
        created_at=STAMP,
    )


def _run(
    definition: CapabilityDefinition,
    revision: CapabilityRevision,
    binding: CapabilityBinding,
    spec: EvaluationSpec,
) -> EvaluationRun:
    return EvaluationRun(
        run_id="run.constraint:v1",
        eval_spec_id=spec.eval_spec_id,
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_binding_id=binding.binding_id,
        idempotency_key="eval.constraint:case-1:attempt-1",
        verdict="pass",
        source="evaluation.replay",
        executor_id=spec.executor_id,
        executor_contract_digest="2" * 64,
        grader_id="deterministic-rule",
        grader_revision="deterministic-rule:v1",
        input_digest="3" * 64,
        output_digest="4" * 64,
        evidence_digest="5" * 64,
        evidence_refs=("artifact://replay/constraint-case-1.json",),
        environment_fingerprint={"runtime": "isolated"},
        provenance={"source": "storage-test"},
        metrics={"pass_rate": 1.0, "latency_ms": 12},
        error_taxonomy={},
        started_at=STAMP,
        finished_at="2026-08-20T00:00:01+00:00",
    )


def _observation(
    definition: CapabilityDefinition,
    revision: CapabilityRevision,
    binding: CapabilityBinding,
    run: EvaluationRun,
) -> CapabilityObservation:
    return CapabilityObservation(
        observation_id="observation.constraint:task-1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        provider_binding_id=binding.binding_id,
        idempotency_key="adapter.hermes:task-1",
        verdict="pass",
        source="adapter.hermes",
        executor_id="adapter.hermes.v1",
        executor_contract_digest="6" * 64,
        grader_id="outcome-verifier",
        grader_revision="outcome-verifier:v1",
        input_digest="7" * 64,
        output_digest="8" * 64,
        evidence_digest="9" * 64,
        evidence_refs=(run.run_id,),
        environment_fingerprint={"adapter": "hermes"},
        provenance={"source": "storage-test"},
        metrics={"success": 1},
        error_taxonomy={},
        observed_at="2026-08-20T00:00:02+00:00",
    )


def _link(definition: CapabilityDefinition, revision: CapabilityRevision) -> CapabilityKnowledgeLink:
    return CapabilityKnowledgeLink(
        link_id="knowledge-link.constraint:paper-1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        knowledge_record_id="knowledge-page:paper-1",
        relation_type="informs_eval",
        source_status="active",
        applicability="candidate",
        source_trust="high",
        review_state="reviewed",
        temporal_validity={"valid_from": STAMP},
        environment_constraints={"scope": "global"},
        contradiction_state="none",
        applicability_score=0.75,
        applicability_evidence_refs=("knowledge-page:paper-1",),
        evidence_refs=("knowledge-page:paper-1",),
        provenance={"source": "storage-test"},
        created_at=STAMP,
    )


def _snapshot(
    definition: CapabilityDefinition,
    revision: CapabilityRevision,
    profile: CapabilityProfile,
    run: EvaluationRun,
    link: CapabilityKnowledgeLink,
) -> CapabilityStateSnapshot:
    return CapabilityStateSnapshot(
        snapshot_id="snapshot.constraint:v1",
        capability_id=definition.capability_id,
        capability_revision_id=revision.revision_id,
        profile_id=profile.profile_id,
        maturity="evaluated",
        confidence=0.75,
        evidence_refs=(run.run_id, link.link_id),
        sample_sufficiency={"observations": 2, "required": 2},
        reliability_metrics={"pass_at_1": 1.0},
        latest_success_ref=run.run_id,
        latest_failure_ref="",
        regression_streak=0,
        dependency_state={"status": "ready"},
        knowledge_applicability={"status": "candidate"},
        provider_applicability={"status": "applicable"},
        environment_applicability={"status": "applicable"},
        input_watermark="observation:1",
        algorithm_revision="capability-state.v3",
        computed_at="2026-08-20T00:00:03+00:00",
    )


def _assessment(profile: CapabilityProfile, revision: CapabilityRevision, snapshot: CapabilityStateSnapshot) -> L5AssessmentV3:
    return L5AssessmentV3(
        assessment_id="assessment.l5:v1",
        profile_id=profile.profile_id,
        loop_maturity="experimenting",
        capability_snapshot_ids=(snapshot.snapshot_id,),
        capability_readiness={
            revision.revision_id: {
                "_revision": {"maturity": snapshot.maturity, "snapshot_id": snapshot.snapshot_id}
            }
        },
        adapter_readiness={"hermes": "ready"},
        deployment_assurance={"status": "not_evaluated"},
        evidence_refs=(snapshot.snapshot_id,),
        created_at="2026-08-20T00:00:03+00:00",
    )


def test_v3_schema_is_idempotent_and_does_not_change_legacy_record_reads(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    memory = RecordEnvelope.create(
        kind="memory",
        title="legacy read canary",
        content={"text": "the original record path remains readable"},
        scope=SCOPE,
        source="storage-test",
    )
    try:
        store.append(memory)
        assert store.get_by_id(memory.record_id, scope=SCOPE) is not None
        assert store.sqlite.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row[0]
            for row in store.sqlite.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "capability_definitions",
            "capability_revisions",
            "capability_relations",
            "capability_bindings",
            "adapter_capability_advertisements",
            "capability_profiles",
            "evaluation_specs",
            "evaluation_runs",
            "capability_ledger_events",
            "capability_observations",
            "capability_knowledge_links",
            "capability_state_snapshots",
            "l5_assessments_v3",
            "l5_assessment_readiness_refs",
            "capability_operation_journal",
        } <= tables
        state = capability_v3_backfill_state(store.sqlite.conn)
        assert state["migration_id"] == CAPABILITY_V3_BACKFILL_MIGRATION
        assert state["status"] == "not_scheduled"
        report = apply_capability_v3_backfill_batch(
            store.sqlite.conn, batch_size=999_999, max_seconds=999.0
        )
        assert report["scheduled"] is False
        assert report["processed"] == 0
        assert report["batch_size"] == 2_000
        assert report["max_seconds"] == 60.0
    finally:
        store.close()


def test_v3_foreign_keys_reject_orphan_revision(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    orphan = _revision(_definition())
    try:
        with pytest.raises(sqlite3.IntegrityError):
            store.mutate_capabilities_atomically(
                lambda capabilities: capabilities.register_revision(orphan, scope=SCOPE)
            )
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_revisions").fetchone()[0] == 0
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_operation_journal").fetchone()[0] == 0
    finally:
        store.close()


def test_v3_repository_cannot_bypass_runtime_transaction_and_audit_outbox(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        with pytest.raises(CapabilityStoreError, match="transaction-local"):
            CapabilityStore(store.sqlite)
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_definitions").fetchone()[0] == 0
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_operation_journal").fetchone()[0] == 0
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0] == 0
    finally:
        store.close()


def test_v3_audit_records_do_not_collide_with_legacy_capability_model_reads(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    legacy = RecordEnvelope.create(
        kind="capability_model",
        title="legacy capability model",
        content={"model": "legacy"},
        scope=SCOPE,
        source="legacy-test",
    )
    try:
        store.append(legacy)
        store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(
                _definition("planning.audit_kind_isolation"),
                scope=SCOPE,
            )
        )
        legacy_rows = store.list_records(kinds=["capability_model"], scope=SCOPE, limit=10)
        assert [row.record_id for row in legacy_rows] == [legacy.record_id]
        audits = store.list_records(kinds=["capability_audit"], scope=SCOPE, limit=10)
        assert len(audits) == 1
        assert audits[0].source == "eimemory.capability.v3"
    finally:
        store.close()


def test_v3_savepoint_removes_partial_ledger_when_callback_handles_conflict(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    definition = _definition()
    conflicting = replace(
        definition,
        description="A different immutable definition payload with the same semantic identity.",
    )
    try:
        def mutation(capabilities):
            capabilities.register_definition(definition, scope=SCOPE)
            with pytest.raises(CapabilityConflict):
                capabilities.register_definition(conflicting, scope=SCOPE)
            return "handled"

        assert store.mutate_capabilities_atomically(mutation) == "handled"
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_definitions").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_ledger_events").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_operation_journal").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0] == 1
    finally:
        store.close()


def test_v3_semantic_retry_requires_the_original_request_key(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    definition = _definition("planning.semantic_retry")
    try:
        first = store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(
                definition,
                scope=SCOPE,
                request_key="definition:semantic-retry:first",
            )
        )
        exact_retry = store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(
                definition,
                scope=SCOPE,
                request_key="definition:semantic-retry:first",
            )
        )
        with pytest.raises(CapabilityConflict, match="original request key"):
            store.mutate_capabilities_atomically(
                lambda capabilities: capabilities.register_definition(
                    definition,
                    scope=SCOPE,
                    request_key="definition:semantic-retry:transport-retry",
                )
            )
        assert first.idempotent is False
        assert exact_retry.idempotent is True
        assert exact_retry.operation_id == first.operation_id
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_ledger_events").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_operation_journal").fetchone()[0] == 1
        assert store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM records WHERE kind='capability_audit'"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_v3_storage_context_is_part_of_idempotency_and_immutable_identity(tmp_path) -> None:
    """Relational context cannot be a mutable side channel around a typed digest."""

    store = RuntimeStore(tmp_path)
    definition = _definition("planning.context_identity")
    revision = _revision(definition)
    binding_a = _binding(definition, revision)
    binding_b = replace(
        binding_a,
        binding_id="binding.context.local:v2",
        provider_instance_id="local-runtime-b",
        implementation_digest="b" * 64,
    )
    profile_a = _profile(definition)
    profile_b = replace(profile_a, profile_id="profile.governance:v2")
    spec = _spec(definition, revision)
    run = _run(definition, revision, binding_a, spec)
    link = _link(definition, revision)
    snapshot = _snapshot(definition, revision, profile_a, run, link)
    try:
        def register_context_a(capabilities):
            capabilities.register_definition(definition, scope=SCOPE)
            capabilities.register_revision(revision, scope=SCOPE)
            capabilities.register_binding(binding_a, scope=SCOPE)
            capabilities.register_binding(binding_b, scope=SCOPE)
            capabilities.register_profile(profile_a, scope=SCOPE)
            capabilities.register_profile(profile_b, scope=SCOPE)
            capabilities.register_evaluation_spec(spec, scope=SCOPE, profile_id=profile_a.profile_id)
            capabilities.record_evaluation_run(run, scope=SCOPE, profile_id=profile_a.profile_id)
            capabilities.register_knowledge_link(
                link,
                scope=SCOPE,
                knowledge_storage_key="knowledge-storage-a",
                knowledge_record_digest="a" * 64,
            )
            capabilities.register_snapshot(snapshot, scope=SCOPE, provider_binding_id=binding_a.binding_id)

        store.mutate_capabilities_atomically(register_context_a)
        journal_count = store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM capability_operation_journal"
        ).fetchone()[0]

        conflicting_writes = (
            lambda capabilities: capabilities.register_evaluation_spec(
                spec,
                scope=SCOPE,
                profile_id=profile_b.profile_id,
            ),
            lambda capabilities: capabilities.record_evaluation_run(
                run,
                scope=SCOPE,
                profile_id=profile_b.profile_id,
            ),
            lambda capabilities: capabilities.register_knowledge_link(
                link,
                scope=SCOPE,
                knowledge_storage_key="knowledge-storage-b",
                knowledge_record_digest="b" * 64,
            ),
            lambda capabilities: capabilities.register_snapshot(
                snapshot,
                scope=SCOPE,
                provider_binding_id=binding_b.binding_id,
            ),
        )
        for mutation in conflicting_writes:
            with pytest.raises(CapabilityIdempotencyConflict):
                store.mutate_capabilities_atomically(mutation)

        # A new key cannot side-step the journal check and mutate the same
        # immutable entity's local relational context either.
        with pytest.raises(CapabilityConflict):
            store.mutate_capabilities_atomically(
                lambda capabilities: capabilities.register_evaluation_spec(
                    spec,
                    scope=SCOPE,
                    profile_id=profile_b.profile_id,
                    request_key="eval-spec:context-b",
                )
            )
        assert store.sqlite.conn.execute(
            "SELECT profile_id FROM evaluation_specs WHERE eval_spec_id=?",
            (spec.eval_spec_id,),
        ).fetchone()[0] == profile_a.profile_id
        assert store.sqlite.conn.execute(
            "SELECT profile_id FROM evaluation_runs WHERE run_id=?",
            (run.run_id,),
        ).fetchone()[0] == profile_a.profile_id
        assert store.sqlite.conn.execute(
            "SELECT knowledge_storage_key FROM capability_knowledge_links WHERE link_id=?",
            (link.link_id,),
        ).fetchone()[0] == "knowledge-storage-a"
        assert store.sqlite.conn.execute(
            "SELECT provider_binding_id FROM capability_state_snapshots WHERE snapshot_id=?",
            (snapshot.snapshot_id,),
        ).fetchone()[0] == binding_a.binding_id
        assert store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM capability_operation_journal"
        ).fetchone()[0] == journal_count
    finally:
        store.close()


def test_v3_observation_cannot_link_a_revision_to_another_capability_binding(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    definition_a = _definition("planning.chain_a")
    revision_a = _revision(definition_a)
    binding_a = _binding(definition_a, revision_a)
    definition_b = _definition("planning.chain_b")
    revision_b = _revision(definition_b)
    binding_b = replace(
        _binding(definition_b, revision_b),
        binding_id="binding.chain-b:v1",
        implementation_digest="f" * 64,
    )
    spec_a = _spec(definition_a, revision_a)
    run_a = _run(definition_a, revision_a, binding_a, spec_a)
    mismatched = replace(
        _observation(definition_a, revision_a, binding_a, run_a),
        observation_id="observation.chain-mismatch:v1",
        provider_binding_id=binding_b.binding_id,
    )
    try:
        def mutation(capabilities):
            capabilities.register_definition(definition_a, scope=SCOPE)
            capabilities.register_definition(definition_b, scope=SCOPE)
            capabilities.register_revision(revision_a, scope=SCOPE)
            capabilities.register_revision(revision_b, scope=SCOPE)
            capabilities.register_binding(binding_a, scope=SCOPE)
            capabilities.register_binding(binding_b, scope=SCOPE)
            with pytest.raises(sqlite3.IntegrityError):
                capabilities.append_observation(mismatched, scope=SCOPE)

        store.mutate_capabilities_atomically(mutation)
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_observations").fetchone()[0] == 0
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_ledger_events").fetchone()[0] == 6
    finally:
        store.close()


def test_v3_assessment_cannot_reference_snapshot_from_another_profile(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    definition = _definition("planning.assessment_profile_chain")
    revision = _revision(definition)
    binding = _binding(definition, revision)
    profile_a = _profile(definition)
    profile_b = replace(profile_a, profile_id="profile.governance:other")
    spec = _spec(definition, revision)
    run = _run(definition, revision, binding, spec)
    link = _link(definition, revision)
    snapshot_b = replace(
        _snapshot(definition, revision, profile_b, run, link),
        snapshot_id="snapshot.constraint:other-profile",
    )
    invalid_assessment = _assessment(profile_a, revision, snapshot_b)
    try:
        def mutation(capabilities):
            capabilities.register_definition(definition, scope=SCOPE)
            capabilities.register_revision(revision, scope=SCOPE)
            capabilities.register_binding(binding, scope=SCOPE)
            capabilities.register_profile(profile_a, scope=SCOPE)
            capabilities.register_profile(profile_b, scope=SCOPE)
            capabilities.register_snapshot(
                snapshot_b,
                scope=SCOPE,
                provider_binding_id=binding.binding_id,
            )
            with pytest.raises(CapabilityStoreError, match="another profile"):
                capabilities.register_assessment(invalid_assessment, scope=SCOPE)

        store.mutate_capabilities_atomically(mutation)
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM l5_assessments_v3").fetchone()[0] == 0
        assert store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM l5_assessment_snapshot_refs"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_v3_assessment_readiness_must_match_snapshot_revision_and_binding(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    definition_a = _definition("planning.readiness_chain_a")
    revision_a = _revision(definition_a)
    binding_a = _binding(definition_a, revision_a)
    binding_b = replace(
        binding_a,
        binding_id="binding.readiness-chain-b:v1",
        provider_instance_id="local-runtime-b",
        implementation_digest="c" * 64,
    )
    definition_b = _definition("planning.readiness_chain_b")
    revision_b = _revision(definition_b)
    profile = _profile(definition_a)
    spec = _spec(definition_a, revision_a)
    run = _run(definition_a, revision_a, binding_a, spec)
    link = _link(definition_a, revision_a)
    snapshot_a = _snapshot(definition_a, revision_a, profile, run, link)
    revision_snapshot = replace(
        snapshot_a,
        snapshot_id="snapshot.constraint:revision-wide",
    )
    base_assessment = _assessment(profile, revision_a, snapshot_a)
    mismatched_maturity = replace(
        _assessment(profile, revision_a, revision_snapshot),
        assessment_id="assessment.l5:wrong-maturity",
        capability_readiness={
            revision_a.revision_id: {
                "_revision": {"maturity": "reliable", "snapshot_id": revision_snapshot.snapshot_id}
            }
        },
    )
    mismatched_revision = replace(
        base_assessment,
        assessment_id="assessment.l5:wrong-revision",
        capability_readiness={
            revision_b.revision_id: {
                "_revision": {"maturity": snapshot_a.maturity, "snapshot_id": snapshot_a.snapshot_id}
            }
        },
    )
    mismatched_binding = replace(
        base_assessment,
        assessment_id="assessment.l5:wrong-binding",
        capability_readiness={
            revision_a.revision_id: {
                binding_b.binding_id: {
                    "maturity": snapshot_a.maturity,
                    "snapshot_id": snapshot_a.snapshot_id,
                }
            }
        },
    )
    provider_bound_revision = replace(
        base_assessment,
        assessment_id="assessment.l5:provider-bound-revision",
    )
    try:
        def mutation(capabilities):
            capabilities.register_definition(definition_a, scope=SCOPE)
            capabilities.register_definition(definition_b, scope=SCOPE)
            capabilities.register_revision(revision_a, scope=SCOPE)
            capabilities.register_revision(revision_b, scope=SCOPE)
            capabilities.register_binding(binding_a, scope=SCOPE)
            capabilities.register_binding(binding_b, scope=SCOPE)
            capabilities.register_profile(profile, scope=SCOPE)
            capabilities.register_snapshot(
                snapshot_a,
                scope=SCOPE,
                provider_binding_id=binding_a.binding_id,
            )
            capabilities.register_snapshot(
                revision_snapshot,
                scope=SCOPE,
                provider_binding_id=None,
            )
            with pytest.raises(CapabilityStoreError, match="revision .* does not match snapshot"):
                capabilities.register_assessment(mismatched_revision, scope=SCOPE)
            with pytest.raises(CapabilityStoreError, match="binding .* does not match snapshot"):
                capabilities.register_assessment(mismatched_binding, scope=SCOPE)
            with pytest.raises(CapabilityStoreError, match="revision-wide readiness cannot cite provider-bound"):
                capabilities.register_assessment(provider_bound_revision, scope=SCOPE)
            with pytest.raises(CapabilityStoreError, match="maturity does not match snapshot"):
                capabilities.register_assessment(mismatched_maturity, scope=SCOPE)

        store.mutate_capabilities_atomically(mutation)
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM l5_assessments_v3").fetchone()[0] == 0
        assert store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM l5_assessment_readiness_refs"
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_v3_domain_writes_use_ledger_index_journal_and_query_indexes(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    definition = _definition()
    dependency = _definition("planning.path_search")
    revision = _revision(definition)
    binding = _binding(definition, revision)
    profile = _profile(definition)
    spec = _spec(definition, revision)
    run = _run(definition, revision, binding, spec)
    observation = _observation(definition, revision, binding, run)
    link = _link(definition, revision)
    snapshot = _snapshot(definition, revision, profile, run, link)
    assessment = _assessment(profile, revision, snapshot)
    relation = CapabilityRelation(
        source_capability_id=dependency.capability_id,
        target_capability_id=definition.capability_id,
        relation_type="depends_on",
        relation_policy={"on_dependency_failure": "blocked"},
        provenance={"source": "storage-test"},
        created_at=STAMP,
    )

    try:
        def register_all(capabilities):
            capabilities.register_definition(definition, scope=SCOPE)
            capabilities.register_definition(dependency, scope=SCOPE)
            capabilities.register_revision(revision, scope=SCOPE)
            capabilities.register_relation(relation, scope=SCOPE)
            capabilities.register_binding(binding, scope=SCOPE)
            capabilities.register_profile(profile, scope=SCOPE)
            capabilities.register_evaluation_spec(spec, scope=SCOPE, profile_id=profile.profile_id)
            capabilities.record_evaluation_run(run, scope=SCOPE, profile_id=profile.profile_id)
            result = capabilities.append_observation(observation, scope=SCOPE)
            capabilities.register_knowledge_link(link, scope=SCOPE)
            capabilities.register_snapshot(snapshot, scope=SCOPE, provider_binding_id=None)
            capabilities.register_assessment(assessment, scope=SCOPE)
            return result

        result = store.mutate_capabilities_atomically(register_all)
        assert result.idempotent is False
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_ledger_events").fetchone()[0] == 12
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_operation_journal").fetchone()[0] == 12
        assert store.capability_export_status()["pending"] == 0
        row = store.sqlite.conn.execute(
            "SELECT ledger_event_id FROM capability_observations WHERE observation_id=?",
            (observation.observation_id,),
        ).fetchone()
        assert row is not None
        assert str(row[0]).startswith("capability-ledger-")
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM l5_assessment_snapshot_refs").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM l5_assessment_readiness_refs").fetchone()[0] == 1
        assert store.capability_export_status()["ok"] is True

        replay = store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.append_observation(observation, scope=SCOPE)
        )
        assert replay.idempotent is True
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_observations").fetchone()[0] == 1

        conflicting = replace(observation, metrics={"success": 0})
        with pytest.raises(CapabilityIdempotencyConflict):
            store.mutate_capabilities_atomically(
                lambda capabilities: capabilities.append_observation(conflicting, scope=SCOPE)
            )

        plan = store.sqlite.conn.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT observation_id FROM capability_observations
            WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND capability_scope=?
              AND capability_revision_id=? AND provider_binding_id=?
            ORDER BY observed_at DESC
            """,
            (
                SCOPE.tenant_id,
                SCOPE.agent_id,
                SCOPE.workspace_id,
                SCOPE.user_id,
                definition.scope,
                revision.revision_id,
                binding.binding_id,
            ),
        ).fetchall()
        assert any("idx_capability_observations_scope_capability_binding_time" in str(row[3]) for row in plan)

        rebuild = store.rebuild_sqlite_from_jsonl(replace=True)
        assert rebuild["ok"] is True
        assert rebuild["replayed"]["capability_v3"] == 12
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_definitions").fetchone()[0] == 2
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_revisions").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_bindings").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_profiles").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM evaluation_runs").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_observations").fetchone()[0] == 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_ledger_events").fetchone()[0] == 12
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM capability_operation_journal").fetchone()[0] == 12
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM l5_assessment_readiness_refs").fetchone()[0] == 1
    finally:
        store.close()


def test_v3_post_commit_export_failure_is_recoverable(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    definition = _definition()
    try:
        def fail_export(*_args, **_kwargs):
            raise OSError("simulated JSONL outage")

        monkeypatch.setattr(store, "_flush_committed_exports", fail_export)
        result = store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(definition, scope=SCOPE)
        )
        assert result.idempotent is False
        assert store.capability_export_status()["ok"] is False
        assert store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM export_outbox WHERE state='pending'"
        ).fetchone()[0] == 1
        monkeypatch.undo()
        assert store.flush_exports()["remaining"] == 0
        assert store.sqlite.conn.execute(
            "SELECT COUNT(*) FROM capability_definitions"
        ).fetchone()[0] == 1
    finally:
        store.close()


def test_v3_export_pending_status_survives_restart_and_recovers(tmp_path, monkeypatch) -> None:
    store = RuntimeStore(tmp_path)
    definition = _definition("planning.restart_recovery")
    resumed: RuntimeStore | None = None
    store_closed = False
    try:
        monkeypatch.setattr(store, "_flush_committed_exports", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("outage")))
        store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(definition, scope=SCOPE)
        )
        assert store.capability_export_status()["pending"] == 1
        store.close()
        store_closed = True
        resumed = RuntimeStore(tmp_path)
        assert resumed.capability_export_status()["ok"] is False
        assert resumed.capability_export_status()["pending"] == 1
        assert resumed.flush_exports()["remaining"] == 0
        assert resumed.capability_export_status()["ok"] is True
        assert resumed.capability_export_status()["pending"] == 0
    finally:
        if resumed is not None:
            resumed.close()
        elif not store_closed:
            store.close()


def test_v3_export_status_survives_prunable_outbox_history(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    resumed: RuntimeStore | None = None
    store_closed = False
    try:
        store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(
                _definition("planning.prunable_export_history"),
                scope=SCOPE,
            )
        )
        assert store.capability_export_status()["ok"] is True
        assert store.sqlite.prune_exported(keep=0) >= 1
        assert store.sqlite.conn.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0] == 0
        assert store.capability_export_status()["ok"] is True
        assert store.capability_export_status()["pending"] == 0

        store.close()
        store_closed = True
        resumed = RuntimeStore(tmp_path)
        assert resumed.capability_export_status()["ok"] is True
        assert resumed.capability_export_status()["pending"] == 0
    finally:
        if resumed is not None:
            resumed.close()
        elif not store_closed:
            store.close()


def test_v3_audit_replay_envelope_requires_exact_scope_and_identities(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        result = store.mutate_capabilities_atomically(
            lambda capabilities: capabilities.register_definition(
                _definition("planning.audit_envelope"),
                scope=SCOPE,
            )
        )
        record = store.get_by_id(
            f"capability_audit_{result.operation_id[:24]}",
            scope=SCOPE,
        )
        assert record is not None
        assert _capability_audit_from_record(
            record,
            scanned_operation_id=result.operation_id,
        )["operation_id"] == result.operation_id

        malformed_scope = deepcopy(record)
        malformed_scope.content["audit"]["scope"].pop("user_id")
        with pytest.raises(ValueError, match="scope is incomplete"):
            _capability_audit_from_record(
                malformed_scope,
                scanned_operation_id=result.operation_id,
            )

        mismatched_scan = deepcopy(record)
        with pytest.raises(ValueError, match="JSONL operation identity"):
            _capability_audit_from_record(
                mismatched_scan,
                scanned_operation_id="not-the-record-operation",
            )

        mismatched_source = deepcopy(record)
        mismatched_source.source = "tampered.source"
        with pytest.raises(ValueError, match="source identity"):
            _capability_audit_from_record(
                mismatched_source,
                scanned_operation_id=result.operation_id,
            )

        mismatched_ledger = deepcopy(record)
        mismatched_ledger.meta["ledger_event_id"] = "capability-ledger-tampered"
        with pytest.raises(ValueError, match="ledger identity"):
            _capability_audit_from_record(
                mismatched_ledger,
                scanned_operation_id=result.operation_id,
            )
    finally:
        store.close()
