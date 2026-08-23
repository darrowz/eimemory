from __future__ import annotations

from collections.abc import Mapping, Sequence
import difflib
import fnmatch
import json
import os
import stat
import subprocess
from hashlib import sha256
from pathlib import Path, PurePosixPath
from typing import Any

from eimemory.governance.code_evolution import run_code_sandbox
from eimemory.governance.code_automation_policy import (
    code_automation_policy_summary,
    load_code_automation_policy,
    machine_policy_context_from_mapping,
)
from eimemory.governance.code_patch_command_policy import code_patch_verification_command_error
from eimemory.core.clock import now_iso


CODE_PATCH_PROPOSAL_REPORT_TYPE = "code_patch_proposal"
CODE_PATCH_PROPOSAL_SCHEMA_VERSION = "code_patch_proposal.v3"
CODE_IMPLEMENTATION_PROPOSAL_SCHEMA_VERSION = "code_implementation_proposal.v2"


def propose_code_patch_v2(
    runtime: Any,
    *,
    transaction_id: str,
    request_id: str,
    nonce: str,
    incident: Mapping[str, Any],
    scope: Mapping[str, Any],
    capability_scope: str = "global",
    repo_root: str | Path = "/dev-project/eimemory",
    base_commit: str,
    base_tree_digest: str,
    allowed_files: Sequence[str],
    test_plan_id: str,
    test_plan_digest: str,
    bounds: Mapping[str, Any],
    origin: str = "manual_bootstrap",
    detector: str = "",
    known_before_detection: bool = True,
    prior_user_reported: bool = True,
    manual_bootstrap: bool = True,
    repository_ref: str = "master",
) -> dict[str, Any]:
    """Request a strict v2 proposal from the exact live Hermes provider.

    This path is intentionally independent of the effect policy.  It creates
    no worktree, executes no command, and never accepts a model-supplied
    command, path, environment, or secret.  Provider resolution always uses
    the active registry/binding/advertisement path; there is no environment
    switch or injectable fallback in this production API.
    """

    from eimemory.adapters.hermes.code_implementation import (
        BINDING_ID,
        CAPABILITY_ID,
        IMPLEMENTATION_DIGEST,
        CodeImplementationError,
        build_request,
        canonical_json,
        resolve_code_implementation_provider,
        validate_attestation,
        validate_response,
    )
    from eimemory.governance.code_evolution_test_plans import protected_test_plan, protected_test_plan_digest

    plan = protected_test_plan(test_plan_id)
    base_report = {
        "ok": False,
        "report_type": "code_implementation_proposal",
        "schema_version": CODE_IMPLEMENTATION_PROPOSAL_SCHEMA_VERSION,
        "proposal_only": True,
        "qualifying": False,
        "capability_id": CAPABILITY_ID,
        "binding_id": BINDING_ID,
        "transaction_id": str(transaction_id or ""),
        "request_id": str(request_id or ""),
    }
    if plan is None:
        return {**base_report, "status": "blocked", "reason": "test_plan_not_registered"}
    if str(test_plan_digest or "") != protected_test_plan_digest(test_plan_id):
        return {**base_report, "status": "blocked", "reason": "test_plan_digest_mismatch"}
    if not isinstance(allowed_files, Sequence) or isinstance(allowed_files, (str, bytes)):
        return {**base_report, "status": "blocked", "reason": "allowed_files_invalid"}
    normalized_paths = [str(item).replace("\\", "/") for item in allowed_files]
    if tuple(normalized_paths) != tuple(plan.allowed_files):
        return {**base_report, "status": "blocked", "reason": "allowed_files_not_protected"}
    root = Path(repo_root)
    source_files: list[dict[str, str]] = []
    try:
        for relative in normalized_paths:
            path = root / relative
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                return {**base_report, "status": "blocked", "reason": "source_file_not_regular"}
            content_bytes = path.read_bytes().replace(b"\r\n", b"\n")
            content = content_bytes.decode("utf-8")
            source_files.append({"path": relative, "sha256": sha256(content_bytes).hexdigest(), "content": content})
    except (OSError, UnicodeError):
        return {**base_report, "status": "blocked", "reason": "source_file_unavailable"}
    if not isinstance(incident, Mapping):
        return {**base_report, "status": "blocked", "reason": "incident_invalid"}
    computed_tree_digest = sha256(
        canonical_json(
            [{"path": item["path"], "sha256": item["sha256"]} for item in source_files]
        ).encode("utf-8")
    ).hexdigest()
    if str(base_tree_digest or "") != computed_tree_digest:
        return {**base_report, "status": "blocked", "reason": "base_tree_digest_mismatch"}
    required_incident = {"incident_id", "incident_digest", "incident_class", "title", "summary", "diagnostic_codes", "acceptance_requirements"}
    if set(incident) != required_incident:
        return {**base_report, "status": "blocked", "reason": "incident_fields_invalid"}
    try:
        request = build_request(
            transaction_id=transaction_id,
            request_id=request_id,
            nonce=nonce,
            incident=incident,
            base={"commit": base_commit, "tree_digest": base_tree_digest},
            allowed_files=source_files,
            bounds=bounds,
            test_plan_id=test_plan_id,
            test_plan_digest=test_plan_digest,
        )
    except CodeImplementationError as exc:
        return {**base_report, "status": "blocked", "reason": str(exc)}

    provider_info = resolve_code_implementation_provider(
        runtime,
        runtime_scope=scope,
        capability_scope=capability_scope,
        checked_at=now_iso(),
        probe=True,
    )
    if provider_info.get("ok") is not True or not callable(getattr(provider_info.get("provider"), "propose_patch_v2", None)):
        return {**base_report, "status": "blocked", "reason": str(provider_info.get("reason") or "provider_unavailable"), "provider": {key: value for key, value in provider_info.items() if key not in {"provider", "resolution"}}}
    provider = provider_info["provider"]
    try:
        raw = provider.propose_patch_v2(request)
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"ok", "operation", "attestation", "response"}
            or raw.get("ok") is not True
            or raw.get("operation") != "propose_patch_v2"
        ):
            return {**base_report, "status": "blocked", "reason": "provider_envelope_invalid"}
        attestation = raw.get("attestation") if isinstance(raw, Mapping) and isinstance(raw.get("attestation"), Mapping) else None
        response = raw.get("response") if isinstance(raw, Mapping) and isinstance(raw.get("response"), Mapping) else raw
        normalized_response = validate_response(response, request=request)
        if attestation is None:
            return {**base_report, "status": "blocked", "reason": "provider_attestation_missing"}
        validate_attestation(
            attestation,
            request=request,
            response=normalized_response,
        )
    except Exception as exc:
        return {**base_report, "status": "blocked", "reason": f"provider_response_invalid:{type(exc).__name__}"}
    proposal_digest = sha256(canonical_json(normalized_response).encode("utf-8")).hexdigest()
    repository_root = str(Path(repo_root).expanduser().resolve())
    return {
        **base_report,
        "ok": True,
        "status": "proposal_ready",
        "proposal_digest": proposal_digest,
        "request_digest": request["request_digest"],
        "response": normalized_response,
        "file_updates": list(normalized_response["file_updates"]),
        "implementation_digest": str(provider_info.get("implementation_digest") or IMPLEMENTATION_DIGEST),
        "provider_instance_id": str(provider_info.get("provider_instance_id") or "hermes.eimemory.code-implementation.production"),
        "qualifying": True,
        # These are normalized transaction coordinates, not provider or model
        # authority.  Keeping them beside the proposal lets the existing
        # promotion owner submit the result without reconstructing incident,
        # repository, or binding facts from untrusted response text.
        "origin": str(origin or "manual_bootstrap"),
        "detector": str(detector or ""),
        "known_before_detection": bool(known_before_detection),
        "prior_user_reported": bool(prior_user_reported),
        "manual_bootstrap": bool(manual_bootstrap),
        "incident": dict(incident),
        "repository": {
            "repository_root": repository_root,
            "repository_ref": str(repository_ref or "master"),
            "base_commit": str(base_commit),
            "base_tree_digest": str(base_tree_digest),
        },
        "provider": {
            "capability_id": CAPABILITY_ID,
            "revision_id": str(request["revision_id"]),
            "binding_id": BINDING_ID,
            "provider_kind": "hermes",
            "provider_instance_id": str(provider_info.get("provider_instance_id") or "hermes.eimemory.code-implementation.production"),
            "operation": "propose_patch_v2",
            "implementation_digest": str(provider_info.get("implementation_digest") or IMPLEMENTATION_DIGEST),
        },
        "advertisement": {
            "advertisement_id": str(provider_info.get("advertisement_id") or ""),
            "advertisement_digest": str(provider_info.get("advertisement_digest") or ""),
        },
        "catalog": {
            "catalog_case_id": str(provider_info.get("catalog_case_id") or ""),
            "catalog_snapshot_digest": str(provider_info.get("catalog_snapshot_digest") or ""),
        },
        "test_plan": {"id": str(test_plan_id), "digest": str(test_plan_digest)},
        "patch_digest": proposal_digest,
    }


def propose_code_patch(
    runtime,
    *,
    incident: dict[str, Any],
    scope: dict | None = None,
    create_worktree: bool = False,
    persist_report: bool = False,
    runner: object | None = None,
    worktree_root: str | Path | None = None,
    proposer: object | None = None,
    file_updates: list[dict[str, Any]] | None = None,
    repo_root: str | Path | None = None,
    machine_policy_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only, auditable code-change proposal.

    The bridge never writes the target repository. A caller may supply an
    already-structured ``file_updates`` payload, or inject a deterministic
    proposer that returns one. Applying a proposal remains the responsibility
    of the separately governed promotion path.
    """
    policy_context = machine_policy_context_from_mapping(
        machine_policy_context if isinstance(machine_policy_context, dict) else incident
    )
    machine_policy = load_code_automation_policy(**policy_context)
    sanitized_incident = _sanitized_incident(
        incident,
        automation_policy=code_automation_policy_summary(machine_policy),
    )

    def policy_proposal(**kwargs: Any) -> dict[str, Any]:
        """Bind every bridge result to the one resolved machine policy."""

        return _proposal(automation_policy=machine_policy, **kwargs)

    sandbox_report = run_code_sandbox(
        runtime,
        incident=sanitized_incident,
        scope=scope,
        create_worktree=create_worktree,
        persist_report=persist_report,
        runner=runner,
        worktree_root=worktree_root,
    )
    sandbox_plan = sandbox_report.get("sandbox_plan")
    is_code_fixable = (
        sandbox_report.get("incident_category") == "code_fixable"
        and isinstance(sandbox_plan, dict)
    )

    if not is_code_fixable:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="not_applicable",
            blocked_reason="not_code_fixable",
            patch_scope=None,
            allowed_files=[],
            sandbox_allowed_files=[],
            verification_commands=[],
            rollback_notes=[],
        )

    sandbox_allowed_files = _coerce_string_list(sandbox_plan.get("allowed_files"))
    rollback_notes = _coerce_string_list(sandbox_plan.get("rollback_notes"))
    default_verification = _coerce_commands(sandbox_plan.get("verification_commands"))
    proposal_root = _resolve_repo_root(repo_root)
    if machine_policy.get("ok") is not True:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_blocked",
            blocked_reason=str(machine_policy.get("reason") or "machine_policy_blocked"),
            patch_scope={"allowed_files": sandbox_allowed_files},
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=default_verification,
            rollback_notes=rollback_notes,
            proposal_source="machine_policy",
        )
    candidate, proposal_source, candidate_error = _proposal_candidate(
        incident=sanitized_incident,
        sandbox_plan=sandbox_plan,
        repo_root=proposal_root,
        allowed_files=sandbox_allowed_files,
        proposer=proposer,
        file_updates=file_updates,
    )
    patch_scope = {"allowed_files": sandbox_allowed_files}
    if candidate_error:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason=candidate_error,
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=default_verification,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )
    if candidate is None or "file_updates" not in candidate:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_unavailable",
            blocked_reason="file_updates_unavailable",
            patch_scope=patch_scope,
            allowed_files=sandbox_allowed_files,
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=default_verification,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )

    normalized_updates, update_error = _normalize_file_updates(candidate.get("file_updates"))
    if update_error:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason=update_error,
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=default_verification,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )
    if not normalized_updates:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_unavailable",
            blocked_reason="file_updates_unavailable",
            patch_scope=patch_scope,
            allowed_files=sandbox_allowed_files,
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=default_verification,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )

    if proposal_root is None:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason="proposal_repo_root_invalid",
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=default_verification,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )
    for update in normalized_updates:
        if not _path_allowed(update["path"], sandbox_allowed_files):
            return policy_proposal(
                sandbox_report=sandbox_report,
                proposal_status="proposal_invalid",
                blocked_reason="file_update_outside_sandbox_allowlist",
                patch_scope=patch_scope,
                allowed_files=[],
                sandbox_allowed_files=sandbox_allowed_files,
                verification_commands=default_verification,
                rollback_notes=rollback_notes,
                proposal_source=proposal_source,
            )

    declared_allowed_files, allowlist_error = _normalize_allowlist(
        candidate.get("allowed_files") or candidate.get("allowlist")
    )
    if allowlist_error:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason=allowlist_error,
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=default_verification,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )
    if declared_allowed_files:
        if any(
            not _path_allowed(pattern, sandbox_allowed_files)
            for pattern in declared_allowed_files
        ):
            return policy_proposal(
                sandbox_report=sandbox_report,
                proposal_status="proposal_invalid",
                blocked_reason="declared_allowlist_outside_sandbox_allowlist",
                patch_scope=patch_scope,
                allowed_files=[],
                sandbox_allowed_files=sandbox_allowed_files,
                verification_commands=default_verification,
                rollback_notes=rollback_notes,
                proposal_source=proposal_source,
            )
        if any(
            not _path_allowed(update["path"], declared_allowed_files)
            for update in normalized_updates
        ):
            return policy_proposal(
                sandbox_report=sandbox_report,
                proposal_status="proposal_invalid",
                blocked_reason="file_update_outside_declared_allowlist",
                patch_scope=patch_scope,
                allowed_files=[],
                sandbox_allowed_files=sandbox_allowed_files,
                verification_commands=default_verification,
                rollback_notes=rollback_notes,
                proposal_source=proposal_source,
            )

    verification_commands = _coerce_commands(
        candidate.get("verification_commands") or candidate.get("verify_commands")
    ) or default_verification
    if _contains_full_test_suite(verification_commands):
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason="full_test_suite_verification_not_allowed",
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=verification_commands,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )
    command_error = code_patch_verification_command_error(verification_commands)
    if command_error:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason=command_error,
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=verification_commands,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )

    file_diffs, file_base_digest, diff_error = _file_diffs_and_base_digest(
        proposal_root, normalized_updates
    )
    if diff_error:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason=diff_error,
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=verification_commands,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )
    unified_diff = "".join(file_diffs)
    if not unified_diff:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason="no_effective_file_updates",
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=verification_commands,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )

    allowed_files = [update["path"] for update in normalized_updates]
    subject_commit = _base_commit(proposal_root)
    subject_state_digest = _subject_state_digest(
        proposal_root, subject_commit=subject_commit
    )
    if not subject_state_digest:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_invalid",
            blocked_reason="subject_state_unavailable",
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=verification_commands,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
            file_base_digest=file_base_digest,
        )
    requested_machine_actions, action_error = _requested_machine_actions(candidate)
    if action_error:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_blocked",
            blocked_reason=action_error,
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=verification_commands,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
        )
    action_policy_error = _machine_policy_action_error(machine_policy, requested_machine_actions)
    if action_policy_error:
        return policy_proposal(
            sandbox_report=sandbox_report,
            proposal_status="proposal_blocked",
            blocked_reason=action_policy_error,
            patch_scope=patch_scope,
            allowed_files=[],
            sandbox_allowed_files=sandbox_allowed_files,
            verification_commands=verification_commands,
            rollback_notes=rollback_notes,
            proposal_source=proposal_source,
            requested_machine_actions=requested_machine_actions,
        )
    patch_digest = _patch_digest(
        repo_root=proposal_root,
        subject_commit=subject_commit,
        subject_state_digest=subject_state_digest,
        allowed_files=allowed_files,
        file_updates=normalized_updates,
        verification_commands=verification_commands,
    )
    return policy_proposal(
        sandbox_report=sandbox_report,
        proposal_status="proposal_ready",
        blocked_reason="",
        patch_scope={"repo_root": str(proposal_root), "allowed_files": allowed_files},
        allowed_files=allowed_files,
        sandbox_allowed_files=sandbox_allowed_files,
        verification_commands=verification_commands,
        rollback_notes=rollback_notes,
        proposal_source=proposal_source,
        file_updates=normalized_updates,
        unified_diff=unified_diff,
        patch_digest=patch_digest,
        base_commit=subject_commit,
        subject_commit=subject_commit,
        subject_state_digest=subject_state_digest,
        file_base_digest=file_base_digest,
        requested_machine_actions=requested_machine_actions,
    )


def _proposal_candidate(
    *,
    incident: dict[str, Any],
    sandbox_plan: dict[str, Any],
    repo_root: Path | None,
    allowed_files: list[str],
    proposer: object | None,
    file_updates: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any] | None, str, str]:
    if file_updates is not None:
        payload = _incident_candidate_payload(incident)
        payload["file_updates"] = file_updates
        return payload, "explicit_file_updates", ""

    payload = _incident_candidate_payload(incident)
    if "file_updates" in payload:
        return payload, "incident_file_updates", ""

    injected = proposer if proposer is not None else incident.get("proposer")
    if injected is None:
        return None, "", ""
    target = injected if callable(injected) else getattr(injected, "propose", None)
    if not callable(target):
        return None, "proposer", "proposer_invalid"
    try:
        result = target(
            incident=dict(incident),
            sandbox_plan=dict(sandbox_plan),
            repo_root=repo_root,
            allowed_files=list(allowed_files),
        )
    except Exception:
        return None, "proposer", "proposer_failed"
    if result is None:
        return None, "proposer", ""
    if isinstance(result, list):
        return {"file_updates": result}, "proposer", ""
    if isinstance(result, dict):
        return dict(result), "proposer", ""
    return None, "proposer", "proposer_result_invalid"


def _incident_candidate_payload(incident: dict[str, Any]) -> dict[str, Any]:
    for key in ("code_patch", "candidate_patch", "patch", "proposal"):
        value = incident.get(key)
        if isinstance(value, dict):
            return _sanitized_candidate_payload(value)
    payload: dict[str, Any] = {}
    for key in (
        "file_updates",
        "allowed_files",
        "allowlist",
        "verification_commands",
        "verify_commands",
        "apply_to_repo",
        "commit_to_repo",
        "deploy_to_production",
    ):
        if key in incident:
            payload[key] = incident[key]
    return _sanitized_candidate_payload(payload)


_UNTRUSTED_AUTOMATION_FIELDS = frozenset(
    {
        "automation_policy",
        "machine_policy",
        "policy",
        "requested_machine_actions",
        "operator_approval_path",
        "operator_approval",
        "approval",
        "approval_status",
        "review_status",
    }
)


def _sanitized_incident(
    incident: dict[str, Any],
    *,
    automation_policy: dict[str, Any],
) -> dict[str, Any]:
    """Drop claimed authority and attach only the trusted policy summary."""

    sanitized = {
        key: value
        for key, value in dict(incident).items()
        if key not in _UNTRUSTED_AUTOMATION_FIELDS
    }
    for key in ("code_patch", "candidate_patch", "patch", "proposal"):
        value = sanitized.get(key)
        if isinstance(value, dict):
            sanitized[key] = _sanitized_candidate_payload(value)
    sanitized["automation_policy"] = dict(automation_policy)
    sanitized["requested_machine_actions"] = []
    return sanitized


def _sanitized_candidate_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in dict(value).items()
        if key not in _UNTRUSTED_AUTOMATION_FIELDS
    }


def _normalize_file_updates(value: Any) -> tuple[list[dict[str, str]], str]:
    if not isinstance(value, list):
        return [], "file_updates_not_list"
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            return [], "invalid_file_update"
        path = _safe_relative_path(item.get("path") or item.get("file"))
        content = item.get("content")
        if not path:
            return [], "unsafe_file_update_path"
        if not isinstance(content, str):
            return [], "file_update_content_not_text"
        if path in seen:
            return [], "duplicate_file_update_path"
        seen.add(path)
        normalized.append({"path": path, "content": content})
    return normalized, ""


def _normalize_allowlist(value: Any) -> tuple[list[str], str]:
    if value is None:
        return [], ""
    raw_items = _coerce_string_list(value)
    if not raw_items:
        return [], ""
    normalized: list[str] = []
    for item in raw_items:
        pattern = _safe_relative_path(item, allow_glob=True)
        if not pattern:
            return [], "unsafe_declared_allowlist"
        if pattern not in normalized:
            normalized.append(pattern)
    return normalized, ""


def _safe_relative_path(value: Any, *, allow_glob: bool = False) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    path = PurePosixPath(raw)
    first = path.parts[0] if path.parts else ""
    if path.is_absolute() or raw.startswith("//") or ":" in first:
        return ""
    if any(part in {"", ".", ".."} for part in path.parts):
        return ""
    if not allow_glob and any(token in raw for token in ("*", "?", "[", "]")):
        return ""
    return "/".join(path.parts)


def _path_allowed(path: str, patterns: list[str]) -> bool:
    for raw_pattern in patterns:
        pattern = _safe_relative_path(raw_pattern, allow_glob=True)
        if not pattern:
            continue
        candidates = {pattern}
        while "**/" in pattern:
            pattern = pattern.replace("**/", "")
            candidates.add(pattern)
        if any(fnmatch.fnmatchcase(path, candidate) for candidate in candidates):
            return True
    return False


def _coerce_commands(value: Any) -> list[str | list[str]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    commands: list[str | list[str]] = []
    for item in value:
        if isinstance(item, str):
            command = item.strip()
            if not command:
                return []
            commands.append(command)
            continue
        if isinstance(item, (list, tuple)) and item and all(not isinstance(part, (dict, list, tuple)) for part in item):
            command = [str(part) for part in item]
            if not any(part.strip() for part in command):
                return []
            commands.append(command)
            continue
        else:
            return []
    return commands


def _contains_full_test_suite(commands: list[str | list[str]]) -> bool:
    for command in commands:
        raw = command if isinstance(command, str) else " ".join(command)
        normalized = " ".join(str(raw).replace("\\", "/").lower().split())
        if "pytest" not in normalized:
            continue
        if any(token.rstrip("/") == "tests" for token in normalized.split(" ")):
            return True
    return False


def _resolve_repo_root(value: str | Path | None) -> Path | None:
    candidate = Path(value) if value is not None else Path.cwd()
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    return resolved if resolved.is_dir() else None


def _file_diffs_and_base_digest(
    repo_root: Path,
    updates: list[dict[str, str]],
) -> tuple[list[str], str, str]:
    digest = sha256()
    diffs: list[str] = []
    for update in updates:
        path = update["path"]
        if _has_symlink_component(repo_root, path):
            return [], "", "file_update_symlink_not_allowed"
        try:
            destination = (repo_root / Path(*PurePosixPath(path).parts)).resolve()
            destination.relative_to(repo_root)
        except (OSError, ValueError):
            return [], "", "file_update_path_escapes_repo"
        if destination.is_symlink() or (destination.exists() and not destination.is_file()):
            return [], "", "file_update_target_not_regular_file"
        try:
            old_bytes = destination.read_bytes() if destination.exists() else b""
            old_content = old_bytes.decode("utf-8") if destination.exists() else ""
        except UnicodeDecodeError:
            return [], "", "file_update_target_not_utf8"
        except OSError:
            return [], "", "file_update_target_unreadable"
        digest.update(f"path:{path}\0".encode("utf-8"))
        digest.update(b"exists:1\0" if destination.exists() else b"exists:0\0")
        digest.update(old_bytes)
        digest.update(b"\0")
        diffs.extend(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                update["content"].splitlines(keepends=True),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
            )
        )
    return diffs, digest.hexdigest(), ""


def _has_symlink_component(repo_root: Path, relative_path: str) -> bool:
    """Reject links/reparse points before resolving a proposal path."""
    current = repo_root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        try:
            entry = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        attributes = getattr(entry, "st_file_attributes", 0)
        if stat.S_ISLNK(entry.st_mode) or bool(reparse_flag and attributes & reparse_flag):
            return True
    return False


def _base_commit(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _subject_state_digest(repo_root: Path, *, subject_commit: str) -> str:
    """Mirror the downstream preflight subject-state contract exactly."""
    if not repo_root.exists() or not repo_root.is_dir():
        return ""
    if subject_commit:
        return sha256(f"git:{subject_commit}".encode("utf-8")).hexdigest()
    ignored_parts = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    digest = sha256()
    try:
        paths = sorted(
            (
                path
                for path in repo_root.rglob("*")
                if not any(part in ignored_parts for part in path.relative_to(repo_root).parts)
            ),
            key=lambda path: path.relative_to(repo_root).as_posix(),
        )
        for path in paths:
            relative = path.relative_to(repo_root).as_posix()
            if path.is_symlink():
                digest.update(f"link:{relative}\0{os.readlink(path)}\0".encode("utf-8"))
                continue
            if not path.is_file():
                continue
            digest.update(f"file:{relative}\0".encode("utf-8"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
    except Exception:
        return ""
    return digest.hexdigest()


def _patch_digest(
    *,
    repo_root: Path,
    subject_commit: str,
    subject_state_digest: str,
    allowed_files: list[str],
    file_updates: list[dict[str, str]],
    verification_commands: list[str | list[str]],
) -> str:
    payload = {
        "repo_root": str(repo_root.resolve()),
        "subject_commit": subject_commit,
        "subject_state_digest": subject_state_digest,
        "allowed_files": sorted(allowed_files),
        "file_updates": sorted(file_updates, key=lambda item: (item["path"], item["content"])),
        "verification_commands": verification_commands,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _requested_machine_actions(candidate: dict[str, Any]) -> tuple[list[str], str]:
    """Require explicit boolean action requests before policy matching."""

    actions = ["local_apply"]
    for field, action in (
        ("commit_to_repo", "commit"),
        ("deploy_to_production", "deployment"),
    ):
        value = candidate.get(field, False)
        if not isinstance(value, bool):
            return [], f"machine_action_{field}_must_be_boolean"
        if value:
            actions.append(action)
    if "deployment" in actions and "commit" not in actions:
        return [], "machine_action_deployment_requires_commit"
    return actions, ""


def _machine_policy_action_error(
    automation_policy: dict[str, Any],
    requested_machine_actions: list[str],
) -> str:
    summary = code_automation_policy_summary(automation_policy)
    if summary.get("ok") is not True:
        return str(summary.get("reason") or "machine_policy_blocked")
    actions = summary.get("actions") if isinstance(summary.get("actions"), dict) else {}
    for action in requested_machine_actions:
        if action not in {"local_apply", "commit", "deployment"}:
            return "machine_policy_action_unknown"
        if actions.get(action) is not True:
            return f"machine_policy_{action}_not_enabled"
    return ""


def _proposal(
    *,
    sandbox_report: dict[str, Any],
    proposal_status: str,
    blocked_reason: str,
    patch_scope: dict[str, Any] | None,
    allowed_files: list[str],
    sandbox_allowed_files: list[str],
    verification_commands: list[str | list[str]],
    rollback_notes: list[str],
    proposal_source: str = "",
    file_updates: list[dict[str, str]] | None = None,
    unified_diff: str = "",
    patch_digest: str = "",
    base_commit: str = "",
    subject_commit: str = "",
    subject_state_digest: str = "",
    file_base_digest: str = "",
    automation_policy: dict[str, Any] | None = None,
    requested_machine_actions: list[str] | None = None,
) -> dict[str, Any]:
    ready = proposal_status == "proposal_ready"
    policy_summary = code_automation_policy_summary(automation_policy)
    return {
        "ok": bool(sandbox_report.get("ok")),
        "report_type": CODE_PATCH_PROPOSAL_REPORT_TYPE,
        "schema_version": CODE_PATCH_PROPOSAL_SCHEMA_VERSION,
        "source_sandbox_report_type": str(sandbox_report.get("report_type") or ""),
        "proposal_status": proposal_status,
        "proposal_source": proposal_source,
        "blocked": not ready,
        "blocked_reason": blocked_reason,
        "read_only": True,
        "mutates_repository": False,
        "authorization_mode": "machine_gated",
        "decision_authority": "machine_policy",
        "machine_policy_required_for_apply": True,
        "automation_policy": policy_summary,
        "requested_machine_actions": list(requested_machine_actions or []),
        "incident_category": str(sandbox_report.get("incident_category") or "unknown"),
        "patch_scope": patch_scope,
        "allowed_files": allowed_files,
        "sandbox_allowed_files": sandbox_allowed_files,
        "file_updates": list(file_updates or []),
        "unified_diff": unified_diff,
        "patch_digest": patch_digest,
        "repo_root": str(patch_scope.get("repo_root") or "") if patch_scope else "",
        "base_commit": base_commit,
        "subject_commit": subject_commit,
        "subject_state_digest": subject_state_digest,
        "file_base_digest": file_base_digest,
        "verification_commands": verification_commands,
        "rollback_notes": rollback_notes,
        "sandbox_plan": sandbox_report.get("sandbox_plan"),
        "persisted_record_id": str(sandbox_report.get("persisted_record_id") or ""),
    }


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
