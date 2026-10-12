"""Read-only, evidence-bound context for known capability-gap remediation.

The machine policy supplies write authority; a profile gap and a hypothesis
never do. The existing Hermes bridge owns provider resolution and attestation,
and promotion_manager remains the sole transaction/effect owner. Known gaps
are explicitly user-reported maintenance, not autonomous discovery evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
import os
import subprocess
from typing import Any

from eimemory.adapters.hermes import code_implementation as provider
from eimemory.governance.capability.capability_hypotheses import (
    hypothesis_behavior_gate,
    resolve_capability_hypothesis,
)
from eimemory.governance.evolution.code_automation_policy import (
    CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    CODE_AUTOMATION_POLICY_PATH_ENV,
    CODE_AUTOMATION_POLICY_SCHEMA_V2,
    load_code_automation_policy,
)
from eimemory.governance.evolution.code_evolution_repository import protected_paths_digest, remote_url_digest
from eimemory.governance.evolution.code_evolution_test_plans import (
    DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID,
    path_allowed_for_incident,
    protected_test_plan,
    protected_test_plan_digest,
)
from eimemory.models.records import ScopeRef
from eimemory.storage.code_evolution_store import CodeEvolutionStore, digest_json


SCHEMA = "dynamic_capability_patch_context.v1"
INCIDENT_CLASS = "capability.gap_maintenance"
DETECTOR_ID = "eimemory.dynamic_capability_gap_maintenance.v1"
_PROVENANCE = dict(origin="user_reported", detector=DETECTOR_ID,
    known_before_detection=True, prior_user_reported=True, manual_bootstrap=False)
_MAX_CASES = 128


def _blocked(reason: str, **extra: Any) -> dict[str, Any]:
    return dict(ok=False, status="blocked", reason=reason, schema=SCHEMA,
        qualifies_for_product_completion=False, **extra)


def _load_policy() -> dict[str, Any]:
    return load_code_automation_policy(
        path=os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    )


def _machine_authority_digest(policy: Mapping[str, Any]) -> str:
    """Bind permission contents without making a later exact incident pin cyclic.

    A hypothesis must exist before its independently attested incident can be
    pinned. The authority hash omits only that optional pin. The final proposal
    and transaction still bind the complete, newly read policy_digest.
    """
    incident = policy["incident"]
    material = {key: policy[key] for key in ("schema_version", "policy_id", "not_before", "expires_at",
        "capability", "repository", "patch", "verification", "effects", "deployment")}
    material["incident"] = {key: value for key, value in incident.items() if key != "incident_digest"}
    return digest_json(material)


def dynamic_patch_machine_policy(item: Mapping[str, Any]) -> dict[str, Any]:
    """Expose a bounded v2 summary for the dedicated strict provider route."""
    policy = _load_policy()
    error = _provider_policy_error(policy, item)
    if error:
        return _blocked(error)
    effects = policy["effects"]
    return dict(ok=True, status="enabled", reason="", declared=True,
        schema_version=CODE_AUTOMATION_POLICY_SCHEMA_V2, source="machine_file",
        policy_id=policy["policy_id"], policy_digest=policy["policy_digest"],
        actions=dict(local_apply=True, commit=effects["commit"] and effects["push"],
            deployment=effects["deployment"]))


def dynamic_patch_candidate_bounds(runtime: Any, *, runtime_scope: ScopeRef | Mapping[str, Any],
    profile_key: str, capability_id: str, capability_revision_id: str,
    provider_binding_ids: Sequence[str], capability_scope: str = "global") -> dict[str, Any]:
    """Intersect live immutable provider capacity, protected plan and machine authority.

    Called before a new hypothesis is persisted. It never rewrites an existing
    zero-write hypothesis and does not confuse a provider's capacity with a
    permission to write. Nonmatching/missing authority remains evaluation-only.
    """
    from eimemory.capabilities.registry import exact_runtime_scope
    from eimemory.capabilities.code_implementation_bootstrap import code_implementation_revision
    from eimemory.core.clock import now_iso

    if (capability_id != provider.CAPABILITY_ID or capability_revision_id != provider.REVISION_ID
            or list(provider_binding_ids) != [provider.BINDING_ID] or capability_scope != "global"):
        return _blocked("dynamic_patch_target_not_supported_by_hermes_v2")
    scope = exact_runtime_scope(runtime_scope)
    try:
        resolution = runtime.capabilities.resolve(capability_id, runtime_scope=scope,
            capability_scope=capability_scope, revision_id=capability_revision_id,
            binding_id=provider.BINDING_ID, at_time=now_iso())
    except Exception:
        return _blocked("dynamic_patch_registry_unavailable")
    if resolution.ok is not True or len(resolution.revisions) != 1 or len(resolution.bindings) != 1:
        return _blocked("dynamic_patch_registry_unavailable")
    revision = resolution.revisions[0]
    revision = revision.get("descriptor") if isinstance(revision.get("descriptor"), Mapping) else revision
    binding = resolution.bindings[0]
    binding = binding.get("descriptor") if isinstance(binding.get("descriptor"), Mapping) else binding
    expected_revision = code_implementation_revision()
    if (revision.get("contract_digest") != expected_revision.contract_digest
            or revision.get("contract") != expected_revision.to_dict()["contract"]
            or binding.get("binding_id") != provider.BINDING_ID
            or binding.get("implementation_digest") != provider.IMPLEMENTATION_DIGEST
            or binding.get("capability_revision_id") != provider.REVISION_ID
            or binding.get("provider_kind") != provider.PROVIDER_KIND
            or binding.get("provider_instance_id") != provider.PROVIDER_INSTANCE_ID
            or binding.get("operations") != [provider.OPERATION]):
        return _blocked("dynamic_patch_registry_contract_mismatch")
    item = dict(profile_key=profile_key, capability_id=capability_id,
        capability_revision_id=capability_revision_id, provider_binding_id=provider.BINDING_ID)
    policy = _load_policy()
    error = _provider_policy_error(policy, item)
    if error:
        return _blocked(error)
    patch = policy["patch"]
    verification = policy["verification"]
    files = patch["allowed_files"]
    if (not files or verification != dict(test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID,
            test_plan_digest=protected_test_plan_digest(DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID), full_suite_required=True)
            or any(not path_allowed_for_incident(INCIDENT_CLASS, path,
                test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID) for path in files)):
        return _blocked("dynamic_patch_policy_allowed_files_not_protected")
    bounds = dict(side_effect_class="bounded_write", max_changes=min(len(files), patch["max_files"], provider.MAX_ALLOWED_FILES),
        allowed_file_paths=sorted(files), maximum_bytes_per_file=min(patch["max_file_bytes"], provider.MAX_FILE_BYTES),
        maximum_total_bytes=min(patch["max_total_bytes"], provider.MAX_TOTAL_BYTES),
        maximum_changed_lines=min(patch["max_changed_lines"], provider.MAX_CHANGED_LINES),
        test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID,
        test_plan_digest=protected_test_plan_digest(DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID),
        registry_contract_digest=expected_revision.contract_digest,
        provider_binding_id=provider.BINDING_ID, implementation_digest=provider.IMPLEMENTATION_DIGEST,
        machine_authority_digest=_machine_authority_digest(policy))
    return dict(ok=True, status="bounded_write_candidate", reason="", candidate_bounds=bounds,
        qualifies_for_product_completion=False)


def _provider_policy_error(policy: Mapping[str, Any], item: Mapping[str, Any]) -> str:
    if (policy.get("ok") is not True or policy.get("status") != "enabled"
            or policy.get("schema_version") != CODE_AUTOMATION_POLICY_SCHEMA_V2):
        return str(policy.get("reason") or "dynamic_patch_v2_machine_policy_unavailable")
    capability = policy.get("capability") if isinstance(policy.get("capability"), Mapping) else {}
    expected = dict(profile_key=str(item.get("profile_key") or ""), capability_id=provider.CAPABILITY_ID,
        revision_id=provider.REVISION_ID, binding_id=provider.BINDING_ID,
        implementation_digest=provider.IMPLEMENTATION_DIGEST, operation=provider.OPERATION)
    if any(capability.get(key) != value for key, value in expected.items()):
        return "dynamic_patch_policy_provider_mismatch"
    if (str(item.get("capability_id") or "") != provider.CAPABILITY_ID
            or str(item.get("capability_revision_id") or "") != provider.REVISION_ID
            or str(item.get("provider_binding_id") or "") != provider.BINDING_ID):
        return "dynamic_patch_target_not_supported_by_hermes_v2"
    incident = policy.get("incident") if isinstance(policy.get("incident"), Mapping) else {}
    if incident.get("class") != INCIDENT_CLASS or incident.get("detector_id") != DETECTOR_ID:
        return "dynamic_patch_policy_incident_mismatch"
    return ""


def _evidence_context(runtime: Any, scope: ScopeRef, item: Mapping[str, Any], gate: Mapping[str, Any]) -> dict[str, Any]:
    """Reload the exact hypothesis and independent suite, never trust a passed dict."""
    from eimemory.governance.capability.hypothesis_producer import revoked_hypothesis_ids

    try:
        if str(item.get("hypothesis_id") or "") in revoked_hypothesis_ids(runtime, runtime_scope=scope, strict=True):
            return _blocked("dynamic_patch_hypothesis_revoked")
        hypothesis = resolve_capability_hypothesis(runtime, runtime_scope=scope,
            hypothesis_id=str(item.get("hypothesis_id") or ""),
            capability_id=str(item.get("capability_id") or ""),
            capability_revision_id=str(item.get("capability_revision_id") or ""),
            capability_scope=str(item.get("capability_scope") or ""))
        current = hypothesis_behavior_gate(runtime, runtime_scope=scope, hypothesis_id=hypothesis.record_id)
    except Exception:
        return _blocked("dynamic_patch_hypothesis_evidence_unavailable")
    if current.get("allowed") is not True or dict(current) != dict(gate):
        return _blocked("dynamic_patch_hypothesis_evidence_changed")
    payload = hypothesis.content
    expected = dict(link_id=item.get("hypothesis_link_id"), link_digest=item.get("hypothesis_link_digest"),
        candidate_bounds=item.get("candidate_bounds"), expected_metric=item.get("expected_metric"))
    if any(payload.get(key) != value for key, value in expected.items()):
        return _blocked("dynamic_patch_hypothesis_context_mismatch")
    bounds = payload.get("candidate_bounds") if isinstance(payload.get("candidate_bounds"), Mapping) else {}
    max_changes = bounds.get("max_changes")
    if isinstance(max_changes, bool) or not isinstance(max_changes, int) or max_changes < 1:
        return _blocked("dynamic_patch_candidate_bounds_forbid_changes")
    if (bounds.get("test_plan_id") != DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID
            or bounds.get("test_plan_digest") != protected_test_plan_digest(DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID)
            or bounds.get("provider_binding_id") != item.get("provider_binding_id")
            or bounds.get("implementation_digest") != provider.IMPLEMENTATION_DIGEST):
        return _blocked("dynamic_patch_candidate_bounds_not_protected")
    feedback_id = str(current.get("qualifying_feedback_id") or "")
    feedback = runtime.store.get_by_id(feedback_id, scope=scope)
    if feedback is None or feedback.scope != scope:
        return _blocked("dynamic_patch_independent_suite_missing")
    details = feedback.content.get("details") if isinstance(feedback.content, Mapping) else {}
    details = details if isinstance(details, Mapping) else {}
    if (details.get("work_item_id") != item.get("work_item_id")
            or details.get("evaluation_suite") is not True):
        return _blocked("dynamic_patch_independent_suite_mismatch")
    raw_cases = item.get("evaluation_cases")
    raw_cases = raw_cases if isinstance(raw_cases, Sequence) and not isinstance(raw_cases, (str, bytes)) else ()
    expected_cases = sorted(str((case.get("artifact") or {}).get("case_id") or "")
        for case in raw_cases if isinstance(case, Mapping))
    ids = details.get("evaluation_artifact_ids")
    if (not expected_cases or len(expected_cases) > _MAX_CASES or not all(expected_cases)
            or len(set(expected_cases)) != len(expected_cases)
            or sorted(details.get("case_ids") or ()) != expected_cases
            or details.get("case_verdicts") != ["pass"] * len(expected_cases)
            or not isinstance(ids, list) or len(ids) != len(expected_cases) or len(ids) != len(set(ids))):
        return _blocked("dynamic_patch_independent_suite_incomplete")
    evidence = []
    for artifact_id in ids:
        artifact = runtime.store.get_by_id(str(artifact_id), scope=scope)
        if artifact is None or artifact.scope != scope or artifact.status != "active":
            return _blocked("dynamic_patch_independent_artifact_missing")
        content = artifact.content if isinstance(artifact.content, Mapping) else {}
        context = content.get("capability_hypothesis") if isinstance(content.get("capability_hypothesis"), Mapping) else {}
        if (artifact.source != "eimemory.governance.capability_hypotheses.evaluation_bridge"
                or content.get("report_type") != "capability_hypothesis_evaluation"
                or content.get("verdict") != "pass" or context.get("hypothesis_id") != hypothesis.record_id
                or content.get("provider_binding_id") != item.get("provider_binding_id")
                or not str(content.get("evaluation_run_id") or "")
                or not str(content.get("evaluation_observation_id") or "")
                or content.get("case_id") not in expected_cases):
            return _blocked("dynamic_patch_independent_artifact_invalid")
        # The feedback gate validates its anchor. Revalidate every other suite
        # artifact through the same existing independent-trace validator.
        from eimemory.governance.capability.capability_hypotheses import _validate_artifact
        valid, _reasons = _validate_artifact("evaluation", artifact, hypothesis, expected_verdict="pass")
        if not valid:
            return _blocked("dynamic_patch_independent_artifact_invalid")
        trace = runtime.store.get_by_id(str(content.get("trace_record_id") or ""), scope=scope)
        probe = runtime.store.get_by_id(str(content.get("probe_id") or ""), scope=scope)
        if (trace is None or trace.scope != scope or probe is None or probe.scope != scope
                or probe.status != "active"):
            return _blocked("dynamic_patch_independent_trace_missing")
        from eimemory.governance.capability.capability_hypotheses import _validate_independent_evaluation_trace
        try:
            _validate_independent_evaluation_trace(trace, probe_id=probe.record_id,
                capability_id=str(item["capability_id"]), capability_revision_id=str(item["capability_revision_id"]),
                provider_binding_id=str(item["provider_binding_id"]), execution_digest=str(content["execution_digest"]))
        except Exception:
            return _blocked("dynamic_patch_independent_trace_invalid")
        evidence.append(dict(case_id=content["case_id"], artifact_id=artifact.record_id,
            execution_digest=content["execution_digest"], probe_id=content["probe_id"],
            trace_record_id=content["trace_record_id"], evaluation_run_id=content["evaluation_run_id"],
            evaluation_observation_id=content["evaluation_observation_id"]))
    if sorted(value["case_id"] for value in evidence) != expected_cases:
        return _blocked("dynamic_patch_independent_suite_incomplete")
    return dict(ok=True, hypothesis_id=hypothesis.record_id, link_id=payload["link_id"],
        link_digest=payload["link_digest"], qualifying_feedback_id=feedback_id,
        evidence=evidence, candidate_bounds=dict(bounds), expected_metric=dict(payload["expected_metric"]),
        statement=str(payload.get("statement") or ""))


def _repository_context(runtime: Any, scope: ScopeRef, policy: Mapping[str, Any]) -> dict[str, Any]:
    from eimemory.config.trusted import trusted_repository_root
    from eimemory.governance.evolution.system_code_repair import _repository_identity
    from eimemory.governance.release.evidence_contract import current_release_identity

    try:
        root = trusted_repository_root()
    except Exception:
        return _blocked("dynamic_patch_trusted_repository_unavailable")
    identity = _repository_identity(root)
    if identity.get("ok") is not True:
        return _blocked(str(identity.get("reason") or "dynamic_patch_repository_unavailable"))
    release = current_release_identity(runtime, scope)
    if release is None or release.commit != identity["base_commit"]:
        return _blocked("repository_release_identity_mismatch")
    repository = policy["repository"]
    patch = policy["patch"]
    files = patch["allowed_files"]
    if not files or any(not path_allowed_for_incident(INCIDENT_CLASS, path,
            test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID) for path in files):
        return _blocked("dynamic_patch_policy_allowed_files_not_protected")
    try:
        branch = subprocess.run(["git", "symbolic-ref", "--short", "HEAD"], cwd=root,
            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=root,
            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        tree = protected_paths_digest(root, files)
    except (OSError, subprocess.SubprocessError):
        return _blocked("dynamic_patch_repository_coordinates_unavailable")
    expected = dict(root=str(root), remote="origin", branch=branch, base_commit=identity["base_commit"],
        base_tree_digest=tree, remote_url_digest=remote_url_digest(remote))
    if not remote or any(repository.get(key) != value for key, value in expected.items()):
        return _blocked("dynamic_patch_policy_repository_mismatch")
    plan = protected_test_plan(DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID)
    verification = policy.get("verification")
    expected_verification = dict(test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID,
        test_plan_digest=protected_test_plan_digest(DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID), full_suite_required=True)
    if plan is None or verification != expected_verification:
        return _blocked("dynamic_patch_policy_test_plan_mismatch")
    return dict(ok=True, repository_root=str(root), repository_ref=branch,
        base_commit=identity["base_commit"], base_tree_digest=tree,
        remote_url_digest=expected["remote_url_digest"], allowed_files=list(files))


def build_dynamic_patch_context(runtime: Any, *, scope: ScopeRef, item: Mapping[str, Any],
    gate: Mapping[str, Any]) -> dict[str, Any]:
    """Build proposal coordinates without creating a transaction or external effect."""
    policy = _load_policy()
    error = _provider_policy_error(policy, item)
    if error:
        return _blocked(error)
    evidence = _evidence_context(runtime, scope, item, gate)
    if evidence.get("ok") is not True:
        return evidence
    repository = _repository_context(runtime, scope, policy)
    if repository.get("ok") is not True:
        return repository
    candidate_bounds = evidence["candidate_bounds"]
    if (candidate_bounds.get("machine_authority_digest") != _machine_authority_digest(policy)
            or sorted(candidate_bounds.get("allowed_file_paths") or ()) != sorted(repository["allowed_files"])):
        return _blocked("dynamic_patch_candidate_policy_bounds_changed")
    material = dict(schema=SCHEMA, scope=asdict(scope), work_item_id=str(item.get("work_item_id") or ""),
        profile_key=str(item.get("profile_key") or ""), capability_id=str(item.get("capability_id") or ""),
        capability_revision_id=str(item.get("capability_revision_id") or ""),
        provider_binding_id=str(item.get("provider_binding_id") or ""),
        base_commit=repository["base_commit"], evidence=evidence, provenance=_PROVENANCE)
    incident_digest = digest_json(material)
    incident = dict(incident_id="dynamic-gap-" + incident_digest[:32], incident_digest=incident_digest,
        incident_class=INCIDENT_CLASS, title="Known capability gap maintenance",
        summary=("Known profile gap. Hypothesis: " + evidence["statement"][:2500]
            + "\nExpected metric: " + str(evidence["expected_metric"].get("metric") or "profile gap closure")[:500]
            + "\nPreserve protected evidence and verification gates."),
        # Exact opaque references live in the digest-bound structured owner
        # context. Putting them into model-facing prose can trip the existing
        # high-entropy secret detector, so free text carries fixed codes only.
        diagnostic_codes=["known_profile_gap", "independent_hypothesis_suite_verified"],
        acceptance_requirements=["independent_suite_provenance_preserved", "protected_focused_regression_and_full_suite_pass",
            "scope_permissions_and_release_identity_preserved", "known_maintenance_does_not_certify_autonomous_discovery"])
    required_digest = (policy.get("incident") or {}).get("incident_digest")
    if required_digest and required_digest != incident_digest:
        return _blocked("dynamic_patch_policy_incident_digest_mismatch")
    ledger = CodeEvolutionStore(runtime.store)
    consumed = ledger.get_policy_consumption(policy["policy_digest"])
    if consumed is not None:
        return _blocked("automation_policy_already_consumed", transaction_id=str(consumed.get("transaction_id") or ""))
    from eimemory.governance.evolution.code_maintenance import _repository_blocker
    blocker = _repository_blocker(ledger, repository["repository_root"])
    if blocker is not None:
        return _blocked("repository_transaction_in_progress_or_quarantined", transaction_id=str(blocker.get("transaction_id") or ""))
    maximum_files = min(policy["patch"]["max_files"], evidence["candidate_bounds"]["max_changes"])
    bounds = dict(maximum_files=maximum_files,
        maximum_bytes_per_file=min(policy["patch"]["max_file_bytes"], candidate_bounds["maximum_bytes_per_file"]),
        maximum_total_bytes=min(policy["patch"]["max_total_bytes"], candidate_bounds["maximum_total_bytes"]),
        maximum_changed_lines=min(policy["patch"]["max_changed_lines"], candidate_bounds["maximum_changed_lines"]))
    transaction_id = "dynamic-gap-" + digest_json([incident_digest, repository["base_commit"], policy["policy_digest"]])[:32]
    existing = ledger.get_transaction(transaction_id)
    if existing is not None:
        return _blocked("dynamic_patch_transaction_already_exists", transaction_id=transaction_id)
    return dict(ok=True, status="context_ready", schema=SCHEMA, incident=incident, evidence=evidence,
        repository=repository, bounds=bounds, policy_digest=policy["policy_digest"],
        profile_key=item["profile_key"], transaction_id=transaction_id,
        request_id=transaction_id + "-request", nonce=transaction_id + "-nonce",
        qualifies_for_product_completion=False, **_PROVENANCE)


def dynamic_patch_proposal_authority_error(runtime: Any, proposal: Mapping[str, Any],
    *, scope: ScopeRef) -> str:
    """Revalidate gap authority at forward-effect and recovery boundaries.

    This is deliberately independent of one-shot policy consumption and live
    repository HEAD: the ordinary effect owner validates those coordinates and
    must be able to reconcile its committed candidate. Revoked hypotheses can
    stop further forward effects; they cannot obstruct rollback.
    """
    incident = proposal.get("incident") if isinstance(proposal.get("incident"), Mapping) else {}
    if incident.get("incident_class") != INCIDENT_CLASS:
        return ""
    context = proposal.get("dynamic_capability_context")
    if not isinstance(context, Mapping):
        return "dynamic_patch_durable_context_missing"
    required = {"schema", "item", "gate", "evidence_digest", "scope", "base_commit", "policy_digest", "context_digest"}
    if set(context) != required or context.get("schema") != SCHEMA or context.get("scope") != asdict(scope):
        return "dynamic_patch_durable_context_invalid"
    material = {key: value for key, value in context.items() if key != "context_digest"}
    if context.get("context_digest") != digest_json(material):
        return "dynamic_patch_durable_context_digest_mismatch"
    if not isinstance(context.get("item"), Mapping) or not isinstance(context.get("gate"), Mapping):
        return "dynamic_patch_durable_context_invalid"
    if any(proposal.get(key) != value for key, value in _PROVENANCE.items()):
        return "dynamic_patch_known_maintenance_provenance_required"
    repository = proposal.get("repository") if isinstance(proposal.get("repository"), Mapping) else {}
    if context.get("base_commit") != repository.get("base_commit"):
        return "dynamic_patch_durable_base_mismatch"
    item = context["item"]
    evidence = _evidence_context(runtime, scope, item, context["gate"])
    if evidence.get("ok") is not True:
        return str(evidence.get("reason") or "dynamic_patch_hypothesis_evidence_unavailable")
    if context["evidence_digest"] != digest_json(evidence):
        return "dynamic_patch_durable_evidence_changed"
    incident_material = dict(schema=SCHEMA, scope=asdict(scope), work_item_id=str(item.get("work_item_id") or ""),
        profile_key=str(item.get("profile_key") or ""), capability_id=str(item.get("capability_id") or ""),
        capability_revision_id=str(item.get("capability_revision_id") or ""),
        provider_binding_id=str(item.get("provider_binding_id") or ""),
        base_commit=context["base_commit"], evidence=evidence, provenance=_PROVENANCE)
    if (incident.get("incident_digest") != digest_json(incident_material)
            or proposal.get("profile_key") != item.get("profile_key")):
        return "dynamic_patch_durable_incident_mismatch"
    return ""


def propose_dynamic_patch(runtime: Any, *, scope: ScopeRef, item: Mapping[str, Any],
    gate: Mapping[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Call only the official provider bridge, then recheck proposal-time authority."""
    from eimemory.governance.evolution.code_evolution_bridge import propose_code_patch_v2
    context = build_dynamic_patch_context(runtime, scope=scope, item=item, gate=gate)
    if context.get("ok") is not True:
        return None, context
    repository = context["repository"]
    try:
        proposal = propose_code_patch_v2(runtime, transaction_id=context["transaction_id"],
            request_id=context["request_id"], nonce=context["nonce"], incident=context["incident"],
            scope=asdict(scope), capability_scope=str(item["capability_scope"]), profile_key=context["profile_key"],
            repo_root=repository["repository_root"], repository_ref=repository["repository_ref"],
            base_commit=repository["base_commit"], base_tree_digest=repository["base_tree_digest"],
            allowed_files=repository["allowed_files"], test_plan_id=DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID,
            test_plan_digest=protected_test_plan_digest(DYNAMIC_CAPABILITY_GAP_TEST_PLAN_ID),
            bounds=context["bounds"], **_PROVENANCE)
    except Exception:
        return None, _blocked("dynamic_patch_provider_request_failed")
    if not isinstance(proposal, Mapping) or proposal.get("ok") is not True:
        return None, _blocked(str(proposal.get("reason") or "dynamic_patch_provider_unavailable")
            if isinstance(proposal, Mapping) else "dynamic_patch_provider_invalid_result", transaction_id=context["transaction_id"])
    refreshed = build_dynamic_patch_context(runtime, scope=scope, item=item, gate=gate)
    if refreshed != context:
        return None, _blocked("dynamic_patch_authority_changed_during_proposal", transaction_id=context["transaction_id"])
    item_keys = ("work_item_id", "profile_key", "capability_id", "capability_revision_id", "provider_binding_id",
        "capability_scope", "hypothesis_id", "hypothesis_link_id", "hypothesis_link_digest", "expected_metric", "candidate_bounds")
    durable_item = {key: item[key] for key in item_keys}
    durable_item["evaluation_cases"] = [{"artifact": {"case_id": value["case_id"]}}
        for value in context["evidence"]["evidence"]]
    durable_context = dict(schema=SCHEMA, item=durable_item, gate=dict(gate),
        evidence_digest=digest_json(context["evidence"]), scope=asdict(scope), base_commit=repository["base_commit"],
        policy_digest=context["policy_digest"])
    durable_context["context_digest"] = digest_json(durable_context)
    proposal = {**dict(proposal), "dynamic_capability_context": durable_context}
    authority_error = dynamic_patch_proposal_authority_error(runtime, proposal, scope=scope)
    if authority_error:
        return None, _blocked(authority_error, transaction_id=context["transaction_id"])
    return dict(code_evolution_proposal=proposal, summary="Known capability-gap remediation proposal"), dict(
        ok=True, status="proposal_ready", schema=SCHEMA, transaction_id=context["transaction_id"],
        incident_id=context["incident"]["incident_id"], hypothesis_id=context["evidence"]["hypothesis_id"],
        qualifying_feedback_id=context["evidence"]["qualifying_feedback_id"],
        evaluation_artifact_ids=[value["artifact_id"] for value in context["evidence"]["evidence"]],
        transaction_owner="promotion_manager", qualifies_for_product_completion=False,
        **_PROVENANCE)
