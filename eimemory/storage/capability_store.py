"""Transaction-local repository for Capability Storage v3 data.

This module deliberately does not own a SQLite connection lifecycle.  It is
created by :class:`RuntimeStore` inside a ``BEGIN IMMEDIATE`` transaction, does
not commit or flush exports itself, and records the exact audit work that must
be exported after commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any, Callable, Mapping

from eimemory.capabilities.contracts import (
    normalize_opaque_id,
    normalize_sha256,
    require_timestamp,
)
from eimemory.capabilities.models import (
    ADVERTISEMENT_STATUSES,
    AdapterCapabilityAdvertisement,
    BINDING_STATUSES,
    DEFINITION_STATUSES,
    EVAL_SPEC_STATUSES,
    PROFILE_STATUSES,
    REVISION_STATUSES,
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
    legacy_profile_payload,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.jsonl import canonical_payload_json, payload_digest
from eimemory.storage.migrations.capability_v3 import CAPABILITY_V3_SCHEMA_VERSION
from eimemory.storage.sqlite_store import SqliteRecordStore


class CapabilityStoreError(RuntimeError):
    """Base error for the capability storage boundary."""


class CapabilityConflict(CapabilityStoreError):
    """An immutable capability entity or CAS target conflicts with stored data."""


class CapabilityIdempotencyConflict(CapabilityConflict):
    """One exact-scope idempotency key was reused with a different request."""


@dataclass(frozen=True, slots=True)
class StoredCapabilityEntity:
    entity_type: str
    entity_id: str
    entity_digest: str
    operation_id: str
    ledger_event_id: str
    idempotent: bool


@dataclass(frozen=True, slots=True)
class LifecycleTransitionReceipt:
    """The durable, compare-and-swap result of an effective lifecycle change."""

    entity_type: str
    entity_id: str
    # ``entity_digest`` is the immutable lifecycle-result digest so generic
    # audit replay can validate it like every other capability mutation.
    entity_digest: str
    # The descriptor digest is separately retained for the next transition's
    # strong precondition; it is never confused with the mutable state result.
    target_entity_digest: str
    status: str
    state_version: int
    state_digest: str
    effective_at: str
    operation_id: str
    ledger_event_id: str
    idempotent: bool


@dataclass(frozen=True, slots=True)
class EffectiveCapabilityEntity:
    """A bounded descriptor view joined to lifecycle state, never a DB row."""

    entity_type: str
    entity_id: str
    entity_digest: str
    payload: Mapping[str, Any]
    status: str
    state_version: int
    state_digest: str
    effective_at: str


@dataclass(frozen=True, slots=True)
class PendingCapabilityAudit:
    """Committed-domain audit work that RuntimeStore must export post-commit."""

    operation_id: str
    ledger_event_id: str
    audit_record_id: str
    action: str
    entity_type: str
    entity_id: str
    entity_digest: str
    scope: ScopeRef
    capability_scope: str
    payload: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class _EntityWrite:
    entity_type: str
    action: str
    table: str
    entity_id: str
    digest_column: str
    entity_digest: str
    capability_scope: str
    created_at: str
    columns: tuple[str, ...]
    values: tuple[Any, ...]
    payload: Mapping[str, Any]
    evidence_refs: tuple[str, ...]
    provenance: Mapping[str, Any]
    storage_context: Mapping[str, Any] = field(default_factory=dict)


_SCOPE_COLUMNS = (
    "tenant_id",
    "agent_id",
    "workspace_id",
    "user_id",
    "capability_scope",
)
_SCOPE_SQL = "tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND capability_scope=?"

_MODEL_BY_ENTITY_TYPE = {
    "definition": CapabilityDefinition,
    "revision": CapabilityRevision,
    "relation": CapabilityRelation,
    "binding": CapabilityBinding,
    "advertisement": AdapterCapabilityAdvertisement,
    "profile": CapabilityProfile,
    "evaluation_spec": EvaluationSpec,
    "evaluation_run": EvaluationRun,
    "observation": CapabilityObservation,
    "knowledge_link": CapabilityKnowledgeLink,
    "snapshot": CapabilityStateSnapshot,
    "assessment": L5AssessmentV3,
}

# Descriptor rows are immutable.  The effective status belongs to a separate
# append-only lifecycle stream so deprecation/quarantine never rewrites an old
# contract or historical observation.  Keep this map explicit: an unknown
# entity type must fail closed rather than acquire a generic mutable state.
_LIFECYCLE_ENTITY_SPECS: dict[str, tuple[str, str, str, frozenset[str]]] = {
    "definition": ("capability_definitions", "capability_id", "definition_digest", DEFINITION_STATUSES),
    "revision": ("capability_revisions", "revision_id", "contract_digest", REVISION_STATUSES),
    "relation": ("capability_relations", "relation_id", "relation_digest", DEFINITION_STATUSES),
    "binding": ("capability_bindings", "binding_id", "binding_digest", BINDING_STATUSES),
    "advertisement": (
        "adapter_capability_advertisements",
        "advertisement_id",
        "advertisement_digest",
        ADVERTISEMENT_STATUSES,
    ),
    "profile": ("capability_profiles", "profile_id", "profile_digest", PROFILE_STATUSES),
    "evaluation_spec": ("evaluation_specs", "eval_spec_id", "spec_digest", EVAL_SPEC_STATUSES),
}

_LIFECYCLE_TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "definition": {
        "discovered": frozenset({"active", "deprecated", "quarantined", "retired"}),
        "active": frozenset({"deprecated", "quarantined", "retired"}),
        "deprecated": frozenset({"quarantined", "retired"}),
        "quarantined": frozenset({"retired"}),
        "retired": frozenset(),
    },
    "revision": {
        "active": frozenset({"deprecated", "quarantined", "retired"}),
        "deprecated": frozenset({"quarantined", "retired"}),
        "quarantined": frozenset({"retired"}),
        "retired": frozenset(),
    },
    "relation": {
        "discovered": frozenset({"active", "deprecated", "quarantined", "retired"}),
        "active": frozenset({"deprecated", "quarantined", "retired"}),
        "deprecated": frozenset({"quarantined", "retired"}),
        "quarantined": frozenset({"retired"}),
        "retired": frozenset(),
    },
    "binding": {
        "active": frozenset({"stale", "disabled", "deprecated", "quarantined"}),
        "stale": frozenset({"active", "disabled", "deprecated", "quarantined"}),
        "disabled": frozenset({"active", "deprecated", "quarantined"}),
        "deprecated": frozenset({"disabled", "quarantined"}),
        "quarantined": frozenset({"disabled", "deprecated"}),
    },
    "advertisement": {
        "active": frozenset({"stale", "disabled", "deprecated", "quarantined"}),
        "stale": frozenset({"active", "disabled", "deprecated", "quarantined"}),
        "disabled": frozenset({"active", "deprecated", "quarantined"}),
        "deprecated": frozenset({"disabled", "quarantined"}),
        "quarantined": frozenset({"disabled", "deprecated"}),
    },
    "profile": {
        "active": frozenset({"deprecated", "retired"}),
        "deprecated": frozenset({"retired"}),
        "retired": frozenset(),
    },
    "evaluation_spec": {
        "active": frozenset({"deprecated", "quarantined", "retired"}),
        "deprecated": frozenset({"quarantined", "retired"}),
        "quarantined": frozenset({"retired"}),
        "retired": frozenset(),
    },
}
_CAPABILITY_STORE_TRANSACTION_TOKEN = object()


def _scope_values(scope: ScopeRef, capability_scope: str) -> tuple[str, str, str, str, str]:
    return (
        scope.tenant_id,
        scope.agent_id,
        scope.workspace_id,
        scope.user_id,
        capability_scope,
    )


def _scope_payload(scope: ScopeRef) -> dict[str, str]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }


def _json(value: Any) -> str:
    return canonical_payload_json(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _normalized_storage_context(value: Mapping[str, Any]) -> dict[str, str]:
    """Return the small, typed SQL context that participates in a write identity.

    Some normalized rows need a relational value that is intentionally outside
    the portable domain dataclass (for example a local profile id or a record
    storage key).  Treating that value as a loose side channel would let the
    same request key silently mean two different writes.  Keep it scalar and
    canonical so it can be part of the request digest, audit envelope, and
    immutable-row comparison.
    """

    normalized: dict[str, str] = {}
    for raw_key, raw_value in sorted(value.items(), key=lambda item: str(item[0])):
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise CapabilityStoreError("capability storage context requires non-empty string keys")
        if raw_value is None:
            normalized[raw_key] = ""
        elif isinstance(raw_value, str):
            normalized[raw_key] = raw_value
        else:
            raise CapabilityStoreError(
                f"capability storage context {raw_key!r} must be a string or null"
            )
    return normalized


def _strict_scope_from_audit(value: Any) -> ScopeRef:
    """Decode an audit scope without ScopeRef's legacy default fallbacks."""

    if not isinstance(value, Mapping):
        raise CapabilityStoreError("capability audit lacks an exact runtime scope")
    required = ("tenant_id", "agent_id", "workspace_id", "user_id")
    if any(key not in value or not isinstance(value[key], str) for key in required):
        raise CapabilityStoreError("capability audit scope is incomplete or malformed")
    if not str(value["tenant_id"]).strip():
        raise CapabilityStoreError("capability audit scope has an empty tenant_id")
    return ScopeRef(
        tenant_id=str(value["tenant_id"]),
        agent_id=str(value["agent_id"]),
        workspace_id=str(value["workspace_id"]),
        user_id=str(value["user_id"]),
    )


def _sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value)


def _advertised_numeric_limits_within_binding(
    advertised: Mapping[str, Any],
    binding: Mapping[str, Any],
) -> bool:
    """Reject a numeric host limit that expands a bound implementation limit.

    Advertisements may add host-only diagnostic limits or omit a bound limit,
    but they may not increase a numeric limit already declared by the provider
    binding.  Nested mappings retain the same rule without imposing a fixed
    global limits taxonomy.
    """

    for key, advertised_value in advertised.items():
        if key not in binding:
            continue
        binding_value = binding[key]
        if isinstance(advertised_value, Mapping) and isinstance(binding_value, Mapping):
            if not _advertised_numeric_limits_within_binding(advertised_value, binding_value):
                return False
            continue
        if (
            isinstance(advertised_value, (int, float))
            and not isinstance(advertised_value, bool)
            and isinstance(binding_value, (int, float))
            and not isinstance(binding_value, bool)
            and advertised_value > binding_value
        ):
            return False
    return True


class CapabilityStore:
    """Typed SQL mapper used only inside a RuntimeStore domain transaction.

    Definition-like records are immutable.  A repeat with the same exact
    payload is idempotent; the same identity or exact request key with a
    different payload fails closed.  Observations are first appended to the
    immutable capability ledger and then indexed in ``capability_observations``.
    """

    def __init__(
        self,
        sqlite: SqliteRecordStore,
        *,
        _transaction_token: object | None = None,
        _read_only: bool = False,
    ) -> None:
        if _transaction_token is not _CAPABILITY_STORE_TRANSACTION_TOKEN:
            raise CapabilityStoreError(
                "CapabilityStore is transaction-local; use RuntimeStore.mutate_capabilities_atomically"
            )
        if not sqlite.conn.in_transaction:
            raise CapabilityStoreError("CapabilityStore requires an active RuntimeStore transaction")
        self._sqlite = sqlite
        self._read_only = bool(_read_only)
        self._pending_audits: list[PendingCapabilityAudit] = []
        self._savepoint_counter = 0
        # A rebuild replays already durable facts.  It must retain their
        # historical timestamps even when they are later than the rebuilding
        # machine's clock; online writes remain subject to current-time guards.
        self._audit_replay_depth = 0

    @property
    def pending_audits(self) -> tuple[PendingCapabilityAudit, ...]:
        return tuple(self._pending_audits)

    def register_definition(
        self,
        definition: CapabilityDefinition,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(self._definition_write(definition), scope=scope, request_key=request_key)

    def register_revision(
        self,
        revision: CapabilityRevision,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(self._revision_write(revision), scope=scope, request_key=request_key)

    def register_relation(
        self,
        relation: CapabilityRelation,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        write = self._relation_write(relation)

        def assert_new_active_relation_is_acyclic() -> None:
            self._assert_relation_activation_is_acyclic(
                source_capability_id=relation.source_capability_id,
                target_capability_id=relation.target_capability_id,
                relation_type=relation.relation_type,
                relation_id=relation.relation_id,
                scope=scope,
                capability_scope=relation.scope,
            )

        # Resolve exact retries and immutable-identity conflicts before
        # inspecting the *current* graph.  The descriptor is immutable: a
        # replay must never be treated as a request to reactivate an old edge.
        return self._write(
            write,
            scope=scope,
            request_key=request_key,
            before_insert=assert_new_active_relation_is_acyclic if relation.status == "active" else None,
        )

    def register_binding(
        self,
        binding: CapabilityBinding,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(self._binding_write(binding), scope=scope, request_key=request_key)

    def register_advertisement(
        self,
        advertisement: AdapterCapabilityAdvertisement,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        """Persist one immutable adapter statement for an existing binding.

        The database FK protects binding existence.  This explicit repository
        check also protects the denormalized provider/revision fields carried
        by the advertisement, so a caller cannot attach an otherwise-valid
        provider statement to a different binding in the same scope.
        """

        return self._write(
            self._advertisement_write(advertisement),
            scope=scope,
            request_key=request_key,
            before_insert=lambda: self._assert_advertisement_binding_matches(
                advertisement,
                scope=scope,
            ),
        )

    def register_profile(
        self,
        profile: CapabilityProfile,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(self._profile_write(profile), scope=scope, request_key=request_key)

    def transition_lifecycle(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_digest: str,
        target_status: str,
        scope: ScopeRef,
        capability_scope: str,
        expected_state_version: int,
        expected_state_digest: str,
        effective_at: str,
        reason: str,
        provenance: Mapping[str, Any],
        request_key: str = "",
    ) -> LifecycleTransitionReceipt:
        """Append one auditable effective-status transition using strong CAS.

        Descriptor contracts are never updated in place.  A transition is a
        separate capability-domain mutation, so it receives the same ledger,
        journal, deterministic audit envelope, and JSONL recovery guarantees as
        a registration write.
        """

        normalized_entity_type = str(entity_type or "").strip()
        spec = _LIFECYCLE_ENTITY_SPECS.get(normalized_entity_type)
        if spec is None:
            raise CapabilityStoreError(f"unsupported capability lifecycle entity type: {entity_type!r}")
        normalized_entity_id = normalize_opaque_id(entity_id, field="lifecycle entity_id")
        normalized_digest = normalize_sha256(entity_digest, field="lifecycle entity_digest")
        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        if isinstance(expected_state_version, bool) or not isinstance(expected_state_version, int) or expected_state_version < 1:
            raise CapabilityStoreError("expected_state_version must be a positive integer")
        normalized_expected_digest = normalize_sha256(
            expected_state_digest, field="expected_state_digest"
        )
        normalized_effective_at = require_timestamp(effective_at, field="effective_at")
        if not isinstance(provenance, Mapping):
            raise CapabilityStoreError("lifecycle provenance must be a mapping")
        normalized_provenance = _mapping(provenance)
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise CapabilityStoreError("lifecycle transition reason is required")
        if len(normalized_reason) > 2048:
            raise CapabilityStoreError("lifecycle transition reason exceeds 2048 characters")
        allowed_statuses = spec[3]
        normalized_target_status = str(target_status or "").strip()
        if normalized_target_status not in allowed_statuses:
            raise CapabilityStoreError(
                f"unsupported lifecycle target status {normalized_target_status!r} for {normalized_entity_type}"
            )
        return self._transition_lifecycle(
            entity_type=normalized_entity_type,
            entity_id=normalized_entity_id,
            entity_digest=normalized_digest,
            target_status=normalized_target_status,
            scope=scope,
            capability_scope=normalized_scope,
            expected_state_version=expected_state_version,
            expected_state_digest=normalized_expected_digest,
            effective_at=normalized_effective_at,
            reason=normalized_reason,
            provenance=normalized_provenance,
            request_key=request_key,
        )

    def register_evaluation_spec(
        self,
        spec: EvaluationSpec,
        *,
        scope: ScopeRef,
        profile_id: str | None = None,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(
            self._evaluation_spec_write(spec, profile_id=profile_id),
            scope=scope,
            request_key=request_key,
        )

    def record_evaluation_run(
        self,
        run: EvaluationRun,
        *,
        scope: ScopeRef,
        profile_id: str | None = None,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(
            self._evaluation_run_write(run, profile_id=profile_id),
            scope=scope,
            request_key=request_key or f"evaluation-run:{run.source}:{run.idempotency_key}",
        )

    def append_observation(
        self,
        observation: CapabilityObservation,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(
            self._observation_write(observation),
            scope=scope,
            request_key=request_key or f"observation:{observation.source}:{observation.idempotency_key}",
        )

    def register_knowledge_link(
        self,
        link: CapabilityKnowledgeLink,
        *,
        scope: ScopeRef,
        knowledge_storage_key: str = "",
        knowledge_record_digest: str = "",
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(
            self._knowledge_link_write(
                link,
                knowledge_storage_key=knowledge_storage_key,
                knowledge_record_digest=knowledge_record_digest,
            ),
            scope=scope,
            request_key=request_key,
        )

    def register_snapshot(
        self,
        snapshot: CapabilityStateSnapshot,
        *,
        scope: ScopeRef,
        provider_binding_id: str | None = None,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(
            self._snapshot_write(snapshot, provider_binding_id=provider_binding_id),
            scope=scope,
            request_key=request_key,
        )

    def register_assessment(
        self,
        assessment: L5AssessmentV3,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(self._assessment_write(assessment), scope=scope, request_key=request_key)

    def operation_journal(self, *, scope: ScopeRef, capability_scope: str) -> list[dict[str, Any]]:
        rows = self._sqlite.conn.execute(
            "SELECT * FROM capability_operation_journal WHERE " + _SCOPE_SQL + " ORDER BY created_at, operation_id",
            _scope_values(scope, capability_scope),
        ).fetchall()
        return [{str(key): row[key] for key in row.keys()} for row in rows]

    def observation_rows(self, *, scope: ScopeRef, capability_scope: str) -> list[dict[str, Any]]:
        rows = self._sqlite.conn.execute(
            "SELECT * FROM capability_observations WHERE " + _SCOPE_SQL + " ORDER BY observed_at, observation_id",
            _scope_values(scope, capability_scope),
        ).fetchall()
        return [{str(key): row[key] for key in row.keys()} for row in rows]

    def list_observations(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        capability_id: str = "",
        capability_revision_id: str = "",
        provider_binding_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Read append-only observation DTOs through the exact-scope boundary.

        This deliberately has no legacy-user fallback and no implicit capability
        attribution.  A caller receives only canonical payloads plus the
        immutable ledger linkage needed to reproduce a v3 aggregation.
        """

        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_capability_id = (
            normalize_opaque_id(capability_id, field="capability_id") if capability_id else ""
        )
        normalized_revision_id = (
            normalize_opaque_id(capability_revision_id, field="capability_revision_id")
            if capability_revision_id
            else ""
        )
        normalized_binding_id = (
            normalize_opaque_id(provider_binding_id, field="provider_binding_id")
            if provider_binding_id
            else ""
        )
        normalized_since = require_timestamp(since, field="since", required=False) if since else ""
        normalized_until = require_timestamp(until, field="until", required=False) if until else ""
        if normalized_since and normalized_until and normalized_since > normalized_until:
            raise CapabilityStoreError("observation since must not be later than until")
        normalized_limit = max(1, min(500, int(limit)))
        where_parts = [
            "tenant_id=?", "agent_id=?", "workspace_id=?", "user_id=?", "capability_scope=?"
        ]
        params: list[Any] = list(_scope_values(scope, normalized_scope))
        if normalized_capability_id:
            where_parts.append("capability_id=?")
            params.append(normalized_capability_id)
        if normalized_revision_id:
            where_parts.append("capability_revision_id=?")
            params.append(normalized_revision_id)
        if normalized_binding_id:
            where_parts.append("provider_binding_id=?")
            params.append(normalized_binding_id)
        if normalized_since:
            where_parts.append("observed_at>=?")
            params.append(normalized_since)
        if normalized_until:
            where_parts.append("observed_at<=?")
            params.append(normalized_until)
        params.append(normalized_limit)
        rows = self._sqlite.conn.execute(
            f"""
            SELECT observation_id, ledger_event_id, observation_digest, observed_at, payload_json
            FROM capability_observations
            WHERE {' AND '.join(where_parts)}
            ORDER BY observed_at DESC, observation_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CapabilityStoreError("stored capability observation payload is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise CapabilityStoreError("stored capability observation payload is not a JSON object")
            result.append(
                {
                    "observation_id": str(row["observation_id"]),
                    "ledger_event_id": str(row["ledger_event_id"]),
                    "observation_digest": str(row["observation_digest"]),
                    "observed_at": str(row["observed_at"]),
                    "payload": payload,
                }
            )
        return result

    def list_evaluation_runs(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        capability_revision_id: str = "",
        provider_binding_id: str = "",
        eval_spec_id: str = "",
        profile_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return bounded terminal evaluation DTOs with exact-scope ownership."""

        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        filters = {
            "capability_revision_id": capability_revision_id,
            "provider_binding_id": provider_binding_id,
            "eval_spec_id": eval_spec_id,
            "profile_id": profile_id,
        }
        where_parts = [
            "tenant_id=?", "agent_id=?", "workspace_id=?", "user_id=?", "capability_scope=?"
        ]
        params: list[Any] = list(_scope_values(scope, normalized_scope))
        for column, raw_value in filters.items():
            if raw_value:
                where_parts.append(f"{column}=?")
                params.append(normalize_opaque_id(raw_value, field=column))
        params.append(max(1, min(500, int(limit))))
        rows = self._sqlite.conn.execute(
            f"""
            SELECT run_id, run_digest, run_state, finished_at, payload_json
            FROM evaluation_runs
            WHERE {' AND '.join(where_parts)}
            ORDER BY finished_at DESC, run_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CapabilityStoreError("stored evaluation run payload is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise CapabilityStoreError("stored evaluation run payload is not a JSON object")
            result.append(
                {
                    "run_id": str(row["run_id"]),
                    "run_digest": str(row["run_digest"]),
                    "run_state": str(row["run_state"]),
                    "finished_at": str(row["finished_at"]),
                    "payload": payload,
                }
            )
        return result

    def list_knowledge_links(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        capability_id: str = "",
        capability_revision_id: str = "",
        knowledge_record_id: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Read typed knowledge links without exposing SQLite rows or fallback scope."""

        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        filters = {
            "capability_id": capability_id,
            "capability_revision_id": capability_revision_id,
            "knowledge_record_id": knowledge_record_id,
        }
        where_parts = [
            "tenant_id=?", "agent_id=?", "workspace_id=?", "user_id=?", "capability_scope=?"
        ]
        params: list[Any] = list(_scope_values(scope, normalized_scope))
        for column, raw_value in filters.items():
            if raw_value:
                where_parts.append(f"{column}=?")
                params.append(normalize_opaque_id(raw_value, field=column))
        params.append(max(1, min(500, int(limit))))
        rows = self._sqlite.conn.execute(
            f"""
            SELECT link_id, link_digest, knowledge_storage_key, knowledge_record_digest, payload_json
            FROM capability_knowledge_links
            WHERE {' AND '.join(where_parts)}
            ORDER BY created_at DESC, link_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CapabilityStoreError("stored capability knowledge-link payload is not valid JSON") from exc
            if not isinstance(payload, dict):
                raise CapabilityStoreError("stored capability knowledge-link payload is not a JSON object")
            result.append(
                {
                    "link_id": str(row["link_id"]),
                    "link_digest": str(row["link_digest"]),
                    "knowledge_storage_key": str(row["knowledge_storage_key"]),
                    "knowledge_record_digest": str(row["knowledge_record_digest"]),
                    "payload": payload,
                }
            )
        return result

    def list_advertisements(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        binding_id: str = "",
        adapter_id: str = "",
        provider_kind: str = "",
        provider_instance_id: str = "",
        status: str | None = "active",
        at_time: str = "",
        fresh_at: str = "",
        limit: int = 100,
    ) -> list[EffectiveCapabilityEntity]:
        """Read bounded provider advertisements without leaking SQL rows.

        ``fresh_at`` is intentionally independent from lifecycle ``at_time``:
        callers may inspect historical lifecycle state while asking whether an
        advertisement was valid at a separate operation timestamp.
        """

        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_binding_id = (
            normalize_opaque_id(binding_id, field="binding_id") if binding_id else ""
        )
        normalized_adapter_id = (
            normalize_opaque_id(adapter_id, field="adapter_id") if adapter_id else ""
        )
        normalized_provider_kind = (
            normalize_opaque_id(provider_kind, field="provider_kind") if provider_kind else ""
        )
        normalized_provider_instance_id = (
            normalize_opaque_id(provider_instance_id, field="provider_instance_id")
            if provider_instance_id
            else ""
        )
        normalized_fresh_at = (
            require_timestamp(fresh_at, field="fresh_at", required=False) if fresh_at else ""
        )
        normalized_limit = max(1, min(500, int(limit)))
        # Read the maximum bounded candidate set before applying payload-only
        # fields.  The typed table deliberately indexes binding/time; the
        # portable payload carries protocol-only adapter metadata.
        candidates = self.list_effective_entities(
            entity_type="advertisement",
            scope=scope,
            capability_scope=normalized_scope,
            status=status,
            at_time=at_time,
            limit=500,
        )
        result: list[EffectiveCapabilityEntity] = []
        for candidate in candidates:
            payload = candidate.payload
            if normalized_binding_id and str(payload.get("binding_id") or "") != normalized_binding_id:
                continue
            if normalized_adapter_id and str(payload.get("adapter_id") or "") != normalized_adapter_id:
                continue
            if normalized_provider_kind and str(payload.get("provider_kind") or "") != normalized_provider_kind:
                continue
            if (
                normalized_provider_instance_id
                and str(payload.get("provider_instance_id") or "") != normalized_provider_instance_id
            ):
                continue
            if normalized_fresh_at and not (
                str(payload.get("advertised_at") or "") <= normalized_fresh_at
                < str(payload.get("expires_at") or "")
            ):
                continue
            result.append(candidate)
            if len(result) >= normalized_limit:
                break
        return result

    def list_adapter_advertisements(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        adapter_id: str = "",
        binding_id: str = "",
        at_time: str = "",
        limit: int = 100,
        fresh_at: str = "",
        provider_kind: str = "",
        provider_instance_id: str = "",
        status: str | None = "active",
    ) -> list[EffectiveCapabilityEntity]:
        """Publicly named bounded adapter-advertisement DTO query.

        ``list_advertisements`` remains the internal concise spelling; this
        name is the stable repository surface for readiness/projector callers.
        Descriptor payloads have already passed the advertisement model's
        diagnostic redaction and lifecycle state is returned separately.
        """

        return self.list_advertisements(
            scope=scope,
            capability_scope=capability_scope,
            adapter_id=adapter_id,
            binding_id=binding_id,
            at_time=at_time,
            limit=limit,
            fresh_at=fresh_at,
            provider_kind=provider_kind,
            provider_instance_id=provider_instance_id,
            status=status,
        )

    def list_effective_entities(
        self,
        *,
        entity_type: str,
        scope: ScopeRef,
        capability_scope: str,
        status: str | None = None,
        at_time: str = "",
        entity_id: str = "",
        capability_id: str = "",
        cursor: str = "",
        limit: int = 100,
    ) -> list[EffectiveCapabilityEntity]:
        """Return lifecycle-effective descriptors through the transaction boundary.

        This is intentionally a bounded DTO query.  Callers never receive a
        SQLite row or a mutable ``payload_json`` string, and the static status in
        the descriptor table is never treated as current truth.
        """

        spec = _LIFECYCLE_ENTITY_SPECS.get(str(entity_type or ""))
        if spec is None:
            raise CapabilityStoreError(f"unsupported capability entity type: {entity_type!r}")
        table, entity_id_column, digest_column, allowed_statuses = spec
        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_limit = max(1, min(500, int(limit)))
        normalized_entity_id = normalize_opaque_id(entity_id, field="entity_id") if entity_id else ""
        normalized_capability_id = (
            normalize_opaque_id(capability_id, field="capability_id") if capability_id else ""
        )
        normalized_cursor = normalize_opaque_id(cursor, field="cursor") if cursor else ""
        normalized_at_time = require_timestamp(at_time, field="at_time", required=False) if at_time else ""
        if status is not None and str(status) not in allowed_statuses:
            raise CapabilityStoreError(f"unsupported lifecycle status {status!r} for {entity_type}")

        scope_values = _scope_values(scope, normalized_scope)
        where_parts = [
            "d.tenant_id=?", "d.agent_id=?", "d.workspace_id=?", "d.user_id=?", "d.capability_scope=?"
        ]
        params: list[Any] = list(scope_values)
        if normalized_entity_id:
            where_parts.append(f"d.{entity_id_column}=?")
            params.append(normalized_entity_id)
        elif normalized_cursor:
            where_parts.append(f"d.{entity_id_column}>?")
            params.append(normalized_cursor)
        if normalized_capability_id:
            if entity_type not in {"definition", "revision", "binding"}:
                raise CapabilityStoreError(f"{entity_type} does not support capability_id filtering")
            if entity_type != "definition":
                where_parts.append("d.capability_id=?")
                params.append(normalized_capability_id)
            elif normalized_entity_id and normalized_entity_id != normalized_capability_id:
                return []
            elif not normalized_entity_id:
                where_parts.append("d.capability_id=?")
                params.append(normalized_capability_id)
        if normalized_at_time:
            join = (
                "JOIN capability_entity_lifecycle_events AS state "
                "ON state.tenant_id=d.tenant_id AND state.agent_id=d.agent_id "
                "AND state.workspace_id=d.workspace_id AND state.user_id=d.user_id "
                "AND state.capability_scope=d.capability_scope "
                f"AND state.entity_type=? AND state.entity_id=d.{entity_id_column}"
            )
            params.insert(0, entity_type)
            where_parts.append(
                "state.state_version=("
                "SELECT later.state_version FROM capability_entity_lifecycle_events AS later "
                "WHERE later.tenant_id=d.tenant_id AND later.agent_id=d.agent_id "
                "AND later.workspace_id=d.workspace_id AND later.user_id=d.user_id "
                "AND later.capability_scope=d.capability_scope "
                f"AND later.entity_type=? AND later.entity_id=d.{entity_id_column} "
                "AND later.effective_at<=? "
                "ORDER BY later.effective_at DESC, later.state_version DESC LIMIT 1)"
            )
            params.extend((entity_type, normalized_at_time))
        else:
            join = (
                "JOIN capability_entity_current_states AS state "
                "ON state.tenant_id=d.tenant_id AND state.agent_id=d.agent_id "
                "AND state.workspace_id=d.workspace_id AND state.user_id=d.user_id "
                "AND state.capability_scope=d.capability_scope "
                f"AND state.entity_type=? AND state.entity_id=d.{entity_id_column}"
            )
            params.insert(0, entity_type)
        if status is not None:
            where_parts.append("state.status=?")
            params.append(str(status))
        params.append(normalized_limit)
        rows = self._sqlite.conn.execute(
            f"""
            SELECT d.{entity_id_column} AS entity_id, d.{digest_column} AS entity_digest,
                   d.payload_json AS payload_json, state.status AS status,
                   state.state_version AS state_version, state.state_digest AS state_digest,
                   state.effective_at AS effective_at
            FROM {table} AS d
            {join}
            WHERE {' AND '.join(where_parts)}
            ORDER BY d.{entity_id_column}
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [self._effective_entity_from_row(entity_type, row) for row in rows]

    def list_lifecycle_events(
        self,
        *,
        entity_type: str,
        entity_id: str,
        scope: ScopeRef,
        capability_scope: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return an immutable, bounded lifecycle history for one entity.

        Lifecycle provenance is part of the existing capability authority.  A
        read-only history query lets readiness consumers verify facts such as
        incubation preflight passes without treating a process-local runtime
        attribute as evidence or creating a second state store.
        """

        spec = _LIFECYCLE_ENTITY_SPECS.get(str(entity_type or ""))
        if spec is None:
            raise CapabilityStoreError(f"unsupported capability entity type: {entity_type!r}")
        normalized_entity_id = normalize_opaque_id(entity_id, field="entity_id")
        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_limit = max(1, min(500, int(limit)))
        rows = self._sqlite.conn.execute(
            """
            SELECT entity_type, entity_id, state_version, status, effective_at,
                   reason, provenance_json, schema_version, state_digest, created_at
            FROM capability_entity_lifecycle_events
            WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=?
              AND capability_scope=? AND entity_type=? AND entity_id=?
            ORDER BY state_version ASC
            LIMIT ?
            """,
            (
                *_scope_values(scope, normalized_scope),
                str(entity_type),
                normalized_entity_id,
                normalized_limit,
            ),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                provenance = json.loads(str(row["provenance_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CapabilityStoreError("stored lifecycle provenance is not valid JSON") from exc
            if not isinstance(provenance, dict):
                raise CapabilityStoreError("stored lifecycle provenance is not an object")
            result.append(
                {
                    "entity_type": str(row["entity_type"]),
                    "entity_id": str(row["entity_id"]),
                    "state_version": int(row["state_version"]),
                    "status": str(row["status"]),
                    "effective_at": str(row["effective_at"]),
                    "reason": str(row["reason"]),
                    "provenance": provenance,
                    "schema_version": str(row["schema_version"]),
                    "state_digest": str(row["state_digest"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def list_profile_revisions(
        self,
        *,
        profile_key: str,
        scope: ScopeRef,
        capability_scope: str,
        status: str | None = "active",
        at_time: str = "",
        limit: int = 100,
    ) -> list[EffectiveCapabilityEntity]:
        """Resolve immutable Profile revisions through the typed lineage index."""

        normalized_key = normalize_opaque_id(profile_key, field="profile_key")
        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_limit = max(1, min(500, int(limit)))
        normalized_at_time = require_timestamp(at_time, field="at_time", required=False) if at_time else ""
        if status is not None and str(status) not in PROFILE_STATUSES:
            raise CapabilityStoreError(f"unsupported profile lifecycle status {status!r}")
        scope_values = _scope_values(scope, normalized_scope)
        where_parts = [
            "lineage.tenant_id=?", "lineage.agent_id=?", "lineage.workspace_id=?",
            "lineage.user_id=?", "lineage.capability_scope=?", "lineage.profile_key=?",
        ]
        params: list[Any] = [*scope_values, normalized_key]
        if normalized_at_time:
            join = (
                "JOIN capability_entity_lifecycle_events AS state "
                "ON state.tenant_id=lineage.tenant_id AND state.agent_id=lineage.agent_id "
                "AND state.workspace_id=lineage.workspace_id AND state.user_id=lineage.user_id "
                "AND state.capability_scope=lineage.capability_scope "
                "AND state.entity_type='profile' AND state.entity_id=lineage.profile_id"
            )
            where_parts.append(
                "state.state_version=("
                "SELECT later.state_version FROM capability_entity_lifecycle_events AS later "
                "WHERE later.tenant_id=lineage.tenant_id AND later.agent_id=lineage.agent_id "
                "AND later.workspace_id=lineage.workspace_id AND later.user_id=lineage.user_id "
                "AND later.capability_scope=lineage.capability_scope "
                "AND later.entity_type='profile' AND later.entity_id=lineage.profile_id "
                "AND later.effective_at<=? "
                "ORDER BY later.effective_at DESC, later.state_version DESC LIMIT 1)"
            )
            params.append(normalized_at_time)
        else:
            join = (
                "JOIN capability_entity_current_states AS state "
                "ON state.tenant_id=lineage.tenant_id AND state.agent_id=lineage.agent_id "
                "AND state.workspace_id=lineage.workspace_id AND state.user_id=lineage.user_id "
                "AND state.capability_scope=lineage.capability_scope "
                "AND state.entity_type='profile' AND state.entity_id=lineage.profile_id"
            )
        if status is not None:
            where_parts.append("state.status=?")
            params.append(str(status))
        params.append(normalized_limit)
        rows = self._sqlite.conn.execute(
            f"""
            SELECT profile.profile_id AS entity_id, profile.profile_digest AS entity_digest,
                   profile.payload_json AS payload_json, state.status AS status,
                   state.state_version AS state_version, state.state_digest AS state_digest,
                   state.effective_at AS effective_at
            FROM capability_profile_lineage AS lineage
            JOIN capability_profiles AS profile
              ON profile.tenant_id=lineage.tenant_id AND profile.agent_id=lineage.agent_id
             AND profile.workspace_id=lineage.workspace_id AND profile.user_id=lineage.user_id
             AND profile.capability_scope=lineage.capability_scope AND profile.profile_id=lineage.profile_id
            {join}
            WHERE {' AND '.join(where_parts)}
            ORDER BY state.effective_at DESC, state.state_version DESC, profile.profile_id DESC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        if rows:
            return [self._effective_entity_from_row("profile", row) for row in rows]

        # WP3 Profile descriptors predate the typed lineage index and carry no
        # ``profile_key``.  Preserve their old logical key (``profile_id``)
        # through a bounded read-only fallback; a later offline migration may
        # materialize lineage rows, but opening a Runtime never scans or writes
        # historical payloads merely to satisfy this lookup.
        legacy_where_parts = [
            "profile.tenant_id=?", "profile.agent_id=?", "profile.workspace_id=?",
            "profile.user_id=?", "profile.capability_scope=?", "profile.profile_id=?",
            "json_type(profile.payload_json, '$.profile_key') IS NULL",
        ]
        legacy_params: list[Any] = [*scope_values, normalized_key]
        if normalized_at_time:
            legacy_join = (
                "JOIN capability_entity_lifecycle_events AS state "
                "ON state.tenant_id=profile.tenant_id AND state.agent_id=profile.agent_id "
                "AND state.workspace_id=profile.workspace_id AND state.user_id=profile.user_id "
                "AND state.capability_scope=profile.capability_scope "
                "AND state.entity_type='profile' AND state.entity_id=profile.profile_id"
            )
            legacy_where_parts.append(
                "state.state_version=("
                "SELECT later.state_version FROM capability_entity_lifecycle_events AS later "
                "WHERE later.tenant_id=profile.tenant_id AND later.agent_id=profile.agent_id "
                "AND later.workspace_id=profile.workspace_id AND later.user_id=profile.user_id "
                "AND later.capability_scope=profile.capability_scope "
                "AND later.entity_type='profile' AND later.entity_id=profile.profile_id "
                "AND later.effective_at<=? "
                "ORDER BY later.effective_at DESC, later.state_version DESC LIMIT 1)"
            )
            legacy_params.append(normalized_at_time)
        else:
            legacy_join = (
                "JOIN capability_entity_current_states AS state "
                "ON state.tenant_id=profile.tenant_id AND state.agent_id=profile.agent_id "
                "AND state.workspace_id=profile.workspace_id AND state.user_id=profile.user_id "
                "AND state.capability_scope=profile.capability_scope "
                "AND state.entity_type='profile' AND state.entity_id=profile.profile_id"
            )
        if status is not None:
            legacy_where_parts.append("state.status=?")
            legacy_params.append(str(status))
        legacy_params.append(normalized_limit)
        legacy_rows = self._sqlite.conn.execute(
            f"""
            SELECT profile.profile_id AS entity_id, profile.profile_digest AS entity_digest,
                   profile.payload_json AS payload_json, state.status AS status,
                   state.state_version AS state_version, state.state_digest AS state_digest,
                   state.effective_at AS effective_at
            FROM capability_profiles AS profile
            {legacy_join}
            WHERE {' AND '.join(legacy_where_parts)}
            ORDER BY state.effective_at DESC, state.state_version DESC, profile.profile_id DESC
            LIMIT ?
            """,
            tuple(legacy_params),
        ).fetchall()
        return [self._effective_entity_from_row("profile", row) for row in legacy_rows]

    def find_seed_manifest_definitions(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        manifest_id: str,
        manifest_version: str,
        limit: int = 129,
    ) -> list[dict[str, str]]:
        """Find bounded immutable bootstrap receipts in the exact owner scope.

        The seed manifest's declaration provenance is the durable receipt.  It
        is queried inside the same write transaction as batch registration so
        a changed ``manifest_id/version`` cannot race a partial bootstrap or
        hide behind a paginated registry list.
        """

        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_manifest_id = normalize_opaque_id(manifest_id, field="manifest_id")
        normalized_manifest_version = normalize_opaque_id(manifest_version, field="manifest_version")
        normalized_limit = max(1, min(129, int(limit)))
        rows = self._sqlite.conn.execute(
            """
            SELECT capability_id, definition_digest, provenance_json
            FROM capability_definitions
            WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND capability_scope=?
              AND json_extract(provenance_json, '$.source')='eimemory.capability_seed_manifest'
              AND json_extract(provenance_json, '$.manifest_id')=?
              AND json_extract(provenance_json, '$.manifest_version')=?
            ORDER BY capability_id
            LIMIT ?
            """,
            (
                *_scope_values(scope, normalized_scope),
                normalized_manifest_id,
                normalized_manifest_version,
                normalized_limit,
            ),
        ).fetchall()
        result: list[dict[str, str]] = []
        for row in rows:
            try:
                provenance = json.loads(str(row["provenance_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CapabilityStoreError("stored seed manifest provenance is not valid JSON") from exc
            if not isinstance(provenance, dict):
                raise CapabilityStoreError("stored seed manifest provenance is not an object")
            result.append(
                {
                    "capability_id": str(row["capability_id"]),
                    "definition_digest": str(row["definition_digest"]),
                    "manifest_digest": str(provenance.get("manifest_digest") or ""),
                }
            )
        return result

    def find_seed_manifest_revisions(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        manifest_id: str,
        manifest_version: str,
        limit: int = 129,
    ) -> list[dict[str, str]]:
        """Find bounded immutable seed-revision receipts in the exact owner scope.

        Definitions alone cannot prove a manifest identity: a caller could
        otherwise attach an extra revision carrying copied manifest provenance.
        Keep this query separate from the generic registry reads so the
        declaration preflight remains exact, bounded, and transaction-local.
        """

        normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_manifest_id = normalize_opaque_id(manifest_id, field="manifest_id")
        normalized_manifest_version = normalize_opaque_id(manifest_version, field="manifest_version")
        normalized_limit = max(1, min(129, int(limit)))
        rows = self._sqlite.conn.execute(
            """
            SELECT revision_id, capability_id, contract_digest, provenance_json
            FROM capability_revisions
            WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND capability_scope=?
              AND json_extract(provenance_json, '$.source')='eimemory.capability_seed_manifest'
              AND json_extract(provenance_json, '$.manifest_id')=?
              AND json_extract(provenance_json, '$.manifest_version')=?
            ORDER BY revision_id
            LIMIT ?
            """,
            (
                *_scope_values(scope, normalized_scope),
                normalized_manifest_id,
                normalized_manifest_version,
                normalized_limit,
            ),
        ).fetchall()
        result: list[dict[str, str]] = []
        for row in rows:
            try:
                provenance = json.loads(str(row["provenance_json"]))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise CapabilityStoreError("stored seed manifest revision provenance is not valid JSON") from exc
            if not isinstance(provenance, dict):
                raise CapabilityStoreError("stored seed manifest revision provenance is not an object")
            result.append(
                {
                    "revision_id": str(row["revision_id"]),
                    "capability_id": str(row["capability_id"]),
                    "contract_digest": str(row["contract_digest"]),
                    "manifest_digest": str(provenance.get("manifest_digest") or ""),
                }
            )
        return result

    @staticmethod
    def _effective_entity_from_row(entity_type: str, row: Any) -> EffectiveCapabilityEntity:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError) as exc:
            raise CapabilityStoreError(f"stored {entity_type} payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise CapabilityStoreError(f"stored {entity_type} payload is not a JSON object")
        return EffectiveCapabilityEntity(
            entity_type=entity_type,
            entity_id=str(row["entity_id"]),
            entity_digest=str(row["entity_digest"]),
            payload=payload,
            status=str(row["status"]),
            state_version=int(row["state_version"]),
            state_digest=str(row["state_digest"]),
            effective_at=str(row["effective_at"]),
        )

    def _assert_relation_activation_is_acyclic(
        self,
        *,
        source_capability_id: str,
        target_capability_id: str,
        relation_type: str,
        relation_id: str,
        scope: ScopeRef,
        capability_scope: str,
    ) -> None:
        """Reject active dependency/composition/supersession cycles in-transaction.

        Reporting and discovery relations are deliberately not treated as graph
        constraints.  The graph is bounded so an imported relation set cannot
        turn a registration into an unbounded recursive traversal.
        """

        if relation_type not in {"depends_on", "composes", "supersedes"}:
            return
        relation_kinds = ("supersedes",) if relation_type == "supersedes" else ("depends_on", "composes")
        placeholders = ", ".join("?" for _ in relation_kinds)
        rows = self._sqlite.conn.execute(
            f"""
            SELECT relation.source_capability_id, relation.target_capability_id
            FROM capability_relations AS relation
            JOIN capability_entity_current_states AS state
              ON state.tenant_id=relation.tenant_id AND state.agent_id=relation.agent_id
             AND state.workspace_id=relation.workspace_id AND state.user_id=relation.user_id
             AND state.capability_scope=relation.capability_scope
             AND state.entity_type='relation' AND state.entity_id=relation.relation_id
            WHERE relation.tenant_id=? AND relation.agent_id=? AND relation.workspace_id=?
              AND relation.user_id=? AND relation.capability_scope=?
              AND state.status='active' AND relation.relation_type IN ({placeholders})
              AND relation.relation_id<>?
            ORDER BY relation.relation_id
            LIMIT 8193
            """,
            (*_scope_values(scope, capability_scope), *relation_kinds, relation_id),
        ).fetchall()
        if len(rows) > 8192:
            raise CapabilityStoreError("capability relation graph exceeds the bounded cycle-check limit")
        graph: dict[str, set[str]] = {}
        for row in rows:
            graph.setdefault(str(row["source_capability_id"]), set()).add(str(row["target_capability_id"]))
        graph.setdefault(source_capability_id, set()).add(target_capability_id)
        pending = [target_capability_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current == source_capability_id:
                raise CapabilityConflict(
                    f"active {relation_type} relation would create a capability cycle"
                )
            if current in seen:
                continue
            seen.add(current)
            if len(seen) > 8192:
                raise CapabilityStoreError("capability relation graph exceeds the bounded cycle-check limit")
            pending.extend(sorted(graph.get(current, ()), reverse=True))

    def _assert_advertisement_binding_matches(
        self,
        advertisement: AdapterCapabilityAdvertisement,
        *,
        scope: ScopeRef,
    ) -> None:
        """Require an advertisement to restate its bound provider contract.

        Provider kind/instance and revision digest are denormalized into the
        advertisement so readiness can be evaluated without changing a
        binding.  Verify them at the only trusted SQL boundary before making
        that immutable statement durable.
        """

        row = self._sqlite.conn.execute(
            """
            SELECT binding.provider_kind, binding.provider_instance_id,
                   binding.capability_revision_id, binding.operations_json,
                   binding.limits_json,
                   revision.contract_digest, revision.payload_json
            FROM capability_bindings AS binding
            JOIN capability_revisions AS revision
              ON revision.tenant_id=binding.tenant_id
             AND revision.agent_id=binding.agent_id
             AND revision.workspace_id=binding.workspace_id
             AND revision.user_id=binding.user_id
             AND revision.capability_scope=binding.capability_scope
             AND revision.revision_id=binding.capability_revision_id
             AND revision.capability_id=binding.capability_id
            WHERE binding.tenant_id=? AND binding.agent_id=?
              AND binding.workspace_id=? AND binding.user_id=?
              AND binding.capability_scope=? AND binding.binding_id=?
            """,
            (*_scope_values(scope, advertisement.scope), advertisement.binding_id),
        ).fetchone()
        if row is None:
            raise CapabilityStoreError(
                "adapter advertisement references an unknown binding in its exact scope"
            )
        expected = {
            "provider_kind": str(row["provider_kind"]),
            "provider_instance_id": str(row["provider_instance_id"]),
            "capability_revision_id": str(row["capability_revision_id"]),
            "contract_digest": str(row["contract_digest"]),
        }
        actual = {
            "provider_kind": advertisement.provider_kind,
            "provider_instance_id": advertisement.provider_instance_id,
            "capability_revision_id": advertisement.capability_revision_id,
            "contract_digest": advertisement.contract_digest,
        }
        mismatches = [key for key in expected if actual[key] != expected[key]]
        if mismatches:
            raise CapabilityStoreError(
                "adapter advertisement does not match its binding: "
                + ", ".join(sorted(mismatches))
            )
        try:
            binding_operations = _sequence(json.loads(str(row["operations_json"])))
            binding_limits = _mapping(json.loads(str(row["limits_json"])))
            revision_payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapabilityStoreError("adapter advertisement binding metadata is malformed") from exc
        if not set(advertisement.operations).issubset(set(binding_operations)):
            raise CapabilityStoreError("adapter advertisement claims an operation absent from its binding")
        if not _advertised_numeric_limits_within_binding(advertisement.limits, binding_limits):
            raise CapabilityStoreError("adapter advertisement expands a numeric binding limit")
        revision_contract = _mapping(_mapping(revision_payload).get("contract"))
        bound_side_effect_class = str(revision_contract.get("side_effect_class") or "")
        if bound_side_effect_class and advertisement.side_effect_class != bound_side_effect_class:
            raise CapabilityStoreError(
                "adapter advertisement side_effect_class does not match its revision contract"
            )

    def replay_audit(self, audit: Mapping[str, Any]) -> StoredCapabilityEntity | LifecycleTransitionReceipt:
        """Replay one already durable audit under explicit historical semantics."""

        self._audit_replay_depth += 1
        try:
            return self._replay_audit(audit)
        finally:
            self._audit_replay_depth -= 1

    def _replay_audit(self, audit: Mapping[str, Any]) -> StoredCapabilityEntity | LifecycleTransitionReceipt:
        """Rebuild a v3 entity from its durable record-stream audit payload.

        Rebuilds use the same immutable write rules and recompute the operation
        identity.  A mismatching operation or digest is a corruption signal,
        never an opportunity to silently accept a different historical fact.
        """

        if str(audit.get("schema") or "") != "capability.audit.v1":
            raise CapabilityStoreError("unsupported capability audit schema")
        entity_type = str(audit.get("entity_type") or "")
        raw_entity = audit.get("entity")
        if not isinstance(raw_entity, Mapping):
            raise CapabilityStoreError("capability audit lacks a structured entity payload")
        scope = _strict_scope_from_audit(audit.get("scope"))
        capability_scope = str(audit.get("capability_scope") or "")
        request_key = str(audit.get("request_key") or "")
        if not capability_scope:
            raise CapabilityStoreError("capability audit lacks a logical capability scope")
        if entity_type == "lifecycle_transition":
            result = self._replay_lifecycle_transition(
                raw_entity,
                scope=scope,
                capability_scope=capability_scope,
                request_key=request_key,
            )
        else:
            model_type = _MODEL_BY_ENTITY_TYPE.get(entity_type)
            if model_type is None:
                raise CapabilityStoreError("capability audit has an unsupported typed entity")
            context = _mapping(audit.get("storage_context"))
            if entity_type == "profile" and "profile_key" not in raw_entity:
                legacy_payload, legacy_digest = legacy_profile_payload(raw_entity)
                if str(legacy_payload["scope"]) != capability_scope:
                    raise CapabilityStoreError("capability audit scope does not match its legacy profile contract")
                result = self._write(
                    self._legacy_profile_write(legacy_payload, legacy_digest=legacy_digest),
                    scope=scope,
                    request_key=request_key,
                )
            else:
                entity = model_type(
                    **{
                        item.name: raw_entity[item.name]
                        for item in fields(model_type)
                        if item.init and item.name in raw_entity
                    }
                )
                if str(getattr(entity, "scope", "")) != capability_scope:
                    raise CapabilityStoreError("capability audit scope does not match its entity contract")
                if entity_type == "definition":
                    result = self.register_definition(entity, scope=scope, request_key=request_key)
                elif entity_type == "revision":
                    result = self.register_revision(entity, scope=scope, request_key=request_key)
                elif entity_type == "relation":
                    result = self.register_relation(entity, scope=scope, request_key=request_key)
                elif entity_type == "binding":
                    result = self.register_binding(entity, scope=scope, request_key=request_key)
                elif entity_type == "advertisement":
                    result = self.register_advertisement(entity, scope=scope, request_key=request_key)
                elif entity_type == "profile":
                    result = self.register_profile(entity, scope=scope, request_key=request_key)
                elif entity_type == "evaluation_spec":
                    result = self.register_evaluation_spec(
                        entity,
                        scope=scope,
                        profile_id=str(context.get("profile_id") or "") or None,
                        request_key=request_key,
                    )
                elif entity_type == "evaluation_run":
                    result = self.record_evaluation_run(
                        entity,
                        scope=scope,
                        profile_id=str(context.get("profile_id") or "") or None,
                        request_key=request_key,
                    )
                elif entity_type == "observation":
                    result = self.append_observation(entity, scope=scope, request_key=request_key)
                elif entity_type == "knowledge_link":
                    result = self.register_knowledge_link(
                        entity,
                        scope=scope,
                        knowledge_storage_key=str(context.get("knowledge_storage_key") or ""),
                        knowledge_record_digest=str(context.get("knowledge_record_digest") or ""),
                        request_key=request_key,
                    )
                elif entity_type == "snapshot":
                    result = self.register_snapshot(
                        entity,
                        scope=scope,
                        provider_binding_id=str(context.get("provider_binding_id") or "") or None,
                        request_key=request_key,
                    )
                else:
                    result = self.register_assessment(entity, scope=scope, request_key=request_key)
        if str(audit.get("operation_id") or "") != result.operation_id:
            raise CapabilityStoreError("capability audit operation identity does not reproduce")
        if str(audit.get("entity_digest") or "") != result.entity_digest:
            raise CapabilityStoreError("capability audit entity digest does not reproduce")
        if str(audit.get("ledger_event_id") or "") != result.ledger_event_id:
            raise CapabilityStoreError("capability audit ledger identity does not reproduce")
        return result

    def _replay_lifecycle_transition(
        self,
        payload: Mapping[str, Any],
        *,
        scope: ScopeRef,
        capability_scope: str,
        request_key: str,
    ) -> LifecycleTransitionReceipt:
        """Rehydrate a transition from its self-contained audit envelope."""

        required = (
            "entity_type",
            "entity_id",
            "entity_digest",
            "expected_state_version",
            "expected_state_digest",
            "target_status",
            "effective_at",
            "reason",
        )
        if any(key not in payload for key in required):
            raise CapabilityStoreError("capability lifecycle audit is incomplete")
        return self.transition_lifecycle(
            entity_type=str(payload["entity_type"]),
            entity_id=str(payload["entity_id"]),
            entity_digest=str(payload["entity_digest"]),
            target_status=str(payload["target_status"]),
            scope=scope,
            capability_scope=capability_scope,
            expected_state_version=int(payload["expected_state_version"]),
            expected_state_digest=str(payload["expected_state_digest"]),
            effective_at=str(payload["effective_at"]),
            reason=str(payload["reason"]),
            provenance=_mapping(payload.get("provenance")),
            request_key=request_key,
        )

    def _transition_lifecycle(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_digest: str,
        target_status: str,
        scope: ScopeRef,
        capability_scope: str,
        expected_state_version: int,
        expected_state_digest: str,
        effective_at: str,
        reason: str,
        provenance: Mapping[str, Any],
        request_key: str,
    ) -> LifecycleTransitionReceipt:
        if self._read_only:
            raise CapabilityStoreError("capability lifecycle mutation is not allowed in a read transaction")
        if not self._sqlite.conn.in_transaction:
            raise CapabilityStoreError("capability lifecycle mutation escaped its RuntimeStore transaction")
        self._savepoint_counter += 1
        savepoint = f"capability_lifecycle_{self._savepoint_counter}"
        self._sqlite.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self._transition_lifecycle_in_savepoint(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_digest=entity_digest,
                target_status=target_status,
                scope=scope,
                capability_scope=capability_scope,
                expected_state_version=expected_state_version,
                expected_state_digest=expected_state_digest,
                effective_at=effective_at,
                reason=reason,
                provenance=provenance,
                request_key=request_key,
            )
        except Exception:
            self._sqlite.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._sqlite.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self._sqlite.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result

    def _transition_lifecycle_in_savepoint(
        self,
        *,
        entity_type: str,
        entity_id: str,
        entity_digest: str,
        target_status: str,
        scope: ScopeRef,
        capability_scope: str,
        expected_state_version: int,
        expected_state_digest: str,
        effective_at: str,
        reason: str,
        provenance: Mapping[str, Any],
        request_key: str,
    ) -> LifecycleTransitionReceipt:
        table, entity_id_column, digest_column, _allowed_statuses = _LIFECYCLE_ENTITY_SPECS[entity_type]
        transition_id = f"lifecycle:{entity_type}:{entity_id}:{expected_state_version + 1}"
        clean_request_key = str(request_key or "").strip() or (
            f"lifecycle:{entity_type}:{entity_id}:{expected_state_version}:{expected_state_digest}:{target_status}:{effective_at}"
        )
        transition_payload = {
            "schema": "capability.lifecycle.v1",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_digest": entity_digest,
            "expected_state_version": expected_state_version,
            "expected_state_digest": expected_state_digest,
            "target_status": target_status,
            "effective_at": effective_at,
            "reason": reason,
            "provenance": dict(provenance),
        }
        request_payload = {
            "schema": "capability.operation.v1",
            "action": "lifecycle_transition",
            "entity_type": "lifecycle_transition",
            "entity_id": transition_id,
            "scope": _scope_payload(scope),
            "capability_scope": capability_scope,
            "request_key": clean_request_key,
            "transition": transition_payload,
        }
        request_digest = payload_digest(request_payload)
        existing = self._sqlite.conn.execute(
            "SELECT operation_id, ledger_event_id, request_digest, result_digest FROM capability_operation_journal WHERE "
            + _SCOPE_SQL
            + " AND request_key=?",
            (*_scope_values(scope, capability_scope), clean_request_key),
        ).fetchone()
        if existing is not None:
            if str(existing["request_digest"]) != request_digest:
                raise CapabilityIdempotencyConflict(
                    "capability lifecycle request key was reused with a different request"
                )
            event = self._sqlite.conn.execute(
                "SELECT status, state_version, state_digest, effective_at FROM capability_entity_lifecycle_events WHERE "
                + _SCOPE_SQL
                + " AND entity_type=? AND entity_id=? AND state_digest=?",
                (
                    *_scope_values(scope, capability_scope),
                    entity_type,
                    entity_id,
                    str(existing["result_digest"]),
                ),
            ).fetchone()
            if event is None:
                raise CapabilityStoreError("capability lifecycle journal is missing its immutable event")
            return LifecycleTransitionReceipt(
                entity_type=entity_type,
                entity_id=entity_id,
                entity_digest=str(event["state_digest"]),
                target_entity_digest=entity_digest,
                status=str(event["status"]),
                state_version=int(event["state_version"]),
                state_digest=str(event["state_digest"]),
                effective_at=str(event["effective_at"]),
                operation_id=str(existing["operation_id"]),
                ledger_event_id=str(existing["ledger_event_id"]),
                idempotent=True,
            )

        # Current-state reads intentionally do not execute a scheduler.  A
        # future-effective transition would otherwise rewrite the current
        # projection early.  Scheduling is a later, separately auditable
        # capability; online CAS transitions must take effect now or earlier.
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if not self._audit_replay_depth and effective_at > now:
            raise CapabilityConflict("online capability lifecycle transition cannot schedule a future effective_at")

        descriptor = self._sqlite.conn.execute(
            f"SELECT {digest_column} FROM {table} WHERE " + _SCOPE_SQL + f" AND {entity_id_column}=?",
            (*_scope_values(scope, capability_scope), entity_id),
        ).fetchone()
        if descriptor is None:
            raise CapabilityStoreError(f"cannot transition missing {entity_type} {entity_id}")
        if str(descriptor[digest_column]) != entity_digest:
            raise CapabilityConflict("capability lifecycle descriptor digest does not match")
        current = self._sqlite.conn.execute(
            "SELECT status, state_version, state_digest, effective_at FROM capability_entity_current_states WHERE "
            + _SCOPE_SQL
            + " AND entity_type=? AND entity_id=?",
            (*_scope_values(scope, capability_scope), entity_type, entity_id),
        ).fetchone()
        if current is None:
            raise CapabilityStoreError(f"cannot transition {entity_type} without an initial lifecycle state")
        current_status = str(current["status"])
        if int(current["state_version"]) != expected_state_version or str(current["state_digest"]) != expected_state_digest:
            raise CapabilityConflict("capability lifecycle compare-and-swap precondition failed")
        if effective_at < str(current["effective_at"]):
            raise CapabilityConflict("online capability lifecycle transition cannot backdate effective_at")
        allowed_targets = _LIFECYCLE_TRANSITIONS.get(entity_type, {}).get(current_status, frozenset())
        if target_status not in allowed_targets:
            raise CapabilityConflict(
                f"invalid capability lifecycle transition {entity_type}:{current_status}->{target_status}"
            )
        if entity_type == "relation" and target_status == "active":
            relation = self._sqlite.conn.execute(
                "SELECT source_capability_id, target_capability_id, relation_type FROM capability_relations WHERE "
                + _SCOPE_SQL
                + " AND relation_id=?",
                (*_scope_values(scope, capability_scope), entity_id),
            ).fetchone()
            if relation is None:
                raise CapabilityStoreError("capability relation descriptor disappeared before lifecycle transition")
            self._assert_relation_activation_is_acyclic(
                source_capability_id=str(relation["source_capability_id"]),
                target_capability_id=str(relation["target_capability_id"]),
                relation_type=str(relation["relation_type"]),
                relation_id=entity_id,
                scope=scope,
                capability_scope=capability_scope,
            )

        next_state_version = expected_state_version + 1
        state_payload = {
            **transition_payload,
            "state_version": next_state_version,
            "predecessor_state_version": expected_state_version,
            "predecessor_state_digest": expected_state_digest,
            "scope": _scope_payload(scope),
            "capability_scope": capability_scope,
        }
        state_digest = payload_digest(state_payload)
        operation_id = sha256(_json(request_payload).encode("utf-8")).hexdigest()
        ledger_event_id = f"capability-ledger-{operation_id[:32]}"
        audit_record_id = f"capability_audit_{operation_id[:24]}"
        audit_payload = {
            "schema": "capability.audit.v1",
            "operation_id": operation_id,
            "ledger_event_id": ledger_event_id,
            "action": "lifecycle_transition",
            "entity_type": "lifecycle_transition",
            "entity_id": transition_id,
            "entity_digest": state_digest,
            "scope": _scope_payload(scope),
            "capability_scope": capability_scope,
            "request_key": clean_request_key,
            "storage_context": {},
            "entity": state_payload,
        }
        audit_payload_digest = payload_digest(audit_payload)
        self._sqlite.conn.execute(
            """
            INSERT INTO capability_entity_lifecycle_events (
                tenant_id, agent_id, workspace_id, user_id, capability_scope,
                entity_type, entity_id, state_version, status, effective_at,
                reason, provenance_json, schema_version, state_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *_scope_values(scope, capability_scope),
                entity_type,
                entity_id,
                next_state_version,
                target_status,
                effective_at,
                reason,
                _json(provenance),
                CAPABILITY_V3_SCHEMA_VERSION,
                state_digest,
                effective_at,
            ),
        )
        updated = self._sqlite.conn.execute(
            """
            UPDATE capability_entity_current_states
            SET status=?, state_version=?, state_digest=?, effective_at=?, provenance_json=?, updated_at=?
            WHERE tenant_id=? AND agent_id=? AND workspace_id=? AND user_id=? AND capability_scope=?
              AND entity_type=? AND entity_id=? AND state_version=? AND state_digest=?
            """,
            (
                target_status,
                next_state_version,
                state_digest,
                effective_at,
                _json(provenance),
                effective_at,
                *_scope_values(scope, capability_scope),
                entity_type,
                entity_id,
                expected_state_version,
                expected_state_digest,
            ),
        )
        if updated.rowcount != 1:
            raise CapabilityConflict("capability lifecycle compare-and-swap update failed")
        self._append_ledger_event(
            scope=scope,
            capability_scope=capability_scope,
            ledger_event_id=ledger_event_id,
            idempotency_key=f"operation:{clean_request_key}",
            event_type="capability.lifecycle_transition",
            entity_type=entity_type,
            entity_id=entity_id,
            payload=audit_payload,
            payload_digest_value=audit_payload_digest,
            audit_record_id=audit_record_id,
            audit_export_operation_id=operation_id,
            provenance=provenance,
            evidence_refs=(),
            occurred_at=effective_at,
        )
        self._sqlite.conn.execute(
            """
            INSERT INTO capability_operation_journal (
                tenant_id, agent_id, workspace_id, user_id, capability_scope,
                operation_id, request_key, action, entity_type, entity_id,
                ledger_event_id, request_digest, result_digest, audit_record_id,
                audit_export_operation_id, audit_exported_at, provenance_json, schema_version,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *_scope_values(scope, capability_scope),
                operation_id,
                clean_request_key,
                "lifecycle_transition",
                "lifecycle_transition",
                transition_id,
                ledger_event_id,
                request_digest,
                state_digest,
                audit_record_id,
                operation_id,
                "",
                _json(provenance),
                CAPABILITY_V3_SCHEMA_VERSION,
                effective_at,
                effective_at,
            ),
        )
        self._pending_audits.append(
            PendingCapabilityAudit(
                operation_id=operation_id,
                ledger_event_id=ledger_event_id,
                audit_record_id=audit_record_id,
                action="lifecycle_transition",
                entity_type="lifecycle_transition",
                entity_id=transition_id,
                entity_digest=state_digest,
                scope=scope,
                capability_scope=capability_scope,
                payload=audit_payload,
                created_at=effective_at,
            )
        )
        return LifecycleTransitionReceipt(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_digest=state_digest,
            target_entity_digest=entity_digest,
            status=target_status,
            state_version=next_state_version,
            state_digest=state_digest,
            effective_at=effective_at,
            operation_id=operation_id,
            ledger_event_id=ledger_event_id,
            idempotent=False,
        )

    def _write(
        self,
        write: _EntityWrite,
        *,
        scope: ScopeRef,
        request_key: str,
        before_insert: Callable[[], None] | None = None,
    ) -> StoredCapabilityEntity:
        if self._read_only:
            raise CapabilityStoreError("capability mutation is not allowed in a read transaction")
        if not self._sqlite.conn.in_transaction:
            raise CapabilityStoreError("capability mutation escaped its RuntimeStore transaction")
        self._savepoint_counter += 1
        savepoint = f"capability_write_{self._savepoint_counter}"
        self._sqlite.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self._write_in_savepoint(
                write,
                scope=scope,
                request_key=request_key,
                before_insert=before_insert,
            )
        except Exception:
            self._sqlite.conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            self._sqlite.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        self._sqlite.conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return result

    def _write_in_savepoint(
        self,
        write: _EntityWrite,
        *,
        scope: ScopeRef,
        request_key: str,
        before_insert: Callable[[], None] | None,
    ) -> StoredCapabilityEntity:
        clean_request_key = str(request_key or "").strip() or (
            f"{write.entity_type}:{write.entity_id}:{write.entity_digest}"
        )
        storage_context = _normalized_storage_context(write.storage_context)
        request_payload = {
            "schema": "capability.operation.v1",
            "action": write.action,
            "entity_type": write.entity_type,
            "entity_id": write.entity_id,
            "entity_digest": write.entity_digest,
            "scope": _scope_payload(scope),
            "capability_scope": write.capability_scope,
            "request_key": clean_request_key,
            "storage_context": storage_context,
        }
        request_digest = payload_digest(request_payload)
        existing = self._sqlite.conn.execute(
            "SELECT operation_id, ledger_event_id, request_digest, result_digest FROM capability_operation_journal WHERE "
            + _SCOPE_SQL
            + " AND request_key=?",
            (*_scope_values(scope, write.capability_scope), clean_request_key),
        ).fetchone()
        if existing is not None:
            if str(existing["result_digest"]) != write.entity_digest or request_digest != str(existing["request_digest"]):
                raise CapabilityIdempotencyConflict(
                    "capability request key was reused with a different request or result"
                )
            return StoredCapabilityEntity(
                entity_type=write.entity_type,
                entity_id=write.entity_id,
                entity_digest=write.entity_digest,
                operation_id=str(existing["operation_id"]),
                ledger_event_id=str(existing["ledger_event_id"]),
                idempotent=True,
            )

        # A transport retry must retain its idempotency key.  Accepting a fresh
        # key for an existing immutable entity would either duplicate the ledger
        # or require a second durable alias stream to preserve fail-closed reuse
        # semantics after a rebuild.  Reject it instead; the original key is the
        # canonical operation identity and remains replayable.
        if self._existing_immutable_entity(
            write,
            scope=scope,
            storage_context=storage_context,
        ):
            raise CapabilityConflict(
                f"immutable {write.entity_type} {write.entity_id} already exists; "
                "retry with its original request key"
            )

        # The initial lifecycle state is projected immediately by the current
        # schema.  Until scheduled activation has its own durable projection,
        # accepting a future descriptor timestamp would make it usable before
        # its stated effective time.
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if not self._audit_replay_depth and write.created_at > now:
            raise CapabilityConflict("online capability registration cannot use a future created_at")

        if before_insert is not None:
            before_insert()

        operation_id = sha256(_json(request_payload).encode("utf-8")).hexdigest()
        ledger_event_id = f"capability-ledger-{operation_id[:32]}"
        audit_record_id = f"capability_audit_{operation_id[:24]}"

        audit_payload = {
            "schema": "capability.audit.v1",
            "operation_id": operation_id,
            "ledger_event_id": ledger_event_id,
            "action": write.action,
            "entity_type": write.entity_type,
            "entity_id": write.entity_id,
            "entity_digest": write.entity_digest,
            "scope": _scope_payload(scope),
            "capability_scope": write.capability_scope,
            "request_key": clean_request_key,
            "storage_context": storage_context,
            "entity": dict(write.payload),
        }
        audit_payload_digest = payload_digest(audit_payload)
        self._append_ledger_event(
            scope=scope,
            capability_scope=write.capability_scope,
            ledger_event_id=ledger_event_id,
            idempotency_key=f"operation:{clean_request_key}",
            event_type=f"capability.{write.action}",
            entity_type=write.entity_type,
            entity_id=write.entity_id,
            payload=audit_payload,
            payload_digest_value=audit_payload_digest,
            audit_record_id=audit_record_id,
            audit_export_operation_id=operation_id,
            provenance=write.provenance,
            evidence_refs=write.evidence_refs,
            occurred_at=write.created_at,
        )
        if write.entity_type == "observation":
            write = self._with_column_value(write, "ledger_event_id", ledger_event_id)
        existing_entity = self._insert_immutable_entity(
            write,
            scope=scope,
            storage_context=storage_context,
        )
        self._ensure_initial_lifecycle_state(write=write, scope=scope)
        if write.entity_type == "profile":
            self._insert_profile_lineage(write=write, scope=scope)
        if write.entity_type == "assessment":
            self._insert_assessment_snapshot_refs(write=write, scope=scope)
        self._sqlite.conn.execute(
            """
            INSERT INTO capability_operation_journal (
                tenant_id, agent_id, workspace_id, user_id, capability_scope,
                operation_id, request_key, action, entity_type, entity_id,
                ledger_event_id, request_digest, result_digest, audit_record_id,
                audit_export_operation_id, audit_exported_at, provenance_json, schema_version,
                created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *_scope_values(scope, write.capability_scope),
                operation_id,
                clean_request_key,
                write.action,
                write.entity_type,
                write.entity_id,
                ledger_event_id,
                request_digest,
                write.entity_digest,
                audit_record_id,
                operation_id,
                "",
                _json(write.provenance),
                CAPABILITY_V3_SCHEMA_VERSION,
                write.created_at,
                write.created_at,
            ),
        )
        self._pending_audits.append(
            PendingCapabilityAudit(
                operation_id=operation_id,
                ledger_event_id=ledger_event_id,
                audit_record_id=audit_record_id,
                action=write.action,
                entity_type=write.entity_type,
                entity_id=write.entity_id,
                entity_digest=write.entity_digest,
                scope=scope,
                capability_scope=write.capability_scope,
                payload=audit_payload,
                created_at=write.created_at,
            )
        )
        return StoredCapabilityEntity(
            entity_type=write.entity_type,
            entity_id=write.entity_id,
            entity_digest=write.entity_digest,
            operation_id=operation_id,
            ledger_event_id=ledger_event_id,
            idempotent=existing_entity,
        )

    def _existing_immutable_entity(
        self,
        write: _EntityWrite,
        *,
        scope: ScopeRef,
        storage_context: Mapping[str, str],
    ) -> bool:
        entity_id_column = write.columns[len(_SCOPE_COLUMNS)]
        unknown_context_columns = set(storage_context) - set(write.columns)
        if unknown_context_columns:
            raise CapabilityStoreError(
                f"{write.entity_type} storage context has no persisted columns: "
                f"{sorted(unknown_context_columns)}"
            )
        selected_columns = [write.digest_column, *storage_context]
        row = self._sqlite.conn.execute(
            f"SELECT {', '.join(selected_columns)} FROM {write.table} WHERE "
            + _SCOPE_SQL
            + f" AND {entity_id_column}=?",
            (*_scope_values(scope, write.capability_scope), write.entity_id),
        ).fetchone()
        if row is not None:
            if str(row[write.digest_column]) != write.entity_digest:
                raise CapabilityConflict(
                    f"{write.entity_type} {write.entity_id} conflicts with an immutable stored payload"
                )
            for column, expected in storage_context.items():
                stored = "" if row[column] is None else str(row[column])
                if stored != expected:
                    raise CapabilityConflict(
                        f"{write.entity_type} {write.entity_id} conflicts with its immutable storage context"
                    )
            return True
        return False

    def _insert_immutable_entity(
        self,
        write: _EntityWrite,
        *,
        scope: ScopeRef,
        storage_context: Mapping[str, str],
    ) -> bool:
        if self._existing_immutable_entity(
            write,
            scope=scope,
            storage_context=storage_context,
        ):
            return True
        placeholders = ", ".join("?" for _ in write.columns)
        try:
            self._sqlite.conn.execute(
                f"INSERT INTO {write.table} ({', '.join(write.columns)}) VALUES ({placeholders})",
                (*_scope_values(scope, write.capability_scope), *write.values),
            )
        except Exception as exc:
            message = str(exc).lower()
            if "unique constraint failed" in message:
                raise CapabilityConflict(
                    f"{write.entity_type} duplicates an existing semantic capability entity"
                ) from exc
            raise
        return False

    def _ensure_initial_lifecycle_state(self, *, write: _EntityWrite, scope: ScopeRef) -> None:
        """Seed the immutable lifecycle history without rewriting descriptor rows.

        Definition/revision/binding/profile/spec status is an effective state,
        not a reason to overwrite a historical descriptor whose digest embeds
        its initial status.  WP4 can append later transitions using these
        normalized history/current tables without a storage redesign.
        """

        status = str(write.payload.get("status") or "").strip()
        if not status:
            return
        current = self._sqlite.conn.execute(
            "SELECT state_version FROM capability_entity_current_states WHERE "
            + _SCOPE_SQL
            + " AND entity_type=? AND entity_id=?",
            (*_scope_values(scope, write.capability_scope), write.entity_type, write.entity_id),
        ).fetchone()
        if current is not None:
            return
        state_payload = {
            "schema": "capability.lifecycle.v1",
            "entity_type": write.entity_type,
            "entity_id": write.entity_id,
            "entity_digest": write.entity_digest,
            "status": status,
            "effective_at": write.created_at,
            "scope": _scope_payload(scope),
            "capability_scope": write.capability_scope,
        }
        state_digest = payload_digest(state_payload)
        self._sqlite.conn.execute(
            """
            INSERT INTO capability_entity_lifecycle_events (
                tenant_id, agent_id, workspace_id, user_id, capability_scope,
                entity_type, entity_id, state_version, status, effective_at,
                reason, provenance_json, schema_version, state_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, '', ?, ?, ?, ?)
            """,
            (
                *_scope_values(scope, write.capability_scope),
                write.entity_type,
                write.entity_id,
                status,
                write.created_at,
                _json(write.provenance),
                CAPABILITY_V3_SCHEMA_VERSION,
                state_digest,
                write.created_at,
            ),
        )
        self._sqlite.conn.execute(
            """
            INSERT INTO capability_entity_current_states (
                tenant_id, agent_id, workspace_id, user_id, capability_scope,
                entity_type, entity_id, status, state_version, state_digest,
                effective_at, provenance_json, schema_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
            """,
            (
                *_scope_values(scope, write.capability_scope),
                write.entity_type,
                write.entity_id,
                status,
                state_digest,
                write.created_at,
                _json(write.provenance),
                CAPABILITY_V3_SCHEMA_VERSION,
                write.created_at,
                write.created_at,
            ),
        )

    def _insert_profile_lineage(self, *, write: _EntityWrite, scope: ScopeRef) -> None:
        """Index an immutable Profile revision under its stable logical key.

        The profile descriptor itself remains in ``capability_profiles`` keyed
        by its immutable revision id.  This additive index makes resolution by
        logical profile key typed and bounded without rewriting old profile
        payloads or turning a profile update into an in-place mutation.
        """

        payload = write.payload
        profile_key = str(payload.get("profile_key") or write.entity_id)
        profile_revision = str(payload.get("revision") or "")
        if not profile_revision:
            raise CapabilityStoreError("capability profile revision is required for lineage")
        existing = self._sqlite.conn.execute(
            "SELECT profile_key, profile_revision, profile_digest FROM capability_profile_lineage WHERE "
            + _SCOPE_SQL
            + " AND profile_id=?",
            (*_scope_values(scope, write.capability_scope), write.entity_id),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["profile_key"]) != profile_key
                or str(existing["profile_revision"]) != profile_revision
                or str(existing["profile_digest"]) != write.entity_digest
            ):
                raise CapabilityConflict("capability Profile revision conflicts with its lineage")
            return
        try:
            self._sqlite.conn.execute(
                """
                INSERT INTO capability_profile_lineage (
                    tenant_id, agent_id, workspace_id, user_id, capability_scope,
                    profile_key, profile_id, profile_revision, profile_digest,
                    status, effective_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *_scope_values(scope, write.capability_scope),
                    profile_key,
                    write.entity_id,
                    profile_revision,
                    write.entity_digest,
                    str(payload.get("status") or ""),
                    write.created_at,
                    write.created_at,
                ),
            )
        except Exception as exc:
            if "unique constraint failed" in str(exc).lower():
                raise CapabilityConflict(
                    f"capability Profile lineage already has revision {profile_revision!r} for {profile_key!r}"
                ) from exc
            raise

    @staticmethod
    def _with_column_value(write: _EntityWrite, column: str, value: Any) -> _EntityWrite:
        try:
            index = write.columns.index(column) - len(_SCOPE_COLUMNS)
        except ValueError as exc:
            raise CapabilityStoreError(f"capability entity has no column {column}") from exc
        if index < 0:
            raise CapabilityStoreError(f"cannot overwrite scoped column {column}")
        values = list(write.values)
        values[index] = value
        return _EntityWrite(
            entity_type=write.entity_type,
            action=write.action,
            table=write.table,
            entity_id=write.entity_id,
            digest_column=write.digest_column,
            entity_digest=write.entity_digest,
            capability_scope=write.capability_scope,
            created_at=write.created_at,
            columns=write.columns,
            values=tuple(values),
            payload=write.payload,
            evidence_refs=write.evidence_refs,
            provenance=write.provenance,
            storage_context=write.storage_context,
        )

    def _insert_assessment_snapshot_refs(self, *, write: _EntityWrite, scope: ScopeRef) -> None:
        snapshot_ids = _sequence(write.payload.get("capability_snapshot_ids"))
        profile_id = str(write.payload.get("profile_id") or "")
        if not profile_id:
            raise CapabilityStoreError("l5 assessment snapshot references require a profile_id")
        snapshots: dict[str, Any] = {}
        for snapshot_id in snapshot_ids:
            snapshot = self._sqlite.conn.execute(
                "SELECT capability_id, capability_revision_id, profile_id, provider_binding_id, maturity "
                "FROM capability_state_snapshots WHERE "
                + _SCOPE_SQL
                + " AND snapshot_id=?",
                (*_scope_values(scope, write.capability_scope), snapshot_id),
            ).fetchone()
            if snapshot is None:
                raise CapabilityStoreError(
                    f"l5 assessment references unknown snapshot {snapshot_id}"
                )
            if str(snapshot["profile_id"]) != profile_id:
                raise CapabilityStoreError(
                    f"l5 assessment snapshot {snapshot_id} belongs to another profile"
                )
            snapshots[snapshot_id] = snapshot
            ref_digest = sha256(
                f"{write.entity_digest}\0{profile_id}\0{snapshot_id}".encode("utf-8")
            ).hexdigest()
            self._sqlite.conn.execute(
                """
                INSERT OR IGNORE INTO l5_assessment_snapshot_refs (
                    tenant_id, agent_id, workspace_id, user_id, capability_scope,
                    assessment_id, snapshot_id, profile_id, created_at, schema_version,
                    provenance_json, ref_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *_scope_values(scope, write.capability_scope),
                    write.entity_id,
                    snapshot_id,
                    profile_id,
                    write.created_at,
                    CAPABILITY_V3_SCHEMA_VERSION,
                    _json(write.provenance),
                    ref_digest,
                ),
            )
        readiness_by_revision = _mapping(write.payload.get("capability_readiness"))
        for raw_revision_id, raw_binding_states in readiness_by_revision.items():
            revision_id = str(raw_revision_id)
            binding_states = _mapping(raw_binding_states)
            for raw_binding_id, raw_readiness in binding_states.items():
                readiness_binding_id = str(raw_binding_id)
                readiness = _mapping(raw_readiness)
                snapshot_id = str(readiness.get("snapshot_id") or "")
                snapshot = snapshots.get(snapshot_id)
                if snapshot is None:
                    raise CapabilityStoreError(
                        "l5 readiness state references a snapshot outside its assessment"
                    )
                if str(snapshot["capability_revision_id"]) != revision_id:
                    raise CapabilityStoreError(
                        f"l5 readiness revision {revision_id} does not match snapshot {snapshot_id}"
                    )
                snapshot_binding_id = str(snapshot["provider_binding_id"] or "")
                if readiness_binding_id == "_revision" and snapshot_binding_id:
                    raise CapabilityStoreError(
                        f"l5 revision-wide readiness cannot cite provider-bound snapshot {snapshot_id}"
                    )
                if readiness_binding_id != "_revision" and snapshot_binding_id != readiness_binding_id:
                    raise CapabilityStoreError(
                        f"l5 readiness binding {readiness_binding_id} does not match snapshot {snapshot_id}"
                    )
                if str(snapshot["maturity"]) != str(readiness.get("maturity") or ""):
                    raise CapabilityStoreError(
                        f"l5 readiness maturity does not match snapshot {snapshot_id}"
                    )
                ref_payload = {
                    "assessment_digest": write.entity_digest,
                    "profile_id": profile_id,
                    "capability_id": str(snapshot["capability_id"]),
                    "capability_revision_id": revision_id,
                    "readiness_binding_id": readiness_binding_id,
                    "snapshot_id": snapshot_id,
                    "readiness": dict(readiness),
                }
                self._sqlite.conn.execute(
                    """
                    INSERT OR IGNORE INTO l5_assessment_readiness_refs (
                        tenant_id, agent_id, workspace_id, user_id, capability_scope,
                        assessment_id, profile_id, capability_id, capability_revision_id,
                        readiness_binding_id, snapshot_id, maturity, readiness_json,
                        created_at, schema_version, provenance_json, ref_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        *_scope_values(scope, write.capability_scope),
                        write.entity_id,
                        profile_id,
                        str(snapshot["capability_id"]),
                        revision_id,
                        readiness_binding_id,
                        snapshot_id,
                        str(readiness.get("maturity") or ""),
                        _json(readiness),
                        write.created_at,
                        CAPABILITY_V3_SCHEMA_VERSION,
                        _json(write.provenance),
                        payload_digest(ref_payload),
                    ),
                )

    def _append_ledger_event(
        self,
        *,
        scope: ScopeRef,
        capability_scope: str,
        ledger_event_id: str,
        idempotency_key: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: Mapping[str, Any],
        payload_digest_value: str,
        audit_record_id: str,
        audit_export_operation_id: str,
        provenance: Mapping[str, Any],
        evidence_refs: tuple[str, ...],
        occurred_at: str,
    ) -> None:
        existing = self._sqlite.conn.execute(
            "SELECT payload_digest FROM capability_ledger_events WHERE " + _SCOPE_SQL + " AND idempotency_key=?",
            (*_scope_values(scope, capability_scope), idempotency_key),
        ).fetchone()
        if existing is not None:
            if str(existing["payload_digest"]) != payload_digest_value:
                raise CapabilityIdempotencyConflict("capability ledger event idempotency conflict")
            return
        self._sqlite.conn.execute(
            """
            INSERT INTO capability_ledger_events (
                tenant_id, agent_id, workspace_id, user_id, capability_scope,
                ledger_event_id, idempotency_key, event_type, entity_type, entity_id,
                payload_json, payload_digest, audit_record_id, audit_export_operation_id,
                provenance_json, evidence_refs_json,
                schema_version, occurred_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                *_scope_values(scope, capability_scope),
                ledger_event_id,
                idempotency_key,
                event_type,
                entity_type,
                entity_id,
                _json(payload),
                payload_digest_value,
                audit_record_id,
                audit_export_operation_id,
                _json(provenance),
                _json(list(evidence_refs)),
                CAPABILITY_V3_SCHEMA_VERSION,
                occurred_at,
                occurred_at,
            ),
        )

    def _definition_write(self, value: CapabilityDefinition) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "capability_id", "display_name", "description", "owner", "status", "risk_tier",
            "tags_json", "supersedes_json", "evidence_refs_json", "provenance_json", "schema_version",
            "definition_digest", "payload_json", "created_at",
        )
        return _EntityWrite(
            "definition", "definition_registered", "capability_definitions", value.capability_id,
            "definition_digest", value.definition_digest, value.scope, value.created_at, columns,
            (
                value.capability_id, value.display_name, value.description, value.owner, value.status, value.risk_tier,
                _json(payload["tags"]), _json(payload["supersedes"]), _json(payload["evidence_refs"]),
                _json(payload["provenance"]), value.schema_version, value.definition_digest, _json(payload), value.created_at,
            ), payload, _sequence(payload.get("evidence_refs")), _mapping(payload.get("provenance")),
        )

    def _revision_write(self, value: CapabilityRevision) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "revision_id", "capability_id", "contract_json", "compatibility", "supersedes_revision_id",
            "compatibility_policy_id", "compatibility_policy_digest", "status", "evidence_refs_json",
            "provenance_json", "schema_version", "contract_digest", "payload_json", "created_at",
        )
        values_tail = (
            value.revision_id, value.capability_id, _json(payload["contract"]), value.compatibility,
            value.supersedes_revision_id, value.compatibility_policy_id, value.compatibility_policy_digest,
            value.status, _json(payload["evidence_refs"]), _json(payload["provenance"]), value.schema_version,
            value.contract_digest, _json(payload), value.created_at,
        )
        return _EntityWrite(
            "revision", "revision_registered", "capability_revisions", value.revision_id, "contract_digest",
            value.contract_digest, value.scope, value.created_at, columns,
            values_tail, payload, _sequence(payload.get("evidence_refs")), _mapping(payload.get("provenance")),
        )

    def _relation_write(self, value: CapabilityRelation) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "relation_id", "source_capability_id", "target_capability_id", "relation_type", "relation_policy_json",
            "status", "evidence_refs_json", "provenance_json", "schema_version", "relation_digest", "payload_json", "created_at",
        )
        values = (
            value.relation_id, value.source_capability_id, value.target_capability_id, value.relation_type,
            _json(payload["relation_policy"]), value.status, _json(payload["evidence_refs"]),
            _json(payload["provenance"]), CAPABILITY_V3_SCHEMA_VERSION, value.relation_digest, _json(payload), value.created_at,
        )
        return self._entity_write(
            "relation", "relation_registered", "capability_relations", value.relation_id, "relation_digest",
            value.relation_digest, value.scope, value.created_at, columns, values, payload,
        )

    def _binding_write(self, value: CapabilityBinding) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "binding_id", "capability_id", "capability_revision_id", "provider_kind", "provider_instance_id",
            "implementation_digest", "operations_json", "limits_json", "environment_fingerprint_json", "applicability_json",
            "advertisement_evidence_refs_json", "status", "advertised_at", "provenance_json", "schema_version",
            "binding_digest", "payload_json", "created_at",
        )
        values = (
            value.binding_id, value.capability_id, value.capability_revision_id, value.provider_kind,
            value.provider_instance_id, value.implementation_digest, _json(payload["operations"]), _json(payload["limits"]),
            _json(payload["environment_fingerprint"]), _json(payload["applicability"]),
            _json(payload["advertisement_evidence_refs"]), value.status, value.advertised_at,
            _json(payload["provenance"]), CAPABILITY_V3_SCHEMA_VERSION, value.binding_digest, _json(payload), value.created_at,
        )
        return self._entity_write(
            "binding", "binding_registered", "capability_bindings", value.binding_id, "binding_digest",
            value.binding_digest, value.scope, value.created_at, columns, values, payload,
            evidence_key="advertisement_evidence_refs",
        )

    def _advertisement_write(
        self,
        value: AdapterCapabilityAdvertisement,
    ) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "advertisement_id", "binding_id", "adapter_id", "provider_kind", "provider_instance_id",
            "status", "advertised_at", "expires_at", "operations_json", "limits_json",
            "environment_fingerprint_json", "applicability_json", "evidence_refs_json", "provenance_json",
            "schema_version", "advertisement_digest", "payload_json", "created_at",
        )
        values = (
            value.advertisement_id,
            value.binding_id,
            value.adapter_id,
            value.provider_kind,
            value.provider_instance_id,
            value.status,
            value.advertised_at,
            value.expires_at,
            _json(payload["operations"]),
            _json(payload["limits"]),
            _json(payload["environment_fingerprint"]),
            _json(payload["applicability"]),
            _json(payload["evidence_refs"]),
            _json(payload["provenance"]),
            value.schema_version,
            value.advertisement_digest,
            _json(payload),
            value.created_at,
        )
        return self._entity_write(
            "advertisement",
            "advertisement_registered",
            "adapter_capability_advertisements",
            value.advertisement_id,
            "advertisement_digest",
            value.advertisement_digest,
            value.scope,
            value.created_at,
            columns,
            values,
            payload,
            evidence_key="evidence_refs",
        )

    def _profile_write(self, value: CapabilityProfile) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "profile_id", "requirements_json", "status", "profile_revision", "provenance_json", "schema_version",
            "profile_digest", "payload_json", "created_at",
        )
        values = (
            value.profile_id, _json(payload["requirements"]), value.status, value.revision,
            _json(payload["provenance"]), CAPABILITY_V3_SCHEMA_VERSION, value.profile_digest, _json(payload), value.created_at,
        )
        return self._entity_write(
            "profile", "profile_registered", "capability_profiles", value.profile_id, "profile_digest",
            value.profile_digest, value.scope, value.created_at, columns, values, payload,
        )

    def _legacy_profile_write(
        self,
        payload: Mapping[str, Any],
        *,
        legacy_digest: str,
    ) -> _EntityWrite:
        """Build a byte-stable WP3 Profile write only while replaying audit.

        New registration must always use :meth:`_profile_write`, which emits a
        ``profile_key`` and the constrained Profile DSL.  This compatibility
        writer retains the old descriptor payload/digest so JSONL rebuild does
        not rewrite historical facts under a new semantic identity.
        """

        profile_id = str(payload["profile_id"])
        requirements = payload["requirements"]
        status = str(payload["status"])
        revision = str(payload["revision"])
        provenance = _mapping(payload["provenance"])
        created_at = str(payload["created_at"])
        scope = str(payload["scope"])
        stored_payload = dict(payload)
        columns = (
            *_SCOPE_COLUMNS,
            "profile_id", "requirements_json", "status", "profile_revision", "provenance_json", "schema_version",
            "profile_digest", "payload_json", "created_at",
        )
        values = (
            profile_id,
            _json(requirements),
            status,
            revision,
            _json(provenance),
            CAPABILITY_V3_SCHEMA_VERSION,
            legacy_digest,
            _json(stored_payload),
            created_at,
        )
        return self._entity_write(
            "profile",
            "profile_registered",
            "capability_profiles",
            profile_id,
            "profile_digest",
            legacy_digest,
            scope,
            created_at,
            columns,
            values,
            stored_payload,
        )

    def _evaluation_spec_write(self, value: EvaluationSpec, *, profile_id: str | None) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "eval_spec_id", "capability_id", "capability_revision_id", "profile_id", "grader_type", "executor_id",
            "executor_contract_digest", "fixture_refs_json", "checks_json", "required_metrics_json", "retry_policy_json",
            "stability_policy_json", "applicability_json", "resource_budget_json", "binding_selector_json",
            "model_grader_policy_json", "status", "spec_revision", "provenance_json", "schema_version", "spec_digest",
            "payload_json", "created_at",
        )
        values = (
            value.eval_spec_id, value.capability_id, value.capability_revision_id, profile_id, value.grader_type,
            value.executor_id, value.executor_contract_digest, _json(payload["fixture_refs"]), _json(payload["checks"]),
            _json(payload["required_metrics"]), _json(payload["retry_policy"]), _json(payload["stability_policy"]),
            _json(payload["applicability"]), _json(payload["resource_budget"]), _json(payload["binding_selector"]),
            _json(payload["model_grader_policy"]), value.status, value.revision, _json(payload["provenance"]),
            CAPABILITY_V3_SCHEMA_VERSION, value.spec_digest, _json(payload), value.created_at,
        )
        return self._entity_write(
            "evaluation_spec", "evaluation_spec_registered", "evaluation_specs", value.eval_spec_id, "spec_digest",
            value.spec_digest, value.scope, value.created_at, columns, values, payload, evidence_key="fixture_refs",
            storage_context={"profile_id": profile_id or ""},
        )

    def _evaluation_run_write(self, value: EvaluationRun, *, profile_id: str | None) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "run_id", "eval_spec_id", "capability_id", "capability_revision_id", "provider_binding_id", "profile_id",
            "idempotency_key", "run_state", "verdict", "source", "executor_id", "executor_contract_digest", "grader_id",
            "grader_revision", "input_digest", "output_digest", "evidence_digest", "evidence_refs_json",
            "environment_fingerprint_json", "provenance_json", "metrics_json", "error_taxonomy_json", "deployment_authority_json",
            "schema_version", "run_digest", "payload_json", "requested_at", "started_at", "finished_at", "terminal_at",
            "row_version", "current_digest", "created_at", "updated_at",
        )
        values = (
            value.run_id, value.eval_spec_id, value.capability_id, value.capability_revision_id,
            value.provider_binding_id, profile_id, value.idempotency_key, "terminal", value.verdict, value.source,
            value.executor_id, value.executor_contract_digest, value.grader_id, value.grader_revision, value.input_digest,
            value.output_digest, value.evidence_digest, _json(payload["evidence_refs"]),
            _json(payload["environment_fingerprint"]), _json(payload["provenance"]), _json(payload["metrics"]),
            _json(payload["error_taxonomy"]), _json(payload["deployment_authority"]), CAPABILITY_V3_SCHEMA_VERSION,
            value.run_digest, _json(payload), value.started_at, value.started_at, value.finished_at, value.finished_at,
            1, value.run_digest, value.started_at, value.finished_at,
        )
        return self._entity_write(
            "evaluation_run", "evaluation_run_recorded", "evaluation_runs", value.run_id, "run_digest",
            value.run_digest, value.scope, value.finished_at, columns, values, payload,
            storage_context={"profile_id": profile_id or ""},
        )

    def _observation_write(self, value: CapabilityObservation) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "observation_id", "capability_id", "capability_revision_id", "provider_binding_id", "idempotency_key",
            "ledger_event_id", "verdict", "source", "executor_id", "executor_contract_digest", "grader_id", "grader_revision",
            "input_digest", "output_digest", "evidence_digest", "evidence_refs_json", "environment_fingerprint_json",
            "provenance_json", "metrics_json", "error_taxonomy_json", "deployment_authority_json", "schema_version",
            "observation_digest", "payload_json", "observed_at", "created_at",
        )
        values = (
            value.observation_id, value.capability_id, value.capability_revision_id, value.provider_binding_id,
            value.idempotency_key, "", value.verdict, value.source, value.executor_id, value.executor_contract_digest,
            value.grader_id, value.grader_revision, value.input_digest, value.output_digest, value.evidence_digest,
            _json(payload["evidence_refs"]), _json(payload["environment_fingerprint"]), _json(payload["provenance"]),
            _json(payload["metrics"]), _json(payload["error_taxonomy"]), _json(payload["deployment_authority"]),
            CAPABILITY_V3_SCHEMA_VERSION, value.observation_digest, _json(payload), value.observed_at, value.observed_at,
        )
        return self._entity_write(
            "observation", "observation_appended", "capability_observations", value.observation_id,
            "observation_digest", value.observation_digest, value.scope, value.observed_at, columns, values, payload,
        )

    def _knowledge_link_write(
        self,
        value: CapabilityKnowledgeLink,
        *,
        knowledge_storage_key: str,
        knowledge_record_digest: str,
    ) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "link_id", "capability_id", "capability_revision_id", "knowledge_record_id", "knowledge_storage_key",
            "knowledge_record_digest", "relation_type", "source_status", "applicability", "source_trust", "review_state",
            "temporal_validity_json", "environment_constraints_json", "contradiction_state", "applicability_score",
            "applicability_evidence_refs_json", "evidence_refs_json", "provenance_json", "schema_version", "link_digest",
            "payload_json", "created_at",
        )
        values = (
            value.link_id, value.capability_id, value.capability_revision_id, value.knowledge_record_id,
            knowledge_storage_key, knowledge_record_digest, value.relation_type, value.source_status, value.applicability,
            value.source_trust, value.review_state, _json(payload["temporal_validity"]),
            _json(payload["environment_constraints"]), value.contradiction_state, value.applicability_score,
            _json(payload["applicability_evidence_refs"]), _json(payload["evidence_refs"]), _json(payload["provenance"]),
            CAPABILITY_V3_SCHEMA_VERSION, value.link_digest, _json(payload), value.created_at,
        )
        return self._entity_write(
            "knowledge_link", "knowledge_link_registered", "capability_knowledge_links", value.link_id, "link_digest",
            value.link_digest, value.scope, value.created_at, columns, values, payload,
            storage_context={
                "knowledge_storage_key": knowledge_storage_key,
                "knowledge_record_digest": knowledge_record_digest,
            },
        )

    def _snapshot_write(
        self,
        value: CapabilityStateSnapshot,
        *,
        provider_binding_id: str | None,
    ) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "snapshot_id", "capability_id", "capability_revision_id", "profile_id", "provider_binding_id", "maturity",
            "confidence", "evidence_refs_json", "sample_sufficiency_json", "reliability_metrics_json", "latest_success_ref",
            "latest_failure_ref", "regression_streak", "dependency_state_json", "knowledge_applicability_json",
            "provider_applicability_json", "environment_applicability_json", "input_watermark", "algorithm_revision",
            "reason_codes_json", "input_digests_json", "provenance_json", "schema_version", "snapshot_digest", "payload_json",
            "computed_at", "created_at",
        )
        provenance = {"projection": "capability_state.v3"}
        values = (
            value.snapshot_id, value.capability_id, value.capability_revision_id, value.profile_id, provider_binding_id,
            value.maturity, value.confidence, _json(payload["evidence_refs"]), _json(payload["sample_sufficiency"]),
            _json(payload["reliability_metrics"]), value.latest_success_ref, value.latest_failure_ref, value.regression_streak,
            _json(payload["dependency_state"]), _json(payload["knowledge_applicability"]),
            _json(payload["provider_applicability"]), _json(payload["environment_applicability"]), value.input_watermark,
            value.algorithm_revision, _json(payload["reason_codes"]), _json(payload["input_digests"]), _json(provenance),
            CAPABILITY_V3_SCHEMA_VERSION, value.snapshot_digest, _json(payload), value.computed_at, value.computed_at,
        )
        return self._entity_write(
            "snapshot", "snapshot_registered", "capability_state_snapshots", value.snapshot_id, "snapshot_digest",
            value.snapshot_digest, value.scope, value.computed_at, columns, values, payload, provenance=provenance,
            storage_context={"provider_binding_id": provider_binding_id or ""},
        )

    def _assessment_write(self, value: L5AssessmentV3) -> _EntityWrite:
        payload = value.to_dict()
        columns = (
            *_SCOPE_COLUMNS,
            "assessment_id", "profile_id", "loop_maturity", "capability_readiness_json", "adapter_readiness_json",
            "deployment_assurance_json", "evidence_refs_json", "algorithm_revision", "input_watermarks_json",
            "provenance_json", "schema_version", "assessment_digest", "payload_json", "created_at",
        )
        provenance = {"projection": "l5_assessment.v3"}
        values = (
            value.assessment_id, value.profile_id, value.loop_maturity, _json(payload["capability_readiness"]),
            _json(payload["adapter_readiness"]), _json(payload["deployment_assurance"]), _json(payload["evidence_refs"]),
            value.algorithm_revision, _json(payload["input_watermarks"]), _json(provenance), CAPABILITY_V3_SCHEMA_VERSION,
            value.assessment_digest, _json(payload), value.created_at,
        )
        return self._entity_write(
            "assessment", "assessment_registered", "l5_assessments_v3", value.assessment_id, "assessment_digest",
            value.assessment_digest, value.scope, value.created_at, columns, values, payload, provenance=provenance,
        )

    def _entity_write(
        self,
        entity_type: str,
        action: str,
        table: str,
        entity_id: str,
        digest_column: str,
        entity_digest: str,
        capability_scope: str,
        created_at: str,
        columns: tuple[str, ...],
        values: tuple[Any, ...],
        payload: Mapping[str, Any],
        *,
        evidence_key: str = "evidence_refs",
        provenance: Mapping[str, Any] | None = None,
        storage_context: Mapping[str, Any] | None = None,
    ) -> _EntityWrite:
        resolved_provenance = provenance if provenance is not None else _mapping(payload.get("provenance"))
        evidence_refs = _sequence(payload.get(evidence_key))
        return _EntityWrite(
            entity_type=entity_type,
            action=action,
            table=table,
            entity_id=entity_id,
            digest_column=digest_column,
            entity_digest=entity_digest,
            capability_scope=capability_scope,
            created_at=created_at,
            columns=columns,
            values=tuple(values),
            payload=payload,
            evidence_refs=evidence_refs,
            provenance=resolved_provenance,
            storage_context=dict(storage_context or {}),
        )


def _open_capability_store(sqlite: SqliteRecordStore, *, read_only: bool = False) -> CapabilityStore:
    """Internal factory reserved for the RuntimeStore transaction boundary."""

    return CapabilityStore(
        sqlite,
        _transaction_token=_CAPABILITY_STORE_TRANSACTION_TOKEN,
        _read_only=read_only,
    )
