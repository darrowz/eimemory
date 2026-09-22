"""Code-apply transaction helpers extracted from promotion_manager (god-file slice).

Helpers that still live in promotion_manager are imported lazily inside call
bodies to avoid import cycles.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key

from eimemory.governance.runtime_protocol import GovernanceRuntime
from eimemory.models.records import RecordEnvelope, ScopeRef

# Constants mirrored from promotion_manager (kept identical).
CODE_APPLY_TRANSACTION_SOURCE = "eimemory.code_apply_transaction"
CODE_APPLY_TRANSACTION_SCHEMA_VERSION = 1
CODE_APPLY_TRANSACTION_IN_FLIGHT = "in_flight"
CODE_APPLY_TRANSACTION_QUARANTINED = "recovery_quarantined"

from eimemory.governance.promotion_git_ops import (
    _current_commit_sha,
    _repo_has_dirty_worktree,
)


def _pm():
    from eimemory.governance import promotion_manager as pm
    return pm


def bind_promotion_manager(pm_module) -> None:
    """Optional eager bind; call sites primarily use ``_pm()`` lazy access."""
    globals()["_bound_pm"] = pm_module


def _find_code_apply_transaction(
    runtime: GovernanceRuntime,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
) -> RecordEnvelope | None:
    """Locate the durable code_apply transaction that owns backups for this candidate."""
    scope_ref = scope or candidate.scope
    candidates: list[str] = []
    for source in (
        candidate.meta.get("transaction_id"),
        candidate.content.get("transaction_id") if isinstance(candidate.content, dict) else None,
    ):
        tid = str(source or "").strip()
        if tid and tid not in candidates:
            candidates.append(tid)
    for artifact_id in candidate.meta.get("applied_artifact_ids") or []:
        try:
            record = runtime.store.get_by_id(str(artifact_id), scope=scope_ref)
        except Exception:
            record = None
        if record is None or not isinstance(record.content, dict):
            continue
        tid = str(record.content.get("transaction_id") or "").strip()
        if tid and tid not in candidates:
            candidates.append(tid)
    for tid in candidates:
        try:
            record = runtime.store.get_by_id(tid, scope=scope_ref)
        except Exception:
            record = None
        if (
            record is not None
            and record.source == CODE_APPLY_TRANSACTION_SOURCE
            and isinstance(record.content, dict)
            and str(record.content.get("transaction_type") or "") == "code_apply"
        ):
            return record
    try:
        records = runtime.store.list_records(kinds=["promotion_request"], scope=scope_ref, limit=200)
    except Exception:
        records = []
    for record in records:
        content = record.content if isinstance(record.content, dict) else {}
        if record.source != CODE_APPLY_TRANSACTION_SOURCE:
            continue
        if str(content.get("transaction_type") or "") != "code_apply":
            continue
        if str(content.get("candidate_id") or "") != candidate.record_id:
            continue
        return record
    return None

def _attempt_code_apply_artifact_rollback(
    runtime: GovernanceRuntime,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    reason: str,
    artifact_ids: list[str],
) -> dict[str, Any]:
    """Best-effort worktree restore from recorded code_apply backups (B02).

    Succeeds only when every applied artifact id is accounted for and file
    restore (or already-restored state) is verified. Never reports ok when
    production deploy was applied — deploy undo is out of band.
    """
    transaction = _find_code_apply_transaction(runtime, candidate, scope=scope)
    if transaction is None:
        return {
            "ok": False,
            "attempted": True,
            "blocked_reason": "artifact_rollback_required",
            "undone": [],
            "unsupported_artifact_kinds": list(artifact_ids),
            "errors": ["code_apply_transaction_missing"],
        }
    content = dict(transaction.content or {})
    # Detect production deploy via playbook / transaction markers.
    production_applied = bool(content.get("production_applied"))
    playbook_ids = [aid for aid in artifact_ids if not _pm()._looks_like_code_path_artifact(aid)]
    for playbook_id in playbook_ids:
        try:
            playbook = runtime.store.get_by_id(playbook_id, scope=scope or candidate.scope)
        except Exception:
            playbook = None
        if playbook is not None and isinstance(playbook.content, dict):
            production_applied = production_applied or bool(playbook.content.get("production_applied"))
            production_applied = production_applied or bool(playbook.meta.get("production_applied"))
    if production_applied:
        # B02 closed-by-design: production deploy undo is impossible without host
        # deploy access. Enter durable reconciliation — never report ok.
        reconciliation_state = {
            "state": "artifact_rollback_required",
            "reason": "production_deploy_undo_unsupported",
            "durable": True,
            "operator_procedure": (
                "Manual production rollback required: restore prior release via "
                "deploy/receipts, then clear artifact_rollback_required after "
                "effect-owner digest reconciliation confirms prior digests."
            ),
            "candidate_id": getattr(candidate, "record_id", None),
            "artifact_ids": list(artifact_ids),
        }
        ledger_event = None
        try:
            ledger_event = _pm()._record_candidate_lifecycle(
                runtime,
                candidate,
                scope=scope,
                action_type="artifact_rollback_required",
                reason="production_deploy_undo_unsupported",
                details=reconciliation_state,
                side_effect={"ok": False, "blocked_reason": "artifact_rollback_required"},
            )
        except Exception as exc:
            ledger_event = {"ok": False, "error": str(exc)}
        try:
            candidate.meta["reconciliation_state"] = reconciliation_state
            candidate.meta["artifact_rollback_required"] = True
            runtime.store.rewrite(candidate)
        except Exception:
            pass
        return {
            "ok": False,
            "attempted": True,
            "blocked_reason": "artifact_rollback_required",
            "requires_reconciliation": True,
            "reconciliation_state": reconciliation_state,
            "lifecycle_ledger": ledger_event,
            "undone": [],
            "unsupported_artifact_kinds": list(artifact_ids),
            "errors": ["code_apply_production_deploy_undo_unsupported"],
        }

    repo_root, repo_error = _pm()._transaction_repo_root(content)
    if repo_error or repo_root is None:
        return {
            "ok": False,
            "attempted": True,
            "blocked_reason": "artifact_rollback_required",
            "undone": [],
            "unsupported_artifact_kinds": list(artifact_ids),
            "errors": [repo_error or "code_apply_repository_unavailable"],
        }
    backups, planned_files, file_error = _deserialize_code_apply_recovery_files(repo_root, content)
    if file_error:
        return {
            "ok": False,
            "attempted": True,
            "blocked_reason": "artifact_rollback_required",
            "undone": [],
            "unsupported_artifact_kinds": list(artifact_ids),
            "errors": [file_error],
        }
    planned_paths = {str(item["path"]) for item in planned_files}
    path_artifacts = [aid for aid in artifact_ids if _pm()._looks_like_code_path_artifact(aid)]
    unknown_paths = [aid for aid in path_artifacts if aid not in planned_paths]
    if unknown_paths:
        return {
            "ok": False,
            "attempted": True,
            "blocked_reason": "artifact_rollback_required",
            "undone": [],
            "unsupported_artifact_kinds": unknown_paths,
            "errors": ["code_apply_artifact_path_not_in_transaction"],
        }

    unchanged, state_error = _code_apply_recovery_file_state(
        repo_root, backups=backups, planned_files=planned_files
    )
    if state_error:
        return {
            "ok": False,
            "attempted": True,
            "blocked_reason": "artifact_rollback_required",
            "undone": [],
            "unsupported_artifact_kinds": list(artifact_ids),
            "errors": [state_error],
        }
    if not unchanged:
        _pm()._restore_file_updates(backups)
        unchanged_after, after_error = _code_apply_recovery_file_state(
            repo_root, backups=backups, planned_files=planned_files
        )
        if after_error or not unchanged_after:
            return {
                "ok": False,
                "attempted": True,
                "blocked_reason": "artifact_rollback_required",
                "undone": [],
                "unsupported_artifact_kinds": list(artifact_ids),
                "errors": [after_error or "code_apply_restore_verify_failed"],
            }

    undone: list[dict[str, Any]] = []
    for playbook_id in playbook_ids:
        try:
            playbook = runtime.store.get_by_id(playbook_id, scope=scope or candidate.scope)
        except Exception:
            playbook = None
        if playbook is None or playbook.kind not in {"learning_playbook", "rule", "memory", "capability_candidate"}:
            return {
                "ok": False,
                "attempted": True,
                "blocked_reason": "artifact_rollback_required",
                "undone": undone,
                "unsupported_artifact_kinds": [playbook_id],
                "errors": ["code_apply_playbook_missing"],
            }
        playbook.status = "rolled_back"
        playbook.meta["rolled_back_reason"] = reason
        runtime.store.rewrite(playbook)
        undone.append({"id": playbook_id, "kind": playbook.kind, "result": {"ok": True}})
    for path in path_artifacts:
        undone.append({"id": path, "kind": "code_file", "result": {"ok": True, "restored": True}})

    # Mark durable transaction rolled back when it was a completed apply.
    if str(transaction.status or "") in {"completed", CODE_APPLY_TRANSACTION_IN_FLIGHT}:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage="rollback_completed",
            status="rolled_back",
            reason=reason,
            rollback={
                "ok": True,
                "execution_type": "code_apply_candidate_rollback",
                "file_restore": {"ok": True, "restored_count": len(backups)},
            },
        )

    if len(undone) != len(artifact_ids):
        missing = [aid for aid in artifact_ids if aid not in {item["id"] for item in undone}]
        return {
            "ok": False,
            "attempted": True,
            "blocked_reason": "artifact_rollback_required",
            "undone": undone,
            "unsupported_artifact_kinds": missing,
            "errors": ["code_apply_artifact_incomplete"],
        }
    return {
        "ok": True,
        "attempted": True,
        "undone": undone,
        "unsupported_artifact_kinds": [],
        "transaction_id": transaction.record_id,
    }

def _inflight_code_apply_transactions(
    runtime: GovernanceRuntime,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 100,
    repo_root: Path | None = None,
    include_quarantined: bool = False,
) -> list[RecordEnvelope]:
    records = runtime.store.list_records(
        kinds=["promotion_request"],
        scope=scope,
        limit=max(1, int(limit)),
    )
    expected_root = str(repo_root.resolve()) if repo_root is not None else ""
    result: list[RecordEnvelope] = []
    for record in records:
        content = record.content if isinstance(record.content, dict) else {}
        if record.source != CODE_APPLY_TRANSACTION_SOURCE:
            continue
        if str(content.get("transaction_type") or "") != "code_apply":
            continue
        statuses = {CODE_APPLY_TRANSACTION_IN_FLIGHT}
        if include_quarantined:
            statuses.add(CODE_APPLY_TRANSACTION_QUARANTINED)
        if str(record.status or "") not in statuses:
            continue
        if expected_root and str(content.get("repo_root") or "") != expected_root:
            continue
        result.append(record)
    return result

def _begin_code_apply_transaction(
    runtime: GovernanceRuntime,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    repo_root: Path,
    file_updates: list[dict[str, str]],
    prepared: list[dict[str, str]],
    backups: list[dict[str, Any]],
    allowed_files: list[str],
    prior_commit_sha: str,
    subject_state_digest: str,
    verification_commands: list[str | list[str]],
    automation_policy: dict[str, Any],
) -> RecordEnvelope:
    scope_ref = (
        scope
        if isinstance(scope, ScopeRef)
        else (ScopeRef.from_dict(scope) if isinstance(scope, dict) else candidate.scope)
    )
    patch_digest = _pm()._code_patch_digest(
        patch,
        repo_root=repo_root,
        subject_commit=prior_commit_sha,
        subject_state_digest=subject_state_digest,
        file_updates=file_updates,
        verification_commands=verification_commands,
    )
    planned_files = [
        {
            "path": str(item["path"]),
            "new_content_sha256": sha256(_pm()._file_update_written_bytes(str(item["content"]))).hexdigest(),
        }
        for item in prepared
    ]
    serialized_backups = _serialize_code_apply_backups(repo_root, prepared=prepared, backups=backups)
    record = RecordEnvelope.create(
        kind="promotion_request",
        title=f"Code apply transaction: {candidate.title}",
        summary=f"Durable in-flight direct code apply for {candidate.record_id}",
        scope=scope_ref,
        source=CODE_APPLY_TRANSACTION_SOURCE,
        status=CODE_APPLY_TRANSACTION_IN_FLIGHT,
        content={
            "schema_version": CODE_APPLY_TRANSACTION_SCHEMA_VERSION,
            "transaction_type": "code_apply",
            "candidate_id": candidate.record_id,
            "loop_id": str(loop_id or ""),
            "repo_root": str(repo_root.resolve()),
            "patch_digest": patch_digest,
            "prior_commit_sha": str(prior_commit_sha or ""),
            "subject_state_digest": str(subject_state_digest or ""),
            "automation_policy": dict(automation_policy),
            "allowed_files": list(allowed_files),
            "planned_files": planned_files,
            "backups": serialized_backups,
            "recovery_patch": {"rollback_commands": _pm()._rollback_commands(patch)},
            "stage": "prepared",
            "stage_history": [{"stage": "prepared"}],
        },
        meta={
            "transaction_type": "code_apply",
            "candidate_id": candidate.record_id,
            "repo_root": str(repo_root.resolve()),
            "patch_digest": patch_digest,
            "automation_policy_id": str(automation_policy.get("policy_id") or ""),
            "stage": "prepared",
        },
    )
    return runtime.store.append(record)

def _serialize_code_apply_backups(
    repo_root: Path,
    *,
    prepared: list[dict[str, str]],
    backups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(prepared) != len(backups):
        raise ValueError("code_apply_backup_count_mismatch")
    serialized: list[dict[str, Any]] = []
    for item, backup in zip(prepared, backups):
        relative_path = str(item["path"])
        backup_path = backup.get("path")
        if not isinstance(backup_path, Path) or _pm()._relative_backup_path(repo_root, backup) != relative_path:
            raise ValueError("code_apply_backup_path_mismatch")
        content = bytes(backup.get("content") or b"")
        serialized.append(
            {
                "path": relative_path,
                "existed": bool(backup.get("existed")),
                "content_b64": b64encode(content).decode("ascii"),
                "content_sha256": sha256(content).hexdigest(),
            }
        )
    return serialized

def _update_code_apply_transaction(
    runtime: GovernanceRuntime,
    transaction: RecordEnvelope,
    *,
    stage: str,
    status: str | None = None,
    reason: str = "",
    rollback: dict[str, Any] | None = None,
    commit: dict[str, Any] | None = None,
    deployment: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    recovery: dict[str, Any] | None = None,
) -> RecordEnvelope:
    content = dict(transaction.content or {})
    history = [dict(item) for item in content.get("stage_history") or [] if isinstance(item, dict)]
    entry: dict[str, Any] = {"stage": str(stage)}
    if reason:
        entry["reason"] = str(reason)
    history.append(entry)
    content["stage"] = str(stage)
    content["stage_history"] = history[-32:]
    if reason:
        content["failure_reason"] = str(reason)
    if rollback is not None:
        content["rollback"] = dict(rollback)
    if commit is not None:
        content["commit"] = dict(commit)
    if deployment is not None:
        content["deployment"] = dict(deployment)
    if verification is not None:
        content["verification"] = dict(verification)
    if recovery is not None:
        content["recovery"] = dict(recovery)
    transaction.content = content
    if status is not None:
        transaction.status = str(status)
    transaction.meta["stage"] = str(stage)
    transaction.meta["transaction_status"] = str(transaction.status)
    if reason:
        transaction.meta["failure_reason"] = str(reason)
    transaction.touch()
    return runtime.store.rewrite(transaction)

def _complete_code_apply_rollback(
    runtime: GovernanceRuntime,
    transaction: RecordEnvelope,
    *,
    rollback: dict[str, Any],
    reason: str,
    verification: dict[str, Any] | None = None,
    commit: dict[str, Any] | None = None,
    deployment: dict[str, Any] | None = None,
) -> RecordEnvelope:
    success = bool(rollback.get("ok"))
    return _update_code_apply_transaction(
        runtime,
        transaction,
        stage="rollback_completed" if success else CODE_APPLY_TRANSACTION_QUARANTINED,
        status="rolled_back" if success else CODE_APPLY_TRANSACTION_QUARANTINED,
        reason=reason,
        rollback=rollback,
        verification=verification,
        commit=commit,
        deployment=deployment,
    )

def _recover_code_apply_transaction(runtime: GovernanceRuntime, transaction: RecordEnvelope) -> dict[str, Any]:
    content = transaction.content if isinstance(transaction.content, dict) else {}
    transaction_id = transaction.record_id
    repo_root, repo_error = _pm()._transaction_repo_root(content)
    if repo_error or repo_root is None:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage=CODE_APPLY_TRANSACTION_QUARANTINED,
            status=CODE_APPLY_TRANSACTION_QUARANTINED,
            reason=repo_error or "code_apply_repository_unavailable",
            recovery={"action": "quarantined", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": False,
            "recovered": False,
            "recovery_quarantined": True,
            "reason": repo_error or "code_apply_repository_unavailable",
            "retried_apply": False,
        }

    backups, planned_files, file_error = _deserialize_code_apply_recovery_files(repo_root, content)
    if file_error:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage=CODE_APPLY_TRANSACTION_QUARANTINED,
            status=CODE_APPLY_TRANSACTION_QUARANTINED,
            reason=file_error,
            recovery={"action": "quarantined", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": False,
            "recovered": False,
            "recovery_quarantined": True,
            "reason": file_error,
            "retried_apply": False,
        }

    unchanged, state_error = _code_apply_recovery_file_state(repo_root, backups=backups, planned_files=planned_files)
    if state_error:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage=CODE_APPLY_TRANSACTION_QUARANTINED,
            status=CODE_APPLY_TRANSACTION_QUARANTINED,
            reason=state_error,
            recovery={"action": "quarantined", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": False,
            "recovered": False,
            "recovery_quarantined": True,
            "reason": state_error,
            "retried_apply": False,
        }

    prior_commit_sha = str(content.get("prior_commit_sha") or "")
    commit = content.get("commit") if isinstance(content.get("commit"), dict) else {}
    new_commit_sha = str(commit.get("commit_sha") or "")
    reset_repo = False
    if (repo_root / ".git").exists():
        current_commit_sha = _current_commit_sha(repo_root, timeout_seconds=30)
        if current_commit_sha == prior_commit_sha:
            reset_repo = False
        elif new_commit_sha and current_commit_sha == new_commit_sha and not _repo_has_dirty_worktree(repo_root):
            reset_repo = True
        else:
            reason = "code_apply_recovery_git_state_ambiguous"
            _update_code_apply_transaction(
                runtime,
                transaction,
                stage=CODE_APPLY_TRANSACTION_QUARANTINED,
                status=CODE_APPLY_TRANSACTION_QUARANTINED,
                reason=reason,
                recovery={
                    "action": "quarantined",
                    "retry_apply": False,
                    "current_commit_sha": current_commit_sha,
                    "prior_commit_sha": prior_commit_sha,
                    "new_commit_sha": new_commit_sha,
                },
            )
            return {
                "transaction_id": transaction_id,
                "ok": False,
                "recovered": False,
                "recovery_quarantined": True,
                "reason": reason,
                "retried_apply": False,
            }

    rollback_side_effect_stages = {
        "deployment_started",
        "deployment_completed",
        "post_deploy_health_started",
        "post_deploy_health_passed",
        "canary_started",
        "canary_completed",
    }
    if unchanged and not reset_repo and str(content.get("stage") or "") not in rollback_side_effect_stages:
        _update_code_apply_transaction(
            runtime,
            transaction,
            stage="recovered_noop",
            status="rolled_back",
            recovery={"action": "noop_already_restored", "retry_apply": False},
        )
        return {
            "transaction_id": transaction_id,
            "ok": True,
            "recovered": True,
            "recovery_quarantined": False,
            "rollback": {"ok": True, "skipped": True, "reason": "already_restored"},
            "retried_apply": False,
        }

    recovery_patch = content.get("recovery_patch") if isinstance(content.get("recovery_patch"), dict) else {}
    rollback = _pm()._rollback_code_patch(
        repo_root=repo_root,
        patch=recovery_patch,
        backups=backups,
        timeout_seconds=30,
        phase="crash_recovery",
        prior_commit_sha=prior_commit_sha,
        reset_repo=reset_repo,
    )
    completed = _pm()._complete_code_apply_rollback(
        runtime,
        transaction,
        rollback=rollback,
        reason="code_apply_crash_recovery",
    )
    return {
        "transaction_id": transaction_id,
        "ok": bool(rollback.get("ok")),
        "recovered": bool(rollback.get("ok")),
        "recovery_quarantined": completed.status == CODE_APPLY_TRANSACTION_QUARANTINED,
        "rollback": rollback,
        "retried_apply": False,
    }

def _deserialize_code_apply_recovery_files(
    repo_root: Path,
    content: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, str]], str]:
    raw_backups = content.get("backups")
    raw_planned = content.get("planned_files")
    if not isinstance(raw_backups, list) or not isinstance(raw_planned, list) or not raw_backups or len(raw_backups) != len(raw_planned):
        return [], [], "code_apply_recovery_backup_invalid"
    backups: list[dict[str, Any]] = []
    planned_files: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    try:
        for backup_data, planned_data in zip(raw_backups, raw_planned):
            if not isinstance(backup_data, dict) or not isinstance(planned_data, dict):
                raise ValueError("code_apply_recovery_backup_invalid")
            relative_path = _pm()._safe_repo_relative_path(str(backup_data.get("path") or ""))
            planned_path = _pm()._safe_repo_relative_path(str(planned_data.get("path") or ""))
            if relative_path != planned_path or relative_path in seen_paths:
                raise ValueError("code_apply_recovery_path_invalid")
            seen_paths.add(relative_path)
            raw_content = b64decode(str(backup_data.get("content_b64") or "").encode("ascii"), validate=True)
            if sha256(raw_content).hexdigest() != str(backup_data.get("content_sha256") or ""):
                raise ValueError("code_apply_recovery_backup_digest_invalid")
            new_digest = str(planned_data.get("new_content_sha256") or "")
            if len(new_digest) != 64:
                raise ValueError("code_apply_recovery_patch_digest_invalid")
            backups.append(
                {
                    "path": _pm()._repo_child(repo_root, relative_path),
                    "existed": bool(backup_data.get("existed")),
                    "content": raw_content,
                }
            )
            planned_files.append({"path": relative_path, "new_content_sha256": new_digest})
    except Exception as exc:
        reason = str(exc).strip()
        return [], [], reason if reason.startswith("code_apply_") else "code_apply_recovery_backup_invalid"
    return backups, planned_files, ""

def _code_apply_recovery_file_state(
    repo_root: Path,
    *,
    backups: list[dict[str, Any]],
    planned_files: list[dict[str, str]],
) -> tuple[bool, str]:
    all_original = True
    try:
        for backup, planned in zip(backups, planned_files):
            destination = backup["path"]
            if not isinstance(destination, Path) or _pm()._relative_backup_path(repo_root, backup) != planned["path"]:
                return False, "code_apply_recovery_path_invalid"
            existed = bool(backup.get("existed"))
            if not destination.exists():
                if existed:
                    return False, f"code_apply_recovery_target_missing:{planned['path']}"
                continue
            if not destination.is_file():
                return False, f"code_apply_recovery_target_not_file:{planned['path']}"
            digest = sha256(destination.read_bytes()).hexdigest()
            original_digest = sha256(bytes(backup.get("content") or b"")).hexdigest()
            if existed and digest == original_digest:
                continue
            if not existed and digest == original_digest:
                return False, f"code_apply_recovery_unexpected_target:{planned['path']}"
            if digest == planned["new_content_sha256"]:
                all_original = False
                continue
            return False, f"code_apply_recovery_target_changed:{planned['path']}"
    except Exception:
        return False, "code_apply_recovery_target_unreadable"
    return all_original, ""
