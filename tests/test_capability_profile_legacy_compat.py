from __future__ import annotations

from hashlib import sha256
import json

from eimemory.capabilities.models import CapabilityDefinition, legacy_profile_payload
from eimemory.capabilities.profiles import CapabilityProfiles
from eimemory.capabilities.registry import CapabilityRegistry
from eimemory.models.records import ScopeRef
from eimemory.storage.capability_store import PendingCapabilityAudit
from eimemory.storage.jsonl import canonical_payload_json
from eimemory.storage.runtime_store import RuntimeStore, _capability_audit_record


SCOPE = ScopeRef(
    tenant_id="tenant-legacy-profile",
    agent_id="agent-legacy-profile",
    workspace_id="workspace-legacy-profile",
    user_id="user-legacy-profile",
)
STAMP = "2020-08-20T00:00:00+00:00"


def _legacy_profile_audit() -> tuple[dict, object, dict]:
    """Build a fact-shaped audit exactly as the WP3 Profile writer emitted."""

    raw_profile = {
        "profile_id": "profile.legacy:exact-v1",
        "requirements": {
            "planning.legacy": {
                "minimum_maturity": "observed",
                "min_pass_rate": 0.5,
                # WP3 accepted bounded declarative extension fields.  WP4
                # must preserve rather than reinterpret them during replay.
                "legacy_policy": "retain-for-audit",
            }
        },
        "created_at": STAMP,
        "status": "active",
        "scope": "global",
        "revision": "v1",
        "provenance": {"source": "wp3-fixture"},
    }
    payload, profile_digest = legacy_profile_payload(raw_profile)
    request_key = "wp3-profile-audit"
    request_payload = {
        "schema": "capability.operation.v1",
        "action": "profile_registered",
        "entity_type": "profile",
        "entity_id": payload["profile_id"],
        "entity_digest": profile_digest,
        "scope": {
            "tenant_id": SCOPE.tenant_id,
            "agent_id": SCOPE.agent_id,
            "workspace_id": SCOPE.workspace_id,
            "user_id": SCOPE.user_id,
        },
        "capability_scope": "global",
        "request_key": request_key,
        "storage_context": {},
    }
    operation_id = sha256(canonical_payload_json(request_payload).encode("utf-8")).hexdigest()
    ledger_event_id = f"capability-ledger-{operation_id[:32]}"
    audit = {
        "schema": "capability.audit.v1",
        "operation_id": operation_id,
        "ledger_event_id": ledger_event_id,
        "action": "profile_registered",
        "entity_type": "profile",
        "entity_id": payload["profile_id"],
        "entity_digest": profile_digest,
        "scope": request_payload["scope"],
        "capability_scope": "global",
        "request_key": request_key,
        "storage_context": {},
        "entity": payload,
    }
    record = _capability_audit_record(
        PendingCapabilityAudit(
            operation_id=operation_id,
            ledger_event_id=ledger_event_id,
            audit_record_id=f"capability_audit_{operation_id[:24]}",
            action="profile_registered",
            entity_type="profile",
            entity_id=payload["profile_id"],
            entity_digest=profile_digest,
            scope=SCOPE,
            capability_scope="global",
            payload=audit,
            created_at=payload["created_at"],
        )
    )
    return audit, record, payload


def test_wp3_profile_audit_rebuilds_with_original_digest_and_resolves_without_lineage(tmp_path) -> None:
    store = RuntimeStore(tmp_path)
    try:
        _audit, record, legacy_payload = _legacy_profile_audit()
        operation_id = str(record.meta["operation_id"])
        store.log.append_payload(record.to_dict(), operation_id=operation_id)

        rebuilt = store.rebuild_sqlite_from_jsonl(replace=True)
        assert rebuilt["ok"] is True, rebuilt
        row = store.sqlite.conn.execute(
            "SELECT profile_digest, payload_json FROM capability_profiles WHERE profile_id=?",
            (legacy_payload["profile_id"],),
        ).fetchone()
        assert row is not None
        assert row["profile_digest"] == legacy_payload["profile_digest"]
        assert "profile_key" not in json.loads(row["payload_json"])

        # Simulate a pre-WP4 SQLite database: its original Profile row exists
        # but no additive lineage index has been backfilled.  The bounded
        # read-only fallback must still expose it under legacy profile_id.
        store.sqlite.conn.execute("DELETE FROM capability_profile_lineage")
        store.sqlite.conn.commit()
        definition = CapabilityDefinition(
            capability_id="planning.legacy",
            display_name="Legacy planning",
            description="A definition used to verify historical Profile reads.",
            owner="compat-test",
            risk_tier="bounded_read",
            tags=("legacy",),
            provenance={"source": "compat-test"},
            created_at=STAMP,
            scope="global",
        )
        CapabilityRegistry(store).register_definition(
            definition,
            runtime_scope=SCOPE,
            request_key="legacy-definition",
        )
        resolved = CapabilityProfiles(store).resolve(
            "profile.legacy:exact-v1",
            runtime_scope=SCOPE,
            capability_scope="global",
        )
        assert resolved["profile"]["profile_digest"] == legacy_payload["profile_digest"]
        assert resolved["profile"]["profile_key"] == legacy_payload["profile_id"]
        assert resolved["requirements"][0]["requirement"]["legacy_policy"] == "retain-for-audit"
    finally:
        store.close()
