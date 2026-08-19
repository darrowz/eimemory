"""Transaction-local repository for Storage v2 capability data.

This module deliberately does not own a SQLite connection lifecycle.  It is
created by :class:`RuntimeStore` inside a ``BEGIN IMMEDIATE`` transaction, does
not commit or flush exports itself, and records the exact audit work that must
be exported after commit.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from hashlib import sha256
from typing import Any, Mapping

from eimemory.capabilities.models import (
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
    "profile": CapabilityProfile,
    "evaluation_spec": EvaluationSpec,
    "evaluation_run": EvaluationRun,
    "observation": CapabilityObservation,
    "knowledge_link": CapabilityKnowledgeLink,
    "snapshot": CapabilityStateSnapshot,
    "assessment": L5AssessmentV3,
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


class CapabilityStore:
    """Typed SQL mapper used only inside a RuntimeStore domain transaction.

    Definition-like records are immutable.  A repeat with the same exact
    payload is idempotent; the same identity or exact request key with a
    different payload fails closed.  Observations are first appended to the
    immutable capability ledger and then indexed in ``capability_observations``.
    """

    def __init__(self, sqlite: SqliteRecordStore, *, _transaction_token: object | None = None) -> None:
        if _transaction_token is not _CAPABILITY_STORE_TRANSACTION_TOKEN:
            raise CapabilityStoreError(
                "CapabilityStore is transaction-local; use RuntimeStore.mutate_capabilities_atomically"
            )
        if not sqlite.conn.in_transaction:
            raise CapabilityStoreError("CapabilityStore requires an active RuntimeStore transaction")
        self._sqlite = sqlite
        self._pending_audits: list[PendingCapabilityAudit] = []
        self._savepoint_counter = 0

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
        return self._write(self._relation_write(relation), scope=scope, request_key=request_key)

    def register_binding(
        self,
        binding: CapabilityBinding,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(self._binding_write(binding), scope=scope, request_key=request_key)

    def register_profile(
        self,
        profile: CapabilityProfile,
        *,
        scope: ScopeRef,
        request_key: str = "",
    ) -> StoredCapabilityEntity:
        return self._write(self._profile_write(profile), scope=scope, request_key=request_key)

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

    def replay_audit(self, audit: Mapping[str, Any]) -> StoredCapabilityEntity:
        """Rebuild a v3 entity from its durable record-stream audit payload.

        Rebuilds use the same immutable write rules and recompute the operation
        identity.  A mismatching operation or digest is a corruption signal,
        never an opportunity to silently accept a different historical fact.
        """

        if str(audit.get("schema") or "") != "capability.audit.v1":
            raise CapabilityStoreError("unsupported capability audit schema")
        entity_type = str(audit.get("entity_type") or "")
        model_type = _MODEL_BY_ENTITY_TYPE.get(entity_type)
        raw_entity = audit.get("entity")
        if model_type is None or not isinstance(raw_entity, Mapping):
            raise CapabilityStoreError("capability audit lacks a supported typed entity")
        scope = _strict_scope_from_audit(audit.get("scope"))
        entity = model_type(
            **{
                item.name: raw_entity[item.name]
                for item in fields(model_type)
                if item.init and item.name in raw_entity
            }
        )
        capability_scope = str(audit.get("capability_scope") or "")
        if not capability_scope or str(getattr(entity, "scope", "")) != capability_scope:
            raise CapabilityStoreError("capability audit scope does not match its entity contract")
        request_key = str(audit.get("request_key") or "")
        context = _mapping(audit.get("storage_context"))
        if entity_type == "definition":
            result = self.register_definition(entity, scope=scope, request_key=request_key)
        elif entity_type == "revision":
            result = self.register_revision(entity, scope=scope, request_key=request_key)
        elif entity_type == "relation":
            result = self.register_relation(entity, scope=scope, request_key=request_key)
        elif entity_type == "binding":
            result = self.register_binding(entity, scope=scope, request_key=request_key)
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

    def _write(
        self,
        write: _EntityWrite,
        *,
        scope: ScopeRef,
        request_key: str,
    ) -> StoredCapabilityEntity:
        if not self._sqlite.conn.in_transaction:
            raise CapabilityStoreError("capability mutation escaped its RuntimeStore transaction")
        self._savepoint_counter += 1
        savepoint = f"capability_write_{self._savepoint_counter}"
        self._sqlite.conn.execute(f"SAVEPOINT {savepoint}")
        try:
            result = self._write_in_savepoint(write, scope=scope, request_key=request_key)
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


def _open_capability_store(sqlite: SqliteRecordStore) -> CapabilityStore:
    """Internal factory reserved for the RuntimeStore transaction boundary."""

    return CapabilityStore(sqlite, _transaction_token=_CAPABILITY_STORE_TRANSACTION_TOKEN)
