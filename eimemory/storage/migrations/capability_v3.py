"""Storage v2 schema for dynamic L5 capability data.

This module owns DDL only.  It is intentionally forward-only and installs no
dual writes or data backfill during construction.  All capability rows carry
both the exact runtime owner scope and the contract's logical capability scope.
"""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any


CAPABILITY_V3_SCHEMA_MIGRATION = "capability.v3.schema.v1"
CAPABILITY_V3_BACKFILL_MIGRATION = "capability.v3.backfill.v1"
CAPABILITY_V3_SCHEMA_VERSION = "capability.v3"

SCOPE_COLUMNS: tuple[str, ...] = (
    "tenant_id",
    "agent_id",
    "workspace_id",
    "user_id",
    "capability_scope",
)


class CapabilityV3SchemaError(RuntimeError):
    """The installed v3 schema does not satisfy its immutable contract."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCOPE_DDL = """
    tenant_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    capability_scope TEXT NOT NULL,
"""
_SCOPE_KEY = "tenant_id, agent_id, workspace_id, user_id, capability_scope"


_TABLE_DDL: tuple[str, ...] = (
    f"""
    CREATE TABLE IF NOT EXISTS capability_definitions (
        {_SCOPE_DDL}
        capability_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        description TEXT NOT NULL,
        owner TEXT NOT NULL,
        status TEXT NOT NULL,
        risk_tier TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        supersedes_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        definition_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, capability_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_revisions (
        {_SCOPE_DDL}
        revision_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        contract_json TEXT NOT NULL,
        compatibility TEXT NOT NULL,
        supersedes_revision_id TEXT NOT NULL DEFAULT '',
        compatibility_policy_id TEXT NOT NULL DEFAULT '',
        compatibility_policy_digest TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        contract_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, revision_id),
        UNIQUE ({_SCOPE_KEY}, capability_id, revision_id),
        UNIQUE ({_SCOPE_KEY}, capability_id, contract_digest),
        FOREIGN KEY ({_SCOPE_KEY}, capability_id)
          REFERENCES capability_definitions ({_SCOPE_KEY}, capability_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_relations (
        {_SCOPE_DDL}
        relation_id TEXT NOT NULL,
        source_capability_id TEXT NOT NULL,
        target_capability_id TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        relation_policy_json TEXT NOT NULL,
        status TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        relation_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, relation_id),
        UNIQUE ({_SCOPE_KEY}, source_capability_id, target_capability_id, relation_type, relation_digest),
        FOREIGN KEY ({_SCOPE_KEY}, source_capability_id)
          REFERENCES capability_definitions ({_SCOPE_KEY}, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, target_capability_id)
          REFERENCES capability_definitions ({_SCOPE_KEY}, capability_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_bindings (
        {_SCOPE_DDL}
        binding_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        capability_revision_id TEXT NOT NULL,
        provider_kind TEXT NOT NULL,
        provider_instance_id TEXT NOT NULL,
        implementation_digest TEXT NOT NULL,
        operations_json TEXT NOT NULL,
        limits_json TEXT NOT NULL,
        environment_fingerprint_json TEXT NOT NULL,
        applicability_json TEXT NOT NULL,
        advertisement_evidence_refs_json TEXT NOT NULL,
        status TEXT NOT NULL,
        advertised_at TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        binding_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, binding_id),
        UNIQUE ({_SCOPE_KEY}, binding_id, capability_revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, capability_revision_id, capability_id)
          REFERENCES capability_revisions ({_SCOPE_KEY}, revision_id, capability_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS adapter_capability_advertisements (
        {_SCOPE_DDL}
        advertisement_id TEXT NOT NULL,
        binding_id TEXT NOT NULL,
        adapter_id TEXT NOT NULL,
        provider_kind TEXT NOT NULL,
        provider_instance_id TEXT NOT NULL,
        status TEXT NOT NULL,
        advertised_at TEXT NOT NULL,
        expires_at TEXT NOT NULL DEFAULT '',
        operations_json TEXT NOT NULL,
        limits_json TEXT NOT NULL,
        environment_fingerprint_json TEXT NOT NULL,
        applicability_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        advertisement_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, advertisement_id),
        UNIQUE ({_SCOPE_KEY}, binding_id, advertisement_digest),
        FOREIGN KEY ({_SCOPE_KEY}, binding_id)
          REFERENCES capability_bindings ({_SCOPE_KEY}, binding_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_profiles (
        {_SCOPE_DDL}
        profile_id TEXT NOT NULL,
        requirements_json TEXT NOT NULL,
        status TEXT NOT NULL,
        profile_revision TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        profile_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, profile_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_entity_lifecycle_events (
        {_SCOPE_DDL}
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        status TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        state_digest TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, entity_type, entity_id, state_version),
        UNIQUE ({_SCOPE_KEY}, entity_type, entity_id, state_digest)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_entity_current_states (
        {_SCOPE_DDL}
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        status TEXT NOT NULL,
        state_version INTEGER NOT NULL,
        state_digest TEXT NOT NULL,
        effective_at TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, entity_type, entity_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS evaluation_specs (
        {_SCOPE_DDL}
        eval_spec_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        capability_revision_id TEXT NOT NULL,
        profile_id TEXT,
        grader_type TEXT NOT NULL,
        executor_id TEXT NOT NULL,
        executor_contract_digest TEXT NOT NULL,
        fixture_refs_json TEXT NOT NULL,
        checks_json TEXT NOT NULL,
        required_metrics_json TEXT NOT NULL,
        retry_policy_json TEXT NOT NULL,
        stability_policy_json TEXT NOT NULL,
        applicability_json TEXT NOT NULL,
        resource_budget_json TEXT NOT NULL,
        binding_selector_json TEXT NOT NULL,
        model_grader_policy_json TEXT NOT NULL,
        status TEXT NOT NULL,
        spec_revision TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        spec_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, eval_spec_id),
        UNIQUE ({_SCOPE_KEY}, eval_spec_id, capability_revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, capability_revision_id, capability_id)
          REFERENCES capability_revisions ({_SCOPE_KEY}, revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, profile_id)
          REFERENCES capability_profiles ({_SCOPE_KEY}, profile_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS evaluation_runs (
        {_SCOPE_DDL}
        run_id TEXT NOT NULL,
        eval_spec_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        capability_revision_id TEXT NOT NULL,
        provider_binding_id TEXT NOT NULL,
        profile_id TEXT,
        idempotency_key TEXT NOT NULL,
        run_state TEXT NOT NULL,
        verdict TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL,
        executor_id TEXT NOT NULL DEFAULT '',
        executor_contract_digest TEXT NOT NULL DEFAULT '',
        grader_id TEXT NOT NULL DEFAULT '',
        grader_revision TEXT NOT NULL DEFAULT '',
        input_digest TEXT NOT NULL DEFAULT '',
        output_digest TEXT NOT NULL DEFAULT '',
        evidence_digest TEXT NOT NULL DEFAULT '',
        evidence_refs_json TEXT NOT NULL DEFAULT '[]',
        environment_fingerprint_json TEXT NOT NULL DEFAULT '{{}}',
        provenance_json TEXT NOT NULL DEFAULT '{{}}',
        metrics_json TEXT NOT NULL DEFAULT '{{}}',
        error_taxonomy_json TEXT NOT NULL DEFAULT '{{}}',
        deployment_authority_json TEXT NOT NULL DEFAULT '{{}}',
        schema_version TEXT NOT NULL,
        run_digest TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL,
        requested_at TEXT NOT NULL,
        started_at TEXT NOT NULL DEFAULT '',
        finished_at TEXT NOT NULL DEFAULT '',
        terminal_at TEXT NOT NULL DEFAULT '',
        row_version INTEGER NOT NULL DEFAULT 1,
        current_digest TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, run_id),
        UNIQUE ({_SCOPE_KEY}, eval_spec_id, provider_binding_id, source, idempotency_key),
        FOREIGN KEY ({_SCOPE_KEY}, eval_spec_id)
          REFERENCES evaluation_specs ({_SCOPE_KEY}, eval_spec_id),
        FOREIGN KEY ({_SCOPE_KEY}, eval_spec_id, capability_revision_id, capability_id)
          REFERENCES evaluation_specs ({_SCOPE_KEY}, eval_spec_id, capability_revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, capability_revision_id, capability_id)
          REFERENCES capability_revisions ({_SCOPE_KEY}, revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, provider_binding_id)
          REFERENCES capability_bindings ({_SCOPE_KEY}, binding_id),
        FOREIGN KEY ({_SCOPE_KEY}, provider_binding_id, capability_revision_id, capability_id)
          REFERENCES capability_bindings ({_SCOPE_KEY}, binding_id, capability_revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, profile_id)
          REFERENCES capability_profiles ({_SCOPE_KEY}, profile_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_ledger_events (
        {_SCOPE_DDL}
        ledger_event_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        event_type TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        payload_digest TEXT NOT NULL,
        audit_record_id TEXT NOT NULL,
        audit_export_operation_id TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, ledger_event_id),
        UNIQUE ({_SCOPE_KEY}, idempotency_key)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_observations (
        {_SCOPE_DDL}
        observation_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        capability_revision_id TEXT NOT NULL,
        provider_binding_id TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        ledger_event_id TEXT NOT NULL,
        verdict TEXT NOT NULL,
        source TEXT NOT NULL,
        executor_id TEXT NOT NULL,
        executor_contract_digest TEXT NOT NULL,
        grader_id TEXT NOT NULL,
        grader_revision TEXT NOT NULL,
        input_digest TEXT NOT NULL,
        output_digest TEXT NOT NULL,
        evidence_digest TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        environment_fingerprint_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        metrics_json TEXT NOT NULL,
        error_taxonomy_json TEXT NOT NULL,
        deployment_authority_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        observation_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        observed_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, observation_id),
        UNIQUE ({_SCOPE_KEY}, capability_revision_id, provider_binding_id, source, idempotency_key),
        FOREIGN KEY ({_SCOPE_KEY}, capability_revision_id, capability_id)
          REFERENCES capability_revisions ({_SCOPE_KEY}, revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, provider_binding_id)
          REFERENCES capability_bindings ({_SCOPE_KEY}, binding_id),
        FOREIGN KEY ({_SCOPE_KEY}, provider_binding_id, capability_revision_id, capability_id)
          REFERENCES capability_bindings ({_SCOPE_KEY}, binding_id, capability_revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, ledger_event_id)
          REFERENCES capability_ledger_events ({_SCOPE_KEY}, ledger_event_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_knowledge_links (
        {_SCOPE_DDL}
        link_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        capability_revision_id TEXT NOT NULL,
        knowledge_record_id TEXT NOT NULL,
        knowledge_storage_key TEXT NOT NULL DEFAULT '',
        knowledge_record_digest TEXT NOT NULL DEFAULT '',
        relation_type TEXT NOT NULL,
        source_status TEXT NOT NULL,
        applicability TEXT NOT NULL,
        source_trust TEXT NOT NULL,
        review_state TEXT NOT NULL,
        temporal_validity_json TEXT NOT NULL,
        environment_constraints_json TEXT NOT NULL,
        contradiction_state TEXT NOT NULL,
        applicability_score REAL NOT NULL,
        applicability_evidence_refs_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        link_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, link_id),
        FOREIGN KEY ({_SCOPE_KEY}, capability_revision_id, capability_id)
          REFERENCES capability_revisions ({_SCOPE_KEY}, revision_id, capability_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_state_snapshots (
        {_SCOPE_DDL}
        snapshot_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        capability_revision_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        provider_binding_id TEXT,
        maturity TEXT NOT NULL,
        confidence REAL NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        sample_sufficiency_json TEXT NOT NULL,
        reliability_metrics_json TEXT NOT NULL,
        latest_success_ref TEXT NOT NULL DEFAULT '',
        latest_failure_ref TEXT NOT NULL DEFAULT '',
        regression_streak INTEGER NOT NULL,
        dependency_state_json TEXT NOT NULL,
        knowledge_applicability_json TEXT NOT NULL,
        provider_applicability_json TEXT NOT NULL,
        environment_applicability_json TEXT NOT NULL,
        input_watermark TEXT NOT NULL,
        algorithm_revision TEXT NOT NULL,
        reason_codes_json TEXT NOT NULL,
        input_digests_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        snapshot_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, snapshot_id),
        UNIQUE ({_SCOPE_KEY}, snapshot_id, profile_id),
        FOREIGN KEY ({_SCOPE_KEY}, capability_revision_id, capability_id)
          REFERENCES capability_revisions ({_SCOPE_KEY}, revision_id, capability_id),
        FOREIGN KEY ({_SCOPE_KEY}, profile_id)
          REFERENCES capability_profiles ({_SCOPE_KEY}, profile_id),
        FOREIGN KEY ({_SCOPE_KEY}, provider_binding_id)
          REFERENCES capability_bindings ({_SCOPE_KEY}, binding_id),
        FOREIGN KEY ({_SCOPE_KEY}, provider_binding_id, capability_revision_id, capability_id)
          REFERENCES capability_bindings ({_SCOPE_KEY}, binding_id, capability_revision_id, capability_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS l5_assessments_v3 (
        {_SCOPE_DDL}
        assessment_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        loop_maturity TEXT NOT NULL,
        capability_readiness_json TEXT NOT NULL,
        adapter_readiness_json TEXT NOT NULL,
        deployment_assurance_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        algorithm_revision TEXT NOT NULL,
        input_watermarks_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        assessment_digest TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, assessment_id),
        UNIQUE ({_SCOPE_KEY}, assessment_id, profile_id),
        FOREIGN KEY ({_SCOPE_KEY}, profile_id)
          REFERENCES capability_profiles ({_SCOPE_KEY}, profile_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS l5_assessment_snapshot_refs (
        {_SCOPE_DDL}
        assessment_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        ref_digest TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, assessment_id, snapshot_id),
        FOREIGN KEY ({_SCOPE_KEY}, assessment_id, profile_id)
          REFERENCES l5_assessments_v3 ({_SCOPE_KEY}, assessment_id, profile_id),
        FOREIGN KEY ({_SCOPE_KEY}, snapshot_id, profile_id)
          REFERENCES capability_state_snapshots ({_SCOPE_KEY}, snapshot_id, profile_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS l5_assessment_readiness_refs (
        {_SCOPE_DDL}
        assessment_id TEXT NOT NULL,
        profile_id TEXT NOT NULL,
        capability_id TEXT NOT NULL,
        capability_revision_id TEXT NOT NULL,
        readiness_binding_id TEXT NOT NULL,
        snapshot_id TEXT NOT NULL,
        maturity TEXT NOT NULL,
        readiness_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        ref_digest TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, assessment_id, capability_revision_id, readiness_binding_id),
        FOREIGN KEY ({_SCOPE_KEY}, assessment_id, profile_id)
          REFERENCES l5_assessments_v3 ({_SCOPE_KEY}, assessment_id, profile_id),
        FOREIGN KEY ({_SCOPE_KEY}, snapshot_id, profile_id)
          REFERENCES capability_state_snapshots ({_SCOPE_KEY}, snapshot_id, profile_id),
        FOREIGN KEY ({_SCOPE_KEY}, capability_revision_id, capability_id)
          REFERENCES capability_revisions ({_SCOPE_KEY}, revision_id, capability_id)
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS capability_operation_journal (
        {_SCOPE_DDL}
        operation_id TEXT NOT NULL,
        request_key TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        ledger_event_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        result_digest TEXT NOT NULL,
        audit_record_id TEXT NOT NULL,
        audit_export_operation_id TEXT NOT NULL,
        audit_exported_at TEXT NOT NULL DEFAULT '',
        provenance_json TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        PRIMARY KEY ({_SCOPE_KEY}, operation_id),
        UNIQUE ({_SCOPE_KEY}, request_key),
        FOREIGN KEY ({_SCOPE_KEY}, ledger_event_id)
          REFERENCES capability_ledger_events ({_SCOPE_KEY}, ledger_event_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS capability_v3_migration_state (
        migration_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        phase TEXT NOT NULL,
        cursor TEXT NOT NULL DEFAULT '',
        rows_scanned INTEGER NOT NULL DEFAULT 0,
        rows_written INTEGER NOT NULL DEFAULT 0,
        rows_skipped INTEGER NOT NULL DEFAULT 0,
        batch_count INTEGER NOT NULL DEFAULT 0,
        source_watermark TEXT NOT NULL DEFAULT '',
        source_digest TEXT NOT NULL DEFAULT '',
        target_digest TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '',
        started_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        finished_at TEXT NOT NULL DEFAULT '',
        restart_count INTEGER NOT NULL DEFAULT 0
    )
    """,
)


_INDEX_DDL: tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS idx_capability_definitions_scope_status ON capability_definitions ({_SCOPE_KEY}, status, capability_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_revisions_scope_capability_effective ON capability_revisions ({_SCOPE_KEY}, capability_id, status, created_at DESC, revision_id DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_bindings_scope_provider_capability ON capability_bindings ({_SCOPE_KEY}, provider_kind, provider_instance_id, capability_id, status, advertised_at DESC, binding_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_advertisements_scope_binding_time ON adapter_capability_advertisements ({_SCOPE_KEY}, binding_id, status, advertised_at DESC, advertisement_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_profiles_scope_status ON capability_profiles ({_SCOPE_KEY}, status, profile_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_entity_current_scope_type_status ON capability_entity_current_states ({_SCOPE_KEY}, entity_type, status, effective_at DESC, entity_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_entity_lifecycle_scope_entity_time ON capability_entity_lifecycle_events ({_SCOPE_KEY}, entity_type, entity_id, effective_at DESC, state_version DESC)",
    f"CREATE INDEX IF NOT EXISTS idx_evaluation_specs_scope_capability_profile ON evaluation_specs ({_SCOPE_KEY}, capability_revision_id, profile_id, status, created_at DESC, eval_spec_id)",
    f"CREATE INDEX IF NOT EXISTS idx_evaluation_runs_scope_capability_binding_state ON evaluation_runs ({_SCOPE_KEY}, capability_revision_id, provider_binding_id, profile_id, run_state, finished_at DESC, run_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_ledger_scope_entity_time ON capability_ledger_events ({_SCOPE_KEY}, entity_type, entity_id, occurred_at DESC, ledger_event_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_observations_scope_capability_binding_time ON capability_observations ({_SCOPE_KEY}, capability_revision_id, provider_binding_id, observed_at DESC, observation_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_observations_scope_verdict_time ON capability_observations ({_SCOPE_KEY}, capability_revision_id, provider_binding_id, verdict, observed_at DESC, observation_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_knowledge_links_scope_capability_knowledge ON capability_knowledge_links ({_SCOPE_KEY}, capability_revision_id, knowledge_record_id, created_at DESC, link_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_snapshots_scope_profile_watermark ON capability_state_snapshots ({_SCOPE_KEY}, profile_id, capability_revision_id, input_watermark DESC, computed_at DESC, snapshot_id)",
    f"CREATE INDEX IF NOT EXISTS idx_l5_assessments_scope_profile_created ON l5_assessments_v3 ({_SCOPE_KEY}, profile_id, created_at DESC, assessment_id)",
    f"CREATE INDEX IF NOT EXISTS idx_l5_assessment_readiness_scope_assessment ON l5_assessment_readiness_refs ({_SCOPE_KEY}, assessment_id, capability_revision_id, readiness_binding_id)",
    f"CREATE INDEX IF NOT EXISTS idx_capability_journal_scope_request ON capability_operation_journal ({_SCOPE_KEY}, request_key, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_capability_journal_export_operation ON capability_operation_journal (audit_export_operation_id)",
)


_REQUIRED_COLUMNS: dict[str, frozenset[str]] = {
    "capability_definitions": frozenset({"capability_id", "definition_digest", "payload_json", "capability_scope"}),
    "capability_revisions": frozenset({"revision_id", "capability_id", "contract_digest", "payload_json", "capability_scope"}),
    "capability_relations": frozenset({"relation_id", "relation_digest", "payload_json", "capability_scope"}),
    "capability_bindings": frozenset({"binding_id", "capability_revision_id", "binding_digest", "payload_json", "capability_scope"}),
    "adapter_capability_advertisements": frozenset({"advertisement_id", "binding_id", "advertisement_digest", "payload_json", "capability_scope"}),
    "capability_profiles": frozenset({"profile_id", "profile_revision", "profile_digest", "payload_json", "capability_scope"}),
    "capability_entity_lifecycle_events": frozenset({"entity_type", "entity_id", "state_version", "status", "state_digest", "capability_scope"}),
    "capability_entity_current_states": frozenset({"entity_type", "entity_id", "status", "state_version", "state_digest", "capability_scope"}),
    "evaluation_specs": frozenset({"eval_spec_id", "capability_revision_id", "spec_digest", "payload_json", "capability_scope"}),
    "evaluation_runs": frozenset({"run_id", "run_state", "row_version", "current_digest", "payload_json", "capability_scope"}),
    "capability_ledger_events": frozenset({"ledger_event_id", "idempotency_key", "payload_digest", "payload_json", "audit_record_id", "audit_export_operation_id", "capability_scope"}),
    "capability_observations": frozenset({"observation_id", "ledger_event_id", "observation_digest", "payload_json", "capability_scope"}),
    "capability_knowledge_links": frozenset({"link_id", "knowledge_record_id", "link_digest", "payload_json", "capability_scope"}),
    "capability_state_snapshots": frozenset({"snapshot_id", "profile_id", "snapshot_digest", "payload_json", "capability_scope"}),
    "l5_assessments_v3": frozenset({"assessment_id", "profile_id", "assessment_digest", "payload_json", "capability_scope"}),
    "l5_assessment_snapshot_refs": frozenset({"assessment_id", "snapshot_id", "profile_id", "ref_digest", "capability_scope"}),
    "l5_assessment_readiness_refs": frozenset({"assessment_id", "profile_id", "capability_id", "capability_revision_id", "readiness_binding_id", "snapshot_id", "ref_digest", "capability_scope"}),
    "capability_operation_journal": frozenset({"operation_id", "request_key", "result_digest", "audit_export_operation_id", "audit_exported_at", "capability_scope"}),
    "capability_v3_migration_state": frozenset({"migration_id", "status", "cursor", "rows_scanned", "target_digest", "last_error"}),
}

_REQUIRED_INDEXES = frozenset(
    statement.split("idx_", 1)[1].split(" ", 1)[0]
    for statement in _INDEX_DDL
)

_REQUIRED_FOREIGN_KEY_TARGETS: dict[str, frozenset[str]] = {
    "capability_revisions": frozenset({"capability_definitions"}),
    "capability_relations": frozenset({"capability_definitions"}),
    "capability_bindings": frozenset({"capability_revisions"}),
    "adapter_capability_advertisements": frozenset({"capability_bindings"}),
    "evaluation_specs": frozenset({"capability_revisions", "capability_profiles"}),
    "evaluation_runs": frozenset({"evaluation_specs", "capability_revisions", "capability_bindings", "capability_profiles"}),
    "capability_observations": frozenset({"capability_revisions", "capability_bindings", "capability_ledger_events"}),
    "capability_knowledge_links": frozenset({"capability_revisions"}),
    "capability_state_snapshots": frozenset({"capability_revisions", "capability_bindings", "capability_profiles"}),
    "l5_assessments_v3": frozenset({"capability_profiles"}),
    "l5_assessment_snapshot_refs": frozenset({"l5_assessments_v3", "capability_state_snapshots"}),
    "l5_assessment_readiness_refs": frozenset({"l5_assessments_v3", "capability_state_snapshots", "capability_revisions"}),
    "capability_operation_journal": frozenset({"capability_ledger_events"}),
}

# Parent-table presence alone is not enough for chained capability facts: a
# child may otherwise point at a valid row for a different revision, binding,
# or profile.  Keep the critical composite shapes structural so a stale schema
# fails closed instead of silently weakening the v3 contract.
_REQUIRED_FOREIGN_KEY_SHAPES: dict[str, frozenset[tuple[str, tuple[str, ...], tuple[str, ...]]]] = {
    "evaluation_runs": frozenset(
        {
            (
                "evaluation_specs",
                (*SCOPE_COLUMNS, "eval_spec_id", "capability_revision_id", "capability_id"),
                (*SCOPE_COLUMNS, "eval_spec_id", "capability_revision_id", "capability_id"),
            ),
            (
                "capability_bindings",
                (*SCOPE_COLUMNS, "provider_binding_id", "capability_revision_id", "capability_id"),
                (*SCOPE_COLUMNS, "binding_id", "capability_revision_id", "capability_id"),
            ),
        }
    ),
    "capability_observations": frozenset(
        {
            (
                "capability_bindings",
                (*SCOPE_COLUMNS, "provider_binding_id", "capability_revision_id", "capability_id"),
                (*SCOPE_COLUMNS, "binding_id", "capability_revision_id", "capability_id"),
            ),
        }
    ),
    "capability_state_snapshots": frozenset(
        {
            (
                "capability_bindings",
                (*SCOPE_COLUMNS, "provider_binding_id", "capability_revision_id", "capability_id"),
                (*SCOPE_COLUMNS, "binding_id", "capability_revision_id", "capability_id"),
            ),
        }
    ),
    "l5_assessment_snapshot_refs": frozenset(
        {
            (
                "l5_assessments_v3",
                (*SCOPE_COLUMNS, "assessment_id", "profile_id"),
                (*SCOPE_COLUMNS, "assessment_id", "profile_id"),
            ),
            (
                "capability_state_snapshots",
                (*SCOPE_COLUMNS, "snapshot_id", "profile_id"),
                (*SCOPE_COLUMNS, "snapshot_id", "profile_id"),
            ),
        }
    ),
    "l5_assessment_readiness_refs": frozenset(
        {
            (
                "l5_assessments_v3",
                (*SCOPE_COLUMNS, "assessment_id", "profile_id"),
                (*SCOPE_COLUMNS, "assessment_id", "profile_id"),
            ),
            (
                "capability_state_snapshots",
                (*SCOPE_COLUMNS, "snapshot_id", "profile_id"),
                (*SCOPE_COLUMNS, "snapshot_id", "profile_id"),
            ),
            (
                "capability_revisions",
                (*SCOPE_COLUMNS, "capability_revision_id", "capability_id"),
                (*SCOPE_COLUMNS, "revision_id", "capability_id"),
            ),
        }
    ),
}


def _mark_schema_migration(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations(migration_id, applied_at) VALUES (?, ?)",
        (CAPABILITY_V3_SCHEMA_MIGRATION, _utc_now()),
    )


def _schema_migration_marked(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id=?",
        (CAPABILITY_V3_SCHEMA_MIGRATION,),
    ).fetchone()
    return row is not None


def ensure_capability_v3_schema(conn: sqlite3.Connection) -> None:
    """Transactionally install the v3 schema and its observable backfill state."""

    # The common path must remain read-only and bounded: in particular, do not
    # run PRAGMA foreign_key_check here because it scans growing evidence tables.
    # Full data integrity checks are an explicit maintenance operation below.
    if is_capability_v3_schema_ready(conn) and _schema_migration_marked(conn):
        return
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for statement in _TABLE_DDL:
            conn.execute(statement)
        _ensure_additive_schema_columns(conn)
        for statement in _INDEX_DDL:
            conn.execute(statement)
        conn.execute(
            """
            INSERT OR IGNORE INTO capability_v3_migration_state (
                migration_id, status, phase, updated_at
            ) VALUES (?, 'not_scheduled', 'not_scheduled', ?)
            """,
            (CAPABILITY_V3_BACKFILL_MIGRATION, _utc_now()),
        )
        _mark_schema_migration(conn)
        _assert_capability_v3_schema(conn)
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def _ensure_additive_schema_columns(conn: sqlite3.Connection) -> None:
    """Add pre-release v3 columns without turning startup into a data migration."""

    journal_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(capability_operation_journal)").fetchall()
    }
    if "audit_exported_at" not in journal_columns:
        conn.execute(
            "ALTER TABLE capability_operation_journal "
            "ADD COLUMN audit_exported_at TEXT NOT NULL DEFAULT ''"
        )


def _assert_capability_v3_schema(conn: sqlite3.Connection) -> None:
    for table, expected_columns in _REQUIRED_COLUMNS.items():
        actual = {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        missing = expected_columns - actual
        if missing:
            raise CapabilityV3SchemaError(
                f"capability v3 table {table} is missing required columns: {sorted(missing)}"
            )
    index_names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    }
    missing_indexes = {f"idx_{name}" for name in _REQUIRED_INDEXES} - index_names
    if missing_indexes:
        raise CapabilityV3SchemaError(
            f"capability v3 schema is missing indexes: {sorted(missing_indexes)}"
        )
    for table, expected_targets in _REQUIRED_FOREIGN_KEY_TARGETS.items():
        foreign_key_rows = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        actual_targets = {str(row[2]) for row in foreign_key_rows}
        missing_targets = expected_targets - actual_targets
        if missing_targets:
            raise CapabilityV3SchemaError(
                f"capability v3 table {table} is missing foreign keys to: {sorted(missing_targets)}"
            )
        expected_shapes = _REQUIRED_FOREIGN_KEY_SHAPES.get(table, frozenset())
        if expected_shapes:
            grouped: dict[int, list[sqlite3.Row | tuple[Any, ...]]] = {}
            for row in foreign_key_rows:
                grouped.setdefault(int(row[0]), []).append(row)
            actual_shapes = {
                (
                    str(rows[0][2]),
                    tuple(str(item[3]) for item in sorted(rows, key=lambda item: int(item[1]))),
                    tuple(str(item[4]) for item in sorted(rows, key=lambda item: int(item[1]))),
                )
                for rows in grouped.values()
            }
            missing_shapes = expected_shapes - actual_shapes
            if missing_shapes:
                raise CapabilityV3SchemaError(
                    f"capability v3 table {table} is missing composite foreign-key shapes"
                )


def is_capability_v3_schema_ready(conn: sqlite3.Connection) -> bool:
    try:
        _assert_capability_v3_schema(conn)
    except (sqlite3.DatabaseError, CapabilityV3SchemaError):
        return False
    return True


def capability_v3_foreign_key_check(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Run an explicit, potentially expensive data-integrity probe.

    This is intentionally not used at RuntimeStore construction.  Call it from
    bounded/offline maintenance or a deployment verification job instead.
    """

    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    return [
        {
            "table": str(row[0]),
            "rowid": int(row[1]),
            "parent": str(row[2]),
            "fkid": int(row[3]),
        }
        for row in rows
    ]


def capability_v3_backfill_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM capability_v3_migration_state WHERE migration_id = ?",
        (CAPABILITY_V3_BACKFILL_MIGRATION,),
    ).fetchone()
    if row is None:
        return {
            "migration_id": CAPABILITY_V3_BACKFILL_MIGRATION,
            "status": "not_installed",
            "phase": "not_installed",
        }
    return {str(key): row[key] for key in row.keys()}


def capability_v3_backfill_is_scheduled(conn: sqlite3.Connection) -> bool:
    state = capability_v3_backfill_state(conn)
    return str(state.get("status") or "") in {"running", "paused"}


def apply_capability_v3_backfill_batch(
    conn: sqlite3.Connection,
    *,
    batch_size: int,
    max_seconds: float,
    offline: bool = False,
) -> dict[str, Any]:
    """Expose bounded backfill semantics without scheduling a backfill yet.

    WP3 installs the durable cursor/count/error contract only.  A later
    forward migration owns source inventory and dual-write/backfill activation.
    """

    bounded_rows = max(1, min(2_000, int(batch_size)))
    bounded_seconds = max(0.001, min(60.0, float(max_seconds)))
    state = capability_v3_backfill_state(conn)
    return {
        "ok": True,
        "migration_id": CAPABILITY_V3_BACKFILL_MIGRATION,
        "scheduled": capability_v3_backfill_is_scheduled(conn),
        "processed": 0,
        "batch_size": bounded_rows,
        "max_seconds": bounded_seconds,
        "offline": bool(offline),
        "state": state,
    }
