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
from eimemory.contracts.nightly_step_evidence import NATIVE_L5_ERROR_CODES, exception_type_name


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
        # known_before_detection / prior_user_reported / manual_bootstrap are
        # product-completion gaps when unproven. A verified True is adverse
        # evidence, not an evidence wait, and must not be waived.
        if any(transaction.get(key) is True for key in (
                'known_before_detection', 'prior_user_reported', 'manual_bootstrap')):
            return True
        if 'managed_incident_proof' in transaction:
            from eimemory.governance.evolution.managed_code_incident_proof import qualification_evidence
            try:
                if any(value is True for value in qualification_evidence(transaction)['facts'].values()):return True
            except (TypeError,ValueError,RuntimeError,OSError):return True
        # Existing immutable proof rejected by the native verifier is not the
        # same claim as having no terminal proof yet. Retain that distinction.
        # terminal_state_outcome_mismatch on a non-qualifying terminal (for
        # example aborted_candidate_restored) means the sample does not
        # qualify for product L5; product completion already records gaps.
        error = transaction.get("evidence_error")
        outcome = str(transaction.get("qualifying_terminal_outcome") or "")
        non_qualifying_mismatch = (
            error == "terminal_state_outcome_mismatch"
            and outcome not in {"succeeded_sedimented", "rolled_back_healthy"}
        )
        if (
            error
            and error not in {"quality_repair_provenance_unproven", "quality_repair_release_unbound"}
            and not non_qualifying_mismatch
        ):
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
    stage = "scope_selection"
    try:
        owner = exact_runtime_scope(scope)
        selector = _capability_selector(owner)
        profile = effective_l5_profile(profile_key)
        from eimemory.governance.evolution.code_evolution_provider_authority import (
            delegated_provider_authority, verify_provider_authority,
        )
        stage = "provider_authority"
        authority = delegated_provider_authority(runtime, owner_scope=owner, profile_key=profile)
        if authority is not None:
            selector = ScopeRef.from_dict(authority['provider_scope'])
        stage = "release_authority"
        before = current_release_identity(runtime, owner)
        if before is None or not before.complete:
            raise ValueError("native_l5_current_release_unverified")
        release = release_identity_payload(before)
        result.update(scope=asdict(owner), selector_scope=asdict(selector), profile_key=profile, **release)
        version_parts = str(before.version or '').split('.')
        if (len(version_parts) == 3 and all(part.isdigit() for part in version_parts)
                and tuple(map(int, version_parts)) >= (1, 14, 156)):
            from eimemory.governance.release.controlled_release_lineage import refresh_current_controlled_release_lineage

            stage = 'controlled_release_lineage'
            result['controlled_release_lineage'] = refresh_current_controlled_release_lineage(
                runtime, scope=owner, release=before,
            )
            if result['controlled_release_lineage'].get('ok') is not True:
                raise ValueError('native_l5_controlled_lineage_revalidation_failed')
        stage = "readiness_reader"
        envelope = runtime.build_l5_readiness_report(scope=asdict(owner), runtime_scope=selector,
            reader_mode="v3", profile_key=profile, capability_scope=capability_scope,
            persist=True, loop_id=loop_id)
        if authority is not None:
            stage = "provider_authority_recheck"
            verify_provider_authority(runtime, authority, owner_scope=owner, profile_key=profile)
            envelope['capability_authority'] = authority
        stage = "envelope_validation"
        domain_pair=(envelope.get('schema'),envelope.get('schema_version')) if isinstance(envelope,dict) else None
        if (not isinstance(envelope, dict) or domain_pair not in {
                    ('l5.reader.v4','l5_readiness.v4'),('l5.reader.v5','l5_readiness.v5')}
                or domain_pair==('l5.reader.v5','l5_readiness.v5') and (
                    envelope.get('qualification_domain')!='registered_code_defect_knowledge.v1'
                    or 'managed_incident_proof' not in envelope.get('transaction_evidence',{}))
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
        stage = "evidence_classification"
        reader_failed = _execution_error(envelope)
        evidence_failed = _negative_evidence(envelope)
        raw = envelope["assessment"]
        stage = "normalized_assessment"
        if raw.get("assessment_id"):
            receipt = raw.get("persisted")
            if (not isinstance(raw["assessment_id"], str)
                    or not isinstance(raw.get("assessment_digest"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", raw["assessment_digest"]) is None
                    or not isinstance(receipt, dict) or receipt.get("entity_type") != "assessment"
                    or receipt.get("entity_id") != raw["assessment_id"]
                    or receipt.get("entity_digest") != raw.get("assessment_digest")):
                raise ValueError("native_l5_normalized_assessment_not_persisted")
        stage = "release_recheck"
        if not same_release_authority(before, current_release_identity(runtime, owner)):
            raise ValueError("native_l5_release_authority_changed")
        stage = "completion_validation"
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
        stage = "archive_write"
        record = append_learning_record_once(runtime, kind="reflection",
            title="Native L5 product assessment", summary="complete" if complete else "incomplete",
            scope=owner, loop_id=loop_id, step_name="native_l5_assessment",
            semantic_key=digest, content=content,
            meta={"report_type": "native_l5_assessment", "envelope_digest": digest},
            source=SOURCE, status="active")
        result["record_id"] = record.record_id
        stage = "archive_readback"
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
        stage = "provider_authority_recheck"
        if authority is not None:
            # The archive retains the original reading if the root delegation
            # changes during persistence; it must not certify current authority.
            verify_provider_authority(runtime, authority, owner_scope=owner, profile_key=profile)
        stage = "result_projection"
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
            result["failure_stage"] = "readiness_reader"
        return result
    except Exception as exc:
        # Preserve failed captures and exception type, never arbitrary provider
        # text. A valid incomplete product is the only non-fatal wait below.
        result.update(error=str(exc) if isinstance(exc, ValueError) and str(exc) in NATIVE_L5_ERROR_CODES
                      else "native_l5_execution_failed", error_type=exception_type_name(exc), failure_stage=stage)
        try:
            from eimemory.governance.learning.nightly_step_evidence import archive_nightly_step_evidence
            result["failure_evidence"] = archive_nightly_step_evidence(
                runtime, scope=scope, run_id=loop_id, step="native_l5_assessment_failure", report=result)
        except Exception as archive_error:
            # Do not recurse or replace the actual failure with an archive error.
            result["failure_evidence"] = {"ok": False, "archive_verified": False, "archive_complete": False,
                "reason": "nightly_step_evidence_unverified", "error_type": exception_type_name(archive_error)}
        return result
