from __future__ import annotations

from hashlib import sha256

from eimemory.capabilities.models import CapabilityDefinition
from eimemory.models.records import ScopeRef
from eimemory.storage.capability_store import PendingCapabilityAudit
from eimemory.storage.jsonl import canonical_payload_json, payload_digest
from eimemory.storage.runtime_store import RuntimeStore, _capability_audit_record


SCOPE = ScopeRef(
    tenant_id="tenant-temporal-replay",
    agent_id="agent-temporal-replay",
    workspace_id="workspace-temporal-replay",
    user_id="user-temporal-replay",
)
CAPABILITY_SCOPE = "global"
FUTURE_CREATED = "2099-01-01T00:00:00.000000Z"
FUTURE_RETIRED = "2099-01-02T00:00:00.000000Z"


def _scope_payload() -> dict[str, str]:
    return {
        "tenant_id": SCOPE.tenant_id,
        "agent_id": SCOPE.agent_id,
        "workspace_id": SCOPE.workspace_id,
        "user_id": SCOPE.user_id,
    }


def _record_for_audit(
    audit: dict,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    entity_digest: str,
    created_at: str,
):
    operation_id = str(audit["operation_id"])
    return _capability_audit_record(
        PendingCapabilityAudit(
            operation_id=operation_id,
            ledger_event_id=str(audit["ledger_event_id"]),
            audit_record_id=f"capability_audit_{operation_id[:24]}",
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_digest=entity_digest,
            scope=SCOPE,
            capability_scope=CAPABILITY_SCOPE,
            payload=audit,
            created_at=created_at,
        )
    )


def _operation_id(payload: dict) -> str:
    return sha256(canonical_payload_json(payload).encode("utf-8")).hexdigest()


def test_rebuild_replays_future_dated_durable_definition_and_lifecycle_fact(tmp_path) -> None:
    """Online writes reject future facts; durable historical replay preserves them."""

    definition = CapabilityDefinition(
        capability_id="planning.future_replay",
        display_name="Future replay",
        description="A recorded fact used to validate time-independent rebuild.",
        owner="replay-test",
        risk_tier="bounded_read",
        tags=("replay",),
        provenance={"source": "replay-test"},
        created_at=FUTURE_CREATED,
        scope=CAPABILITY_SCOPE,
    )
    definition_payload = definition.to_dict()
    definition_request_key = "future-definition-audit"
    definition_request = {
        "schema": "capability.operation.v1",
        "action": "definition_registered",
        "entity_type": "definition",
        "entity_id": definition.capability_id,
        "entity_digest": definition.definition_digest,
        "scope": _scope_payload(),
        "capability_scope": CAPABILITY_SCOPE,
        "request_key": definition_request_key,
        "storage_context": {},
    }
    definition_operation_id = _operation_id(definition_request)
    definition_audit = {
        "schema": "capability.audit.v1",
        "operation_id": definition_operation_id,
        "ledger_event_id": f"capability-ledger-{definition_operation_id[:32]}",
        "action": "definition_registered",
        "entity_type": "definition",
        "entity_id": definition.capability_id,
        "entity_digest": definition.definition_digest,
        "scope": _scope_payload(),
        "capability_scope": CAPABILITY_SCOPE,
        "request_key": definition_request_key,
        "storage_context": {},
        "entity": definition_payload,
    }

    initial_state_payload = {
        "schema": "capability.lifecycle.v1",
        "entity_type": "definition",
        "entity_id": definition.capability_id,
        "entity_digest": definition.definition_digest,
        "status": "active",
        "effective_at": FUTURE_CREATED,
        "scope": _scope_payload(),
        "capability_scope": CAPABILITY_SCOPE,
    }
    initial_state_digest = payload_digest(initial_state_payload)
    transition_payload = {
        "schema": "capability.lifecycle.v1",
        "entity_type": "definition",
        "entity_id": definition.capability_id,
        "entity_digest": definition.definition_digest,
        "expected_state_version": 1,
        "expected_state_digest": initial_state_digest,
        "target_status": "deprecated",
        "effective_at": FUTURE_RETIRED,
        "reason": "recorded future lifecycle fact",
        "provenance": {"policy_id": "replay-temporal-test"},
    }
    transition_request_key = "future-definition-retire-audit"
    transition_id = f"lifecycle:definition:{definition.capability_id}:2"
    transition_request = {
        "schema": "capability.operation.v1",
        "action": "lifecycle_transition",
        "entity_type": "lifecycle_transition",
        "entity_id": transition_id,
        "scope": _scope_payload(),
        "capability_scope": CAPABILITY_SCOPE,
        "request_key": transition_request_key,
        "transition": transition_payload,
    }
    transition_operation_id = _operation_id(transition_request)
    transition_state_payload = {
        **transition_payload,
        "state_version": 2,
        "predecessor_state_version": 1,
        "predecessor_state_digest": initial_state_digest,
        "scope": _scope_payload(),
        "capability_scope": CAPABILITY_SCOPE,
    }
    transition_state_digest = payload_digest(transition_state_payload)
    transition_audit = {
        "schema": "capability.audit.v1",
        "operation_id": transition_operation_id,
        "ledger_event_id": f"capability-ledger-{transition_operation_id[:32]}",
        "action": "lifecycle_transition",
        "entity_type": "lifecycle_transition",
        "entity_id": transition_id,
        "entity_digest": transition_state_digest,
        "scope": _scope_payload(),
        "capability_scope": CAPABILITY_SCOPE,
        "request_key": transition_request_key,
        "storage_context": {},
        "entity": transition_state_payload,
    }

    store = RuntimeStore(tmp_path)
    try:
        definition_record = _record_for_audit(
            definition_audit,
            action="definition_registered",
            entity_type="definition",
            entity_id=definition.capability_id,
            entity_digest=definition.definition_digest,
            created_at=FUTURE_CREATED,
        )
        transition_record = _record_for_audit(
            transition_audit,
            action="lifecycle_transition",
            entity_type="lifecycle_transition",
            entity_id=transition_id,
            entity_digest=transition_state_digest,
            created_at=FUTURE_RETIRED,
        )
        store.log.append_payload(
            definition_record.to_dict(),
            operation_id=definition_operation_id,
        )
        store.log.append_payload(
            transition_record.to_dict(),
            operation_id=transition_operation_id,
        )

        rebuilt = store.rebuild_sqlite_from_jsonl(replace=True)
        assert rebuilt["ok"] is True, rebuilt
        state = store.sqlite.conn.execute(
            "SELECT status, state_version, state_digest FROM capability_entity_current_states "
            "WHERE entity_type='definition' AND entity_id=?",
            (definition.capability_id,),
        ).fetchone()
        assert state is not None
        assert (state["status"], state["state_version"], state["state_digest"]) == (
            "deprecated",
            2,
            transition_state_digest,
        )
    finally:
        store.close()
