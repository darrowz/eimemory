"""Persist one release-bound native L5 reading without inventing completion."""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
import re
from typing import Any, Mapping

from eimemory.capabilities.profile_bootstrap import DEFAULT_L5_PROFILE_KEY
from eimemory.capabilities.registry import exact_runtime_scope
from eimemory.config.loader import load_settings
from eimemory.contracts.release_identity import release_identity_payload, same_release_authority
from eimemory.governance.release.evidence_contract import current_release_identity
from eimemory.governance.learning.learning_state import append_learning_record_once
from eimemory.governance.l5.l5_scope_authority import authorized_capability_scopes
from eimemory.identity import default_operator_user_id
from eimemory.models.records import ScopeRef
from eimemory.models.source_partitions import DEFAULT_SOURCE_ID


SCHEMA = "native_l5_assessment_maintenance.v1"
SOURCE = "eimemory.l5.native_assessment"


def effective_l5_profile(profile_key: str = "") -> str:
    return str(profile_key or os.environ.get("EIMEMORY_L5_V3_PROFILE") or DEFAULT_L5_PROFILE_KEY).strip()


def _capability_selector(owner: ScopeRef) -> ScopeRef:
    """Use deployment configuration only within this caller's sharing grant.

    Report ownership never supplies the product identity. Tenant isolation is
    retained, and unrecognized users cannot borrow the deployment operator's
    capability registry merely by selecting a channel workspace.
    """
    settings = load_settings()
    candidate = ScopeRef(tenant_id=owner.tenant_id,
        agent_id=str(os.environ.get("EIMEMORY_AGENT_ID") or
            os.environ.get("EIMEMORY_DEPLOY_SCOPE_AGENT") or settings.default_agent_id),
        workspace_id=str(os.environ.get("EIMEMORY_WORKSPACE_ID") or
            os.environ.get("EIMEMORY_DEPLOY_SCOPE_WORKSPACE") or settings.default_workspace_id),
        user_id=default_operator_user_id())
    return candidate if candidate in authorized_capability_scopes(owner) else owner


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _execution_error(value: Any, depth: int = 0) -> bool:
    if depth > 16:
        return True
    if isinstance(value, dict):
        if any(value.get(key) for key in ("error", "errors", "ledger_error")):
            return True
        if str(value.get("reason") or "").startswith("capability_projection_unavailable:"):
            return True
        return any(_execution_error(child, depth + 1) for child in value.values())
    if isinstance(value, list):
        return any(_execution_error(child, depth + 1) for child in value)
    return False


def _negative_evidence(envelope: dict[str, Any]) -> bool:
    raw = envelope["assessment"]
    projection = raw.get("projection")
    snapshots = projection.get("snapshots", []) if isinstance(projection, dict) else []
    if any(isinstance(row, dict) and row.get("maturity") in {"regressed", "quarantined"}
           for row in snapshots):
        return True
    deployment = raw.get("deployment_assurance")
    if isinstance(deployment, dict) and deployment.get("required") is True and deployment.get("blocking") is True:
        return True
    transaction = envelope.get("transaction_evidence")
    if isinstance(transaction, dict):
        from eimemory.contracts.code_incident_provenance import qualification_provenance
        transaction = qualification_provenance(transaction)
        if transaction.get("quarantined") is True:
            return True
        if any(transaction.get(key) is True for key in
                ('known_before_detection', 'prior_user_reported', 'manual_bootstrap')):
            return True
        # Existing immutable proof rejected by the native verifier is not the
        # same claim as having no terminal proof yet. Retain that distinction.
        error = transaction.get("evidence_error")
        if error and error not in {"quality_repair_provenance_unproven", "quality_repair_release_unbound"}:
            return True
    return False


def run_native_l5_assessment(runtime: Any, *, scope: Mapping[str, Any],
                             profile_key: str = "", capability_scope: str = "global",
                             loop_id: str = "nightly_l5_native") -> dict[str, Any]:
    """Observe once, retain the real v4 envelope and distinguish all three gates.

    The raw builder owns normalized persistence and trusted registry aliasing.
    Report and quality/business evidence ownership remain the caller's exact
    scope. No bootstrap, event, terminal receipt or prior-knowledge fact is
    created here.
    """
    result: dict[str, Any] = {"schema": SCHEMA, "report_type": "native_l5_assessment",
        "ok": False, "execution_ok": False, "enabled": True, "control_plane_ok": False,
        "product_l5_complete": False, "status": "failed", "awaiting_evidence": False,
        "archive_verified": False, "record_id": ""}
    try:
        owner = exact_runtime_scope(scope)
        selector = _capability_selector(owner)
        profile = effective_l5_profile(profile_key)
        before = current_release_identity(runtime, owner)
        if before is None or not before.complete:
            raise ValueError("native_l5_current_release_unverified")
        release = release_identity_payload(before)
        result.update(scope=asdict(owner), selector_scope=asdict(selector), profile_key=profile, **release)
        envelope = runtime.build_l5_readiness_report(scope=asdict(owner), runtime_scope=selector,
            reader_mode="v3", profile_key=profile, capability_scope=capability_scope,
            persist=True, loop_id=loop_id)
        if (not isinstance(envelope, dict) or envelope.get("schema") != "l5.reader.v4"
                or envelope.get("schema_version") != "l5_readiness.v4"
                or envelope.get("reader_mode") != "v3"
                or envelope.get("scope") != asdict(owner)
                or envelope.get("profile_key") != profile
                or type(envelope.get("control_plane_ok")) is not bool
                or type(envelope.get("product_l5_complete")) is not bool
                or envelope.get("ok") is not envelope["product_l5_complete"]
                or not isinstance(envelope.get("assessment"), dict)):
            raise ValueError("native_l5_envelope_contract_invalid")
        # Includes exception states deliberately caught by the raw reader.
        # Retain a structurally valid failed reading, but never waive it as
        # evidence starvation or let it certify product completion.
        reader_failed = _execution_error(envelope)
        evidence_failed = _negative_evidence(envelope)
        raw = envelope["assessment"]
        if raw.get("assessment_id"):
            receipt = raw.get("persisted")
            if (not isinstance(raw["assessment_id"], str)
                    or not isinstance(raw.get("assessment_digest"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", raw["assessment_digest"]) is None
                    or not isinstance(receipt, dict) or receipt.get("entity_type") != "assessment"
                    or receipt.get("entity_id") != raw["assessment_id"]
                    or receipt.get("entity_digest") != raw.get("assessment_digest")):
                raise ValueError("native_l5_normalized_assessment_not_persisted")
        if not same_release_authority(before, current_release_identity(runtime, owner)):
            raise ValueError("native_l5_release_authority_changed")
        digest = _digest(envelope)
        complete = envelope["product_l5_complete"]
        if complete and not raw.get("assessment_id"):
            raise ValueError("native_l5_normalized_assessment_not_persisted")
        if (envelope.get("status") != ("ready" if complete else "incomplete")
                or envelope.get("completion_status") != ("complete" if complete else "incomplete")
                or complete and (not envelope["control_plane_ok"] or reader_failed or evidence_failed)):
            raise ValueError("native_l5_completion_contract_invalid")
        content = {"schema": SCHEMA, "report_type": "native_l5_assessment",
            "scope": asdict(owner), "selector_scope": asdict(selector), "profile_key": profile,
            "execution_ok": not reader_failed,
            "control_plane_ok": envelope["control_plane_ok"], "product_l5_complete": complete,
            "evidence_failed": evidence_failed,
            "envelope_digest": digest, "envelope": envelope}
        record = append_learning_record_once(runtime, kind="reflection",
            title="Native L5 product assessment", summary="complete" if complete else "incomplete",
            scope=owner, loop_id=loop_id, step_name="native_l5_assessment",
            semantic_key=digest, content=content,
            meta={"report_type": "native_l5_assessment", "envelope_digest": digest},
            source=SOURCE, status="active")
        result["record_id"] = record.record_id
        records = runtime.store.list_records_by_sources_exact_scope(scope=owner,
            sources=[SOURCE], kinds=["reflection"], record_ids=[record.record_id], limit=2)
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError("native_l5_archive_missing_after_write")
        record = records[0]
        saved = record.content if isinstance(record.content, dict) else {}
        if (record.scope != owner or record.source != SOURCE or record.kind != "reflection"
                or record.status != "active" or record.source_id != DEFAULT_SOURCE_ID
                or record.record_id != result["record_id"]
                or saved.get("envelope_digest") != digest or _digest(saved.get("envelope")) != digest
                or any(saved.get(key) != content[key] for key in
                       ("scope", "selector_scope", "profile_key", "execution_ok", "control_plane_ok",
                        "product_l5_complete", "evidence_failed"))
                or any(record.meta.get(key) != value or saved.get(key) != value
                       for key, value in release.items())
                or not same_release_authority(before, current_release_identity(runtime, owner))):
            raise ValueError("native_l5_archive_authority_mismatch")
        result.update(ok=complete and not reader_failed, execution_ok=not reader_failed,
            control_plane_ok=envelope["control_plane_ok"], product_l5_complete=complete,
            status="failed" if reader_failed else "blocked" if evidence_failed else "complete" if complete else "incomplete",
            awaiting_evidence=not complete and not reader_failed and not evidence_failed,
            evidence_failed=evidence_failed, archive_verified=True, envelope_digest=digest,
            assessment_id=str(raw.get("assessment_id") or ""),
            assessment_persisted=isinstance(raw.get("persisted"), dict),
            gaps=envelope.get("gaps", []), envelope=envelope)
        if reader_failed:
            result["error"] = "native_l5_reader_execution_failed"
        return result
    except Exception as exc:
        # Preserve failed captures and exception type, never arbitrary provider
        # text. A valid incomplete product is the only non-fatal wait below.
        result.update(error=str(exc) if isinstance(exc, ValueError) and str(exc).startswith("native_l5_")
                      else "native_l5_execution_failed", error_type=type(exc).__name__)
        return result
