"""Known, user-requested repairs through the existing strict effect owner.

Maintenance is production work, not evidence of autonomous discovery. This
entry point never accepts paths, commands, provider identity, or effect flags
from its caller, and never relabels a known issue as a system detector event.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
import re
import subprocess
from typing import Any

from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    load_code_automation_policy,
)
from eimemory.governance.code_evolution_bridge import propose_code_patch_v2
from eimemory.governance.code_evolution_repository import protected_paths_digest, remote_url_digest
from eimemory.governance.code_evolution_test_plans import (
    INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
    allowed_files_for_incident,
    protected_test_plan_digest,
)
from eimemory.governance.code_evolution_transaction import CodeEvolutionTransactionManager
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.code_evolution_store import CodeEvolutionStore, digest_json


SOURCE = "eimemory.code_maintenance"
DETECTOR_ID = "eimemory.code_maintenance.v1"
INCIDENT_CLASS = "code.incident_routing_stale"
SCHEMA = "code_maintenance.v1"
_PROVENANCE = dict(origin="user_reported", detector=DETECTOR_ID,
                   known_before_detection=True, prior_user_reported=True, manual_bootstrap=False)
_REQUIREMENTS = (
    "non_active_incidents_never_reach_provider",
    "detector_release_must_exactly_match_current_base",
    "current_active_incidents_keep_existing_strict_route",
    "one_shot_policy_and_transaction_uniqueness_preserved",
    "Preserve autonomous provenance and L5 qualification",
    "protected_focused_regression_and_full_suite_pass",
)
_REPORT_FIELDS = frozenset({"schema", "scope", "base_commit", "repository_root", "title", "summary", "evidence"})


def _result(ok: bool, status: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    return dict(ok=ok, status=status, reason=reason, **_PROVENANCE,
                qualifies_for_product_completion=False, **extra)


def _scope(scope: ScopeRef | Mapping[str, Any]) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(dict(scope))


def _report_valid(report: Any) -> bool:
    return bool(isinstance(report, dict) and set(report) == _REPORT_FIELDS
        and report.get("schema") == SCHEMA
        and isinstance(report.get("scope"), dict)
        and set(report["scope"]) == {"tenant_id", "agent_id", "workspace_id", "user_id"}
        and re.fullmatch(r"[0-9a-f]{40}", str(report.get("base_commit") or ""))
        and isinstance(report.get("repository_root"), str) and report["repository_root"]
        and isinstance(report.get("title"), str) and 0 < len(report["title"]) <= 512
        and isinstance(report.get("summary"), str) and 0 < len(report["summary"]) <= 4096
        and isinstance(report.get("evidence"), list) and 0 < len(report["evidence"]) <= 20
        and all(isinstance(value, str) and 0 < len(value) <= 2048 for value in report["evidence"]))


def _incident(report: dict[str, Any]) -> dict[str, Any]:
    digest = digest_json(dict(maintenance_report=report, provenance=_PROVENANCE, incident_class=INCIDENT_CLASS))
    return dict(incident_id=f"maintenance-{digest[:32]}", incident_digest=digest,
                incident_class=INCIDENT_CLASS, title=report["title"], summary=report["summary"],
                diagnostic_codes=["known_incident_routing_status_and_release_validation_failure"],
                acceptance_requirements=list(_REQUIREMENTS))


def record_code_maintenance(
    runtime: Any, *, scope: ScopeRef | Mapping[str, Any], base_commit: str,
    title: str, summary: str, evidence: Sequence[str], repo_root: str | Path = "/dev-project/eimemory",
) -> dict[str, Any]:
    """Record an explicitly known repair with an exact current base and proof.

    ``evidence`` is descriptive audit material, not executable authority.
    The caller supplies no incident class, source flags, paths, or test plan.
    """
    scope_ref = _scope(scope)
    context = _repository_context(runtime, scope_ref, Path(repo_root))
    if context.get("ok") is not True:
        return _result(False, "blocked", str(context.get("reason") or "repository_unavailable"))
    if base_commit != context["base_commit"] or not re.fullmatch(r"[0-9a-f]{40}", str(base_commit)):
        return _result(False, "blocked", "maintenance_base_commit_mismatch")
    report = dict(schema=SCHEMA, scope=asdict(scope_ref), base_commit=base_commit,
                  repository_root=context["repository_root"], title=title, summary=summary,
                  evidence=list(evidence) if isinstance(evidence, (list, tuple)) else None)
    if not _report_valid(report):
        return _result(False, "blocked", "maintenance_report_invalid")
    incident = _incident(report)
    existing = runtime.store.list_records_by_meta_value(kinds=["incident"], scope=scope_ref,
        meta_key="incident_digest", meta_value=incident["incident_digest"], limit=10) or []
    for record in existing:
        if _trusted_record(record, scope_ref, context) == incident:
            return _result(True, "recorded", record_id=record.record_id, incident=incident, idempotent=True)
    if existing:
        return _result(False, "blocked", "maintenance_record_already_closed_or_conflicted")
    record = runtime.store.append(RecordEnvelope.create(
        kind="incident", title=title, summary=summary, scope=scope_ref, source=SOURCE, status="active",
        content={**incident, "maintenance_report": report}, provenance=dict(_PROVENANCE),
        evidence=list(report["evidence"]),
        meta=dict(incident_digest=incident["incident_digest"], base_commit=base_commit,
                  incident_class=INCIDENT_CLASS, qualifies_for_product_completion=False),
    ))
    return _result(True, "recorded", record_id=record.record_id, incident=incident, idempotent=False)


def _trusted_record(record: Any, scope: ScopeRef, context: Mapping[str, Any]) -> dict[str, Any] | None:
    provenance = getattr(record, "provenance", None)
    if (record is None or getattr(record, "kind", "") != "incident"
            or getattr(record, "source", "") != SOURCE or getattr(record, "status", "") != "active"
            or not isinstance(provenance, dict) or provenance != _PROVENANCE
            or any(type(provenance.get(field)) is not bool
                   for field in ("known_before_detection", "prior_user_reported", "manual_bootstrap"))
            or getattr(record, "scope", None) != scope):
        return None
    content = record.content if isinstance(record.content, dict) else {}
    report = content.get("maintenance_report")
    if (not _report_valid(report) or report["scope"] != asdict(scope)
            or report["base_commit"] != context["base_commit"]
            or report["repository_root"] != context["repository_root"]):
        return None
    incident = _incident(report)
    if (set(content) != set(incident) | {"maintenance_report"}
            or any(content.get(key) != value for key, value in incident.items())
            or record.meta.get("incident_digest") != incident["incident_digest"]
            or record.meta.get("base_commit") != report["base_commit"]
            or record.meta.get("incident_class") != INCIDENT_CLASS
            or record.meta.get("qualifies_for_product_completion") is not False
            or record.evidence != report["evidence"]):
        return None
    return incident


def process_code_maintenance(
    runtime: Any, *, scope: ScopeRef | Mapping[str, Any], record_id: str,
    repo_root: str | Path = "/dev-project/eimemory",
) -> dict[str, Any]:
    """Submit only the named known issue, at most once, through strict effects."""
    from eimemory.capabilities.profiles import CapabilityProfiles, CapabilityProfileError

    scope_ref = _scope(scope)
    context = _repository_context(runtime, scope_ref, Path(repo_root))
    if context.get("ok") is not True:
        return _result(False, "blocked", str(context.get("reason") or "repository_unavailable"))
    record = runtime.store.get_by_id(str(record_id), scope=scope_ref)
    incident = _trusted_record(record, scope_ref, context)
    if incident is None:
        return _result(False, "blocked", "maintenance_record_not_current_and_trusted")
    policy = load_code_automation_policy(path=CODE_AUTOMATION_POLICY_DEFAULT_PATH)
    mismatch = _policy_error(policy, incident, context)
    if mismatch:
        return _result(False, "blocked", mismatch)
    ledger = CodeEvolutionStore(runtime.store)
    policy_digest = policy["policy_digest"]
    transaction_id = "maintenance-repair-" + digest_json(
        [record_id, incident["incident_digest"], context["base_commit"], policy_digest])[:32]
    existing = ledger.get_transaction(transaction_id)
    if existing is not None:
        return _result(True, "existing", transaction_id=transaction_id, transaction=existing, idempotent=True)
    consumption = ledger.get_policy_consumption(policy_digest)
    if consumption is not None:
        return _result(False, "blocked", "automation_policy_already_consumed",
                       transaction_id=str(consumption.get("transaction_id") or ""))
    blocker = _repository_blocker(ledger, context["repository_root"])
    if blocker is not None:
        return _result(False, "blocked", "repository_transaction_in_progress_or_quarantined",
                       transaction_id=str(blocker.get("transaction_id") or ""))
    profile_key = policy["capability"]["profile_key"]
    try:
        CapabilityProfiles(runtime.store).resolve(profile_key, runtime_scope=scope_ref, capability_scope="global")
    except CapabilityProfileError:
        return _result(False, "blocked", "automation_policy_profile_unavailable")
    allowed_files = allowed_files_for_incident(INCIDENT_CLASS, test_plan_id=INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID)
    proposal = propose_code_patch_v2(runtime, transaction_id=transaction_id,
        request_id=f"{transaction_id}-request", nonce=f"{transaction_id}-nonce", incident=incident,
        scope=asdict(scope_ref), profile_key=profile_key, repo_root=context["repository_root"],
        base_commit=context["base_commit"], base_tree_digest=context["base_tree_digest"],
        allowed_files=allowed_files, test_plan_id=INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
        test_plan_digest=protected_test_plan_digest(INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID),
        bounds=dict(maximum_files=len(allowed_files), maximum_bytes_per_file=48 * 1024,
                    maximum_total_bytes=96 * 1024, maximum_changed_lines=400), **_PROVENANCE)
    if proposal.get("ok") is not True:
        return _result(False, "proposal_blocked", str(proposal.get("reason") or "provider_unavailable"),
                       transaction_id=transaction_id)
    # Provider work can take minutes. Recheck current authority before creating
    # the sole durable transaction; the ledger then enforces uniqueness by CAS.
    refreshed = load_code_automation_policy(path=CODE_AUTOMATION_POLICY_DEFAULT_PATH)
    current = _repository_context(runtime, scope_ref, Path(repo_root))
    if (current != context or refreshed.get("policy_digest") != policy_digest
            or _policy_error(refreshed, incident, current)
            or _trusted_record(runtime.store.get_by_id(str(record_id), scope=scope_ref), scope_ref, current) != incident
            or ledger.get_policy_consumption(policy_digest) is not None
            or _repository_blocker(ledger, context["repository_root"]) is not None):
        return _result(False, "blocked", "maintenance_authority_changed_during_proposal", transaction_id=transaction_id)
    execution = CodeEvolutionTransactionManager(runtime, owner_id=f"maintenance:{record_id}").submit_proposal(
        proposal, scope=asdict(scope_ref), effects_enabled=True, apply=True)
    return _result(execution.get("ok") is True,
        "submitted" if execution.get("ok") is True else "transaction_blocked",
        str(execution.get("blocked_reason") or ""), transaction_id=transaction_id,
        transaction=execution.get("transaction") or {}, execution=execution)


def _policy_error(policy: Mapping[str, Any], incident: Mapping[str, Any], context: Mapping[str, Any]) -> str:
    from eimemory.adapters.hermes import code_implementation as provider

    if policy.get("ok") is not True or policy.get("status") != "enabled":
        return "automation_policy_unavailable"
    if not re.fullmatch(r"[0-9a-f]{64}", str(policy.get("policy_digest") or "")):
        return "automation_policy_digest_invalid"
    expected = dict(
        incident={"class": INCIDENT_CLASS, "detector_id": DETECTOR_ID, "incident_digest": incident["incident_digest"]},
        repository=dict(root=context["repository_root"], remote="origin", branch="master",
            base_commit=context["base_commit"], base_tree_digest=context["base_tree_digest"],
            remote_url_digest=context["remote_url_digest"]),
        capability=dict(capability_id=provider.CAPABILITY_ID, revision_id=provider.REVISION_ID,
            binding_id=provider.BINDING_ID, implementation_digest=provider.IMPLEMENTATION_DIGEST, operation=provider.OPERATION),
        verification=dict(test_plan_id=INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID,
            test_plan_digest=protected_test_plan_digest(INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID), full_suite_required=True),
    )
    for section, fields in expected.items():
        observed = policy.get(section)
        if not isinstance(observed, Mapping) or any(observed.get(key) != value for key, value in fields.items()):
            return f"maintenance_policy_{section}_mismatch"
    if not str(policy["capability"].get("profile_key") or "").strip():
        return "maintenance_policy_profile_missing"
    allowed = allowed_files_for_incident(INCIDENT_CLASS, test_plan_id=INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID)
    if not allowed or tuple((policy.get("patch") or {}).get("allowed_files") or ()) != allowed:
        return "maintenance_policy_allowed_files_mismatch"
    if not all((policy.get("effects") or {}).get(name) is True
               for name in ("commit", "push", "deployment", "rollback", "sedimentation")):
        return "maintenance_policy_effects_incomplete"
    return ""


def _repository_context(runtime: Any, scope: ScopeRef, root: Path) -> dict[str, Any]:
    from eimemory.governance.evidence_contract import current_release_identity
    from eimemory.governance.system_code_repair import _repository_identity

    root = root.expanduser().resolve()
    identity = _repository_identity(root)
    if identity.get("ok") is not True:
        return identity
    release = current_release_identity(runtime, scope)
    if release is None or release.commit != identity["base_commit"]:
        return dict(ok=False, reason="repository_release_identity_mismatch")
    try:
        branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root,
            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        paths = allowed_files_for_incident(INCIDENT_CLASS, test_plan_id=INCIDENT_ROUTING_REPAIR_TEST_PLAN_ID)
        if branch != "master" or not remote or not paths:
            return dict(ok=False, reason="maintenance_repository_coordinates_invalid")
        tree = protected_paths_digest(root, paths)
    except (OSError, subprocess.SubprocessError):
        return dict(ok=False, reason="maintenance_repository_identity_unavailable")
    return dict(ok=True, base_commit=identity["base_commit"], base_tree_digest=tree,
        remote_url_digest=remote_url_digest(remote), repository_root=str(root), repository_ref=branch)


def _repository_blocker(ledger: CodeEvolutionStore, root: str) -> dict[str, Any] | None:
    """Read the same repository lock predicate as create_transaction, uncapped."""
    def read():
        row = ledger.conn.execute(
            "SELECT t.transaction_id,t.current_state FROM code_evolution_transactions t "
            "WHERE t.repository_root=? AND t.repository_ref IN ('master','refs/heads/master') "
            "AND (t.terminal=0 OR (t.current_state='RECOVERY_QUARANTINED' AND NOT EXISTS ("
            "SELECT 1 FROM code_evolution_quarantine_resolutions r WHERE r.transaction_id=t.transaction_id))) "
            "ORDER BY t.created_at LIMIT 1", (root,)).fetchone()
        return dict(row) if row is not None else None
    return ledger._read(read)


__all__ = ["SOURCE", "DETECTOR_ID", "INCIDENT_CLASS", "record_code_maintenance", "process_code_maintenance"]
