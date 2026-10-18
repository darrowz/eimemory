"""Route trusted system incidents into the governed code-evolution v2 path."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import subprocess
from typing import Any

from eimemory.governance.evolution.code_evolution_repository import protected_paths_digest
from eimemory.governance.evolution.code_evolution_test_plans import (
    INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
    RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID,
    RELEASE_REPORT_FAILURE_TEST_PLAN_ID,
    RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
    allowed_files_for_incident,
    protected_test_plan_digest,
)
from eimemory.ops.system_code_repair_failure import (
    DETECTOR_ID as SYSTEM_REPAIR_FAILURE_DETECTOR_ID,
    INCIDENT_CLASS as SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS,
    SOURCE as SYSTEM_REPAIR_FAILURE_SOURCE,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.code_evolution_store import CodeEvolutionStore


_ROUTES = {
    "deployment.runtime_commit_drift": (
        "eimemory.runtime_identity_drift",
        RUNTIME_IDENTITY_DRIFT_TEST_PLAN_ID,
    ),
    "release.closure_internal_failure": (
        "eimemory.release_closure_failure",
        RELEASE_CLOSURE_FAILURE_TEST_PLAN_ID,
    ),
    "release.closure_report_failure": (
        "eimemory.release_closure_failure",
        RELEASE_REPORT_FAILURE_TEST_PLAN_ID,
    ),
    SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS: (
        SYSTEM_REPAIR_FAILURE_SOURCE,
        INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
    ),
}
_INCIDENT_FIELDS = (
    "incident_id",
    "incident_digest",
    "incident_class",
    "title",
    "summary",
    "diagnostic_codes",
    "acceptance_requirements",
)


def _resolve_repo_root(repo_root: str | Path | None) -> str:
    if repo_root is not None and str(repo_root).strip():
        return str(repo_root)
    from eimemory.config.trusted import trusted_repository_root
    return str(trusted_repository_root())


def process_system_code_incidents(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    repo_root: str | Path | None = None,
    max_items: int = 1,
) -> dict[str, Any]:
    """Submit at most ``max_items`` genuine detector incidents for repair."""

    from eimemory.governance.evolution.code_evolution_bridge import propose_code_patch_v2
    from eimemory.governance.release.evidence_contract import current_release_identity

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))
    scope_payload = {
        "tenant_id": scope_ref.tenant_id,
        "agent_id": scope_ref.agent_id,
        "workspace_id": scope_ref.workspace_id,
        "user_id": scope_ref.user_id,
    }
    root = Path(_resolve_repo_root(repo_root)).expanduser().resolve()
    repository = _repository_identity(root)
    if repository.get("ok") is not True:
        return {"ok": False, "status": "blocked", "reason": repository.get("reason"), "processed": []}
    release = current_release_identity(runtime, scope_ref)
    if release is None or release.commit != repository["base_commit"]:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "repository_release_identity_mismatch",
            "processed": [],
        }

    try:
        records = runtime.store.list_records_by_sources_exact_scope(
            kinds=["incident"], sources=list(dict.fromkeys(route[0] for route in _ROUTES.values())),
            scope=scope_ref, limit=501)
    except (AttributeError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error):
        return {"ok": False, "status": "blocked", "reason": "incident_source_page_unavailable",
                "source_page_complete": False, "repair_complete": False, "processed": []}
    if not isinstance(records, list) or len(records) > 500:
        return {"ok": False, "status": "blocked", "reason": "incident_source_page_incomplete",
                "source_page_complete": False, "repair_complete": False, "processed": []}
    sources = {route[0] for route in _ROUTES.values()}
    if any(getattr(record, "scope", None) != scope_ref or getattr(record, "source", None) not in sources
           or getattr(record, "kind", None) != "incident" for record in records):
        return {"ok": False, "status": "blocked", "reason": "incident_source_page_mismatched",
                "source_page_complete": False, "repair_complete": False, "processed": []}
    unknown_lifecycle = []
    for record in records:
        if (getattr(record, "status", None) not in {"active", "resolved", "archived", "quarantined", "inactive", "superseded", "rejected"}
                and (_trusted_incident_identity(record, repository["base_commit"]) is not None
                     or _trusted_incident_identity(record, repository["base_commit"], diagnosis=True) is not None)):
            unknown_lifecycle.append(record.record_id)
    if unknown_lifecycle:
        return {"ok": False, "status": "blocked", "reason": "current_incident_lifecycle_unverified",
                "repair_complete": False, "pending_incident_record_ids": unknown_lifecycle, "processed": []}
    current = [(record, incident) for record in records
               if (incident := _trusted_incident(record, repository["base_commit"])) is not None]
    if any(incident["incident_class"] == "deployment.runtime_commit_drift" for _, incident in current):
        from eimemory.ops.runtime_identity_drift import current_runtime_identity_incident
        try:
            current_digest = current_runtime_identity_incident(
                runtime.store, scope=scope_ref, expected_commit=repository["base_commit"])
        except (AttributeError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error):
            current_digest = None
        if current_digest is None:
            return {"ok": False, "status": "blocked", "reason": "runtime_identity_current_observation_unverified",
                    "repair_complete": False, "processed": []}
        current = [(record, incident) for record, incident in current
                   if incident["incident_class"] != "deployment.runtime_commit_drift"
                   or incident["incident_digest"] == current_digest]
    diagnosis = [record.record_id for record in records
                 if _trusted_incident(record, repository["base_commit"], diagnosis=True) is not None]
    if not current:
        if diagnosis:
            return {"ok": False, "status": "diagnosis_required", "processed": [],
                    "reason": "closure_diagnosis_not_completed", "repair_complete": False,
                    "pending_incident_record_ids": diagnosis}
        return {"ok": True, "status": "idle", "processed": []}
    # Policy is required only after exact current detector evidence exists.
    # Historical incidents and unrelated monitor noise cannot make an
    # otherwise healthy owner depend on a missing or consumed grant.
    policy_incident_digest, automation_policy_digest, profile_key = _automation_policy_identity()
    if not policy_incident_digest or not automation_policy_digest or not profile_key:
        return {
            "ok": False,
            "status": "blocked",
            "reason": "automation_policy_identity_unavailable",
            "repair_complete": False,
            "pending_incident_record_ids": [record.record_id for record, _ in current],
            "processed": [],
        }
    eligible = []
    for record, incident in current:
        if policy_incident_digest and incident["incident_digest"] != policy_incident_digest:
            continue
        incident_class = incident["incident_class"]
        source, plan_id = _ROUTES[incident_class]
        if str(getattr(record, "source", "") or "") != source:
            continue
        eligible.append((record, incident))
    if not eligible:
        pending = [record.record_id for record, _ in current]
        if pending:
            return {"ok": False, "status": "blocked", "processed": [],
                    "reason": "incident_policy_digest_mismatch", "repair_complete": False,
                    "pending_incident_record_ids": pending}
        return {"ok": True, "status": "idle", "processed": []}
    # A one-shot policy already consumed by a prior transaction cannot fund a
    # new candidate.  Stop before provider calls and verification; active
    # transactions are resumed by the separate recovery owner.
    ledger = CodeEvolutionStore(runtime.store)
    consumption = ledger.get_policy_consumption(automation_policy_digest)
    if consumption is not None:
        existing = _matching_consumed_transaction(
            ledger, consumption, eligible=eligible, scope=scope_payload,
            repository_root=str(root), base_commit=repository["base_commit"],
            policy_digest=automation_policy_digest, profile_key=profile_key,
        )
        if existing is not None:
            return {"ok": True, "status": "existing", "repair_complete": False,
                    "processed": [existing]}
        return {
            "ok": False,
            "status": "blocked",
            "reason": "automation_policy_already_consumed",
            "policy_transaction_id": str(consumption.get("transaction_id") or ""),
            "processed": [],
        }
    from eimemory.capabilities.profiles import CapabilityProfiles, CapabilityProfileError

    try:
        CapabilityProfiles(runtime.store).resolve(profile_key, runtime_scope=scope_ref, capability_scope="global")
    except CapabilityProfileError:
        return {
            "ok": False, "status": "blocked", "reason": "automation_policy_profile_unavailable",
            "profile_key": profile_key, "processed": [],
        }
    processed: list[dict[str, Any]] = []
    for record, incident in eligible:
        incident_class = incident["incident_class"]
        source, plan_id = _ROUTES[incident_class]
        if len(processed) >= max(0, min(10, int(max_items))):
            break
        transaction_id = _stable_id(
            "system-repair",
            incident["incident_digest"],
            repository["base_commit"],
            automation_policy_digest,
        )
        existing = ledger.get_transaction(transaction_id)
        if existing is not None:
            processed.append(
                {
                    "incident_id": incident["incident_id"],
                    "transaction_id": transaction_id,
                    "status": str(existing.get("current_state") or "existing"),
                    "idempotent": True,
                }
            )
            continue
        allowed_files = allowed_files_for_incident(incident_class, test_plan_id=plan_id)
        try:
            proposal = propose_code_patch_v2(
                runtime,
                transaction_id=transaction_id,
                request_id=_stable_id("system-repair-request", transaction_id),
                nonce=_stable_id("system-repair-nonce", transaction_id),
                incident=incident,
                scope=scope_payload,
                profile_key=profile_key,
                repo_root=root,
                base_commit=repository["base_commit"],
                base_tree_digest=protected_paths_digest(root, allowed_files),
                allowed_files=allowed_files,
                test_plan_id=plan_id,
                test_plan_digest=protected_test_plan_digest(plan_id),
                bounds={
                    "maximum_files": len(allowed_files),
                    "maximum_bytes_per_file": 48 * 1024,
                    "maximum_total_bytes": 96 * 1024,
                    "maximum_changed_lines": 400,
                },
                origin="system_detector",
                detector=str(record.provenance.get("detector") or ""),
                known_before_detection=False,
                prior_user_reported=False,
                manual_bootstrap=False,
            )
        except Exception as exc:
            proposal = {"ok": False, "reason": f"proposal_error:{type(exc).__name__}"}
        if not isinstance(proposal, Mapping):
            proposal = {"ok": False, "reason": "proposal_result_not_object"}
        if proposal.get("ok") is not True:
            processed.append(
                {
                    "incident_id": incident["incident_id"],
                    "transaction_id": transaction_id,
                    "status": "proposal_blocked",
                    "reason": str(proposal.get("reason") or "proposal_unavailable"),
                }
            )
            continue
        opportunity = {
            "opportunity_id": f"system-incident:{incident['incident_id']}",
            "opportunity_type": "code_patch",
            "source": "system_detector",
            "risk_level": "medium",
            "trigger": incident["title"],
            "policy_update": "Correct the detected defect and pass the protected focused and regression plan.",
            "source_event_payload": {
                "verification": "detector observation is valid",
                "incident_id": incident["incident_id"],
            },
            "source_outcome_payload": {
                "source_trust": "system_verified",
                "verification": "persisted system detector incident",
            },
            "code_evolution_proposal": proposal,
        }
        try:
            evolution = runtime.run_autonomous_evolution(
                scope=scope_payload,
                apply=True,
                opportunities=[opportunity],
                mine_events=False,
                max_apply=1,
                persist_report=True,
            )
        except Exception as exc:
            processed.append(
                {
                    "incident_id": incident["incident_id"],
                    "transaction_id": transaction_id,
                    "status": "submission_failed",
                    "reason": f"autonomous_evolution_error:{type(exc).__name__}",
                }
            )
            continue
        processed.append(
            {
                "incident_id": incident["incident_id"],
                "transaction_id": transaction_id,
                "status": "submitted" if isinstance(evolution, Mapping) and evolution.get("ok") is True else "submission_failed",
                "reason": "" if isinstance(evolution, Mapping) and evolution.get("ok") is True else "autonomous_evolution_not_ok",
                "evolution": evolution,
            }
        )
    # Persist the hand-off result separately from the immutable detector
    # observation. A submitted proposal is not a completed repair.
    by_incident = {incident["incident_id"]: record for record, incident in eligible}
    for item in processed:
        item["incident_record_id"] = by_incident[item["incident_id"]].record_id
        item["repair_complete"] = False
        item["attempt_record_id"] = _record_repair_attempt(runtime, scope_ref, item)
    processing_ok = all(item.get("status") not in {"proposal_blocked", "submission_failed"} for item in processed)
    return {
        "ok": processing_ok,
        "status": ("processed" if processing_ok else "blocked") if processed else "idle",
        "repair_complete": False,
        "processed": processed,
    }


def _matching_consumed_transaction(
    ledger: CodeEvolutionStore, consumption: Mapping[str, Any], *,
    eligible: list[tuple[Any, dict[str, Any]]], scope: Mapping[str, str],
    repository_root: str, base_commit: str, policy_digest: str, profile_key: str,
) -> dict[str, Any] | None:
    """Recognize an existing hand-off without authorizing another effect.

    The recovery owner retains responsibility for current effects authority.
    This read-only path must bind the original consumption to precisely the
    current owner's still-active transaction before suppressing a new attempt.
    """
    from eimemory.governance.evolution.code_evolution_transaction import STATES, TERMINAL_STATES
    from eimemory.storage.code_evolution_store import digest_json

    try:
        if not isinstance(consumption, Mapping) or consumption.get("policy_digest") != policy_digest:
            return None
        matches = [(record, incident) for record, incident in eligible
                   if consumption.get("transaction_id") == _stable_id(
                       "system-repair", incident["incident_digest"], base_commit, policy_digest)]
        if len(matches) != 1:
            return None
        record, incident = matches[0]
        transaction_id = consumption["transaction_id"]
        transaction = ledger.get_transaction(transaction_id)
        if not isinstance(transaction, Mapping):
            return None
        payload = transaction.get("payload")
        if (not isinstance(payload, Mapping) or payload.get("scope") != dict(scope)
                or payload.get("transaction_id") != transaction_id
                or any(transaction.get(field) != value for field, value in scope.items())
                or transaction.get("transaction_id") != transaction_id
                or transaction.get("terminal") != 0
                or transaction.get("current_state") not in set(STATES).difference(TERMINAL_STATES)
                or transaction.get("repository_root") != repository_root
                or transaction.get("repository_ref") not in {"master", "refs/heads/master"}
                or transaction.get("base_commit") != base_commit
                or transaction.get("incident_id") != incident["incident_id"]
                or transaction.get("incident_digest") != incident["incident_digest"]
                or transaction.get("incident_class") != incident["incident_class"]
                or transaction.get("origin") != "system_detector"
                or transaction.get("detector") != record.provenance.get("detector")
                or transaction.get("profile_key") != profile_key
                or transaction.get("policy_digest") != policy_digest):
            return None
        source_incident = payload.get("incident")
        source_repository = payload.get("repository")
        if (not isinstance(source_incident, Mapping) or not isinstance(source_repository, Mapping)
                or any(source_incident.get(field) != incident[field]
                       for field in ("incident_id", "incident_digest", "incident_class"))
                or (source_repository.get("repository_root") or source_repository.get("root")) != repository_root
                or (source_repository.get("repository_ref") or source_repository.get("ref")) != transaction["repository_ref"]
                or source_repository.get("base_commit") != base_commit
                or payload.get("origin") != transaction["origin"]
                or payload.get("detector") != transaction["detector"]):
            return None
        stored = json.loads(consumption.get("payload_json", ""))
        if not isinstance(stored, dict):
            return None
        material, policy = stored.get("authorization_material"), stored.get("authorized_policy")
        authorization_digest = consumption.get("authorization_receipt_digest")
        if (not isinstance(material, dict) or not isinstance(policy, dict)
                or material.get("schema") != "code_automation_authorization.v2"
                or material.get("transaction_id") != transaction_id
                or material.get("policy_digest") != policy_digest
                or material.get("incident_digest") != incident["incident_digest"]
                or digest_json(material) != authorization_digest
                or transaction.get("authorization_digest") != authorization_digest
                or policy.get("ok") is not True or policy.get("policy_digest") != policy_digest
                or payload.get("authorized_policy") != policy):
            return None
        for key, fields in (
            ("provider", ("capability_id", "revision_id", "binding_id", "provider_kind", "provider_instance_id", "implementation_digest")),
            ("advertisement", ("advertisement_id", "advertisement_digest")),
            ("catalog", ("catalog_case_id", "catalog_snapshot_digest")),
            ("repository", ("repository_root", "repository_remote", "repository_ref", "base_commit", "base_tree_digest")),
        ):
            if material.get(key) != {field: transaction.get(field) for field in fields}:
                return None
        if (any(material.get(field) != transaction.get(field)
                for field in ("proposal_digest", "patch_digest", "candidate_tree_digest"))
                or any(material.get(field) != policy.get(field) for field in ("effects", "deployment"))):
            return None
        # These are original proposal coordinates, unlike candidate/state/
        # authorization outputs, which native stages update only in SQL.
        # Inspect both persisted source layers when a v2 proposal is present;
        # a correct consumption cannot repair a damaged proposal projection.
        layers = [payload]
        if payload.get("payload"):
            if not isinstance(payload["payload"], Mapping):
                return None
            layers.append(payload["payload"])
        for layer in layers:
            provider, repository = layer.get("provider"), layer.get("repository")
            original_incident = layer.get("incident")
            if (not isinstance(provider, Mapping) or not isinstance(repository, Mapping)
                    or not isinstance(original_incident, Mapping)
                    or any(provider.get(field) != transaction.get(field) for field in material["provider"])
                    or any(original_incident.get(field) != incident[field]
                           for field in ("incident_id", "incident_digest", "incident_class"))
                    or (repository.get("repository_root") or repository.get("root")) != transaction["repository_root"]
                    or (repository.get("repository_ref") or repository.get("ref")) != transaction["repository_ref"]
                    or any(repository.get(field) != transaction.get(field)
                           for field in ("repository_remote", "base_commit", "base_tree_digest"))
                    or layer.get("proposal_digest") != transaction.get("proposal_digest")
                    or (layer.get("patch_digest") or layer.get("proposal_digest")) != transaction.get("patch_digest")):
                return None
            for key, fields in (("advertisement", ("advertisement_id", "advertisement_digest")),
                                ("catalog", ("catalog_case_id", "catalog_snapshot_digest"))):
                coordinates = layer.get(key) if layer is not payload else layer
                if not isinstance(coordinates, Mapping) or any(
                        coordinates.get(field) != transaction.get(field) for field in fields):
                    return None
        return {"incident_id": incident["incident_id"], "incident_record_id": record.record_id,
                "transaction_id": transaction_id, "status": transaction["current_state"],
                "idempotent": True, "recovery_required": True, "repair_complete": False}
    except (AttributeError, KeyError, TypeError, ValueError, RuntimeError, OSError, sqlite3.Error):
        return None


def _record_repair_attempt(runtime: Any, scope: ScopeRef, item: Mapping[str, Any]) -> str:
    from eimemory.models.records import RecordEnvelope
    from eimemory.governance.release.closure_verdict import report_digest

    payload = {key: item.get(key) for key in (
        "incident_id", "incident_record_id", "transaction_id", "status", "reason")}
    payload.update(report_type="system_code_repair_attempt", repair_complete=False)
    digest = report_digest(payload)
    record = RecordEnvelope.create(
        kind="reflection", scope=scope, title="System repair hand-off", source="eimemory.system_code_repair",
        summary=str(item.get("status") or "unknown"), content=payload,
        meta={"report_type": "system_code_repair_attempt", "attempt_digest": digest},
    )
    record.record_id = "repair-attempt-" + digest
    saved = runtime.store.append(record, existing_match=lambda old: (
        old.scope == scope and old.source == record.source and old.content == payload))
    return saved.record_id


def _automation_policy_incident_digest() -> str:
    """Return the sole incident authorized by an enabled machine policy."""

    return _automation_policy_identity()[0]


def _automation_policy_identity() -> tuple[str, str, str]:
    """Return exact incident, policy and Profile key from machine authority."""

    from eimemory.governance.evolution.code_automation_policy import (
        CODE_AUTOMATION_POLICY_DEFAULT_PATH,
        load_code_automation_policy,
    )

    loaded = load_code_automation_policy(path=CODE_AUTOMATION_POLICY_DEFAULT_PATH)
    policy = loaded if isinstance(loaded, Mapping) else {}
    incident = policy.get("incident") if isinstance(policy.get("incident"), Mapping) else {}
    digest = str(incident.get("incident_digest") or "").strip().lower()
    policy_digest = str(policy.get("policy_digest") or "").strip().lower()
    capability = policy.get("capability") if isinstance(policy.get("capability"), Mapping) else {}
    profile_key = str(capability.get("profile_key") or "").strip()
    if policy.get("ok") is not True or policy.get("status") != "enabled":
        return "", "", ""
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        return "", "", ""
    if len(policy_digest) != 64 or any(char not in "0123456789abcdef" for char in policy_digest):
        return "", "", ""
    return digest, policy_digest, profile_key


def _trusted_incident(record: Any, base_commit: str, *, diagnosis: bool = False) -> dict[str, Any] | None:
    if getattr(record, "status", None) != "active":
        return None
    return _trusted_incident_identity(record, base_commit, diagnosis=diagnosis)


def _trusted_incident_identity(record: Any, base_commit: str, *, diagnosis: bool = False) -> dict[str, Any] | None:
    """Validate detector identity before classifying its lifecycle state."""
    provenance = getattr(record, "provenance", {})
    meta = getattr(record, "meta", {})
    content = getattr(record, "content", {})
    if not isinstance(provenance, Mapping) or not isinstance(meta, Mapping) or not isinstance(content, Mapping):
        return None
    if (
        provenance.get("origin") != "system_detector"
        or provenance.get("known_before_detection") is not False
        or provenance.get("prior_user_reported") is not False
        or meta.get("observation_valid") is not True
    ):
        return None
    detector_report = content.get("detector_report")
    if (
        not isinstance(detector_report, Mapping)
        or detector_report.get("origin") != "system_detector"
        or detector_report.get("manual_bootstrap") is not False
        or detector_report.get("observation_valid") is not True
    ):
        return None
    incident = {field: content.get(field) for field in _INCIDENT_FIELDS}
    if (
        any(value is None for value in incident.values())
        or str(incident["incident_class"] or "") not in _ROUTES
        or str(meta.get("incident_digest") or "") != str(incident["incident_digest"] or "")
    ):
        return None
    source, _ = _ROUTES[incident["incident_class"]]
    if (getattr(record, "source", None) != source
            or provenance.get("detector") != source + ".v1"):
        return None
    if diagnosis and (detector_report.get("schema") != "release_closure_failure.v2"
                      or detector_report.get("status") != "diagnosis_required"):
        return None
    if incident["incident_class"] in {"release.closure_internal_failure", "release.closure_report_failure"}:
        if detector_report.get("release_commit") != base_commit:
            return None
        if detector_report.get("schema") == "release_closure_failure.v2":
            from eimemory.governance.release.closure_verdict import report_digest
            from dataclasses import asdict

            identity = detector_report.get("identity")
            if (detector_report.get("status") != ("diagnosis_required" if diagnosis else "failure_detected")
                    or detector_report.get("repair_eligible") is not (False if diagnosis else True)
                    or detector_report.get("detector") != "eimemory.release_closure_failure.v1"
                    or provenance.get("detector") != "eimemory.release_closure_failure.v1"
                    or not isinstance(identity, Mapping)
                    or identity.get("schema") != "release_closure_incident_identity.v2"
                    or identity.get("detector") != "eimemory.release_closure_failure.v1"
                    or identity.get("status") != detector_report.get("status")
                    or identity.get("scope") != asdict(record.scope)
                    or identity.get("incident_class") != incident["incident_class"]
                    or identity.get("release_commit") != base_commit
                    or identity.get("report_digest") != detector_report.get("report_digest")
                    or detector_report.get("incident") != incident
                    or report_digest(dict(identity)) != incident["incident_digest"]):
                return None
        elif incident["incident_class"] == "release.closure_report_failure":
            return None
    elif incident["incident_class"] == "deployment.runtime_commit_drift":
        from eimemory.ops.runtime_identity_drift import REPORT_SCHEMA, validate_runtime_identity_drift_report
        if (detector_report.get("expected_commit") != base_commit
                or detector_report.get("schema") != REPORT_SCHEMA
                or not validate_runtime_identity_drift_report(detector_report, scope=record.scope)
                or detector_report.get("incident") != incident
                or detector_report.get("identity_digest") != incident["incident_digest"]
                or getattr(record, "record_id", None) != incident["incident_id"]):
            return None
    elif incident["incident_class"] == SYSTEM_REPAIR_FAILURE_INCIDENT_CLASS:
        if (
            detector_report.get("detector") != SYSTEM_REPAIR_FAILURE_DETECTOR_ID
            or detector_report.get("release_commit") != base_commit
        ):
            return None
    return incident


def _repository_identity(root: Path) -> dict[str, Any]:
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip().lower()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"ok": False, "reason": "repository_identity_unavailable"}
    if status:
        return {"ok": False, "reason": "repository_worktree_not_clean"}
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        return {"ok": False, "reason": "repository_commit_invalid"}
    return {"ok": True, "base_commit": commit}


def _stable_id(prefix: str, *parts: str) -> str:
    material = "\0".join(str(part or "") for part in parts)
    return f"{prefix}-{sha256(material.encode('utf-8')).hexdigest()[:32]}"


__all__ = ["process_system_code_incidents"]
