from __future__ import annotations

from pathlib import Path
from typing import Any

from eimemory.governance.code_evolution import run_code_sandbox


CODE_PATCH_PROPOSAL_REPORT_TYPE = "code_patch_proposal"


def propose_code_patch(
    runtime,
    *,
    incident: dict[str, Any],
    scope: dict | None = None,
    create_worktree: bool = False,
    persist_report: bool = False,
    runner: object | None = None,
    worktree_root: str | Path | None = None,
) -> dict[str, Any]:
    """Build a sandbox-backed code patch proposal without mutating production."""
    sandbox_report = run_code_sandbox(
        runtime,
        incident=incident,
        scope=scope,
        create_worktree=create_worktree,
        persist_report=persist_report,
        runner=runner,
        worktree_root=worktree_root,
    )
    sandbox_plan = sandbox_report.get("sandbox_plan")
    is_code_fixable = sandbox_report.get("incident_category") == "code_fixable" and isinstance(sandbox_plan, dict)

    if not is_code_fixable:
        return _proposal(
            sandbox_report=sandbox_report,
            proposal_status="not_applicable",
            patch_scope=None,
            allowed_files=[],
            verification_commands=[],
            rollback_notes=[],
        )

    allowed_files = _coerce_string_list(sandbox_plan.get("allowed_files"))
    return _proposal(
        sandbox_report=sandbox_report,
        proposal_status="sandbox_ready",
        patch_scope={"allowed_files": allowed_files},
        allowed_files=allowed_files,
        verification_commands=_coerce_string_list(sandbox_plan.get("verification_commands")),
        rollback_notes=_coerce_string_list(sandbox_plan.get("rollback_notes")),
    )


def _proposal(
    *,
    sandbox_report: dict[str, Any],
    proposal_status: str,
    patch_scope: dict[str, Any] | None,
    allowed_files: list[str],
    verification_commands: list[str],
    rollback_notes: list[str],
) -> dict[str, Any]:
    return {
        "ok": bool(sandbox_report.get("ok")),
        "report_type": CODE_PATCH_PROPOSAL_REPORT_TYPE,
        "source_sandbox_report_type": str(sandbox_report.get("report_type") or ""),
        "proposal_status": proposal_status,
        "incident_category": str(sandbox_report.get("incident_category") or "unknown"),
        "patch_scope": patch_scope,
        "allowed_files": allowed_files,
        "verification_commands": verification_commands,
        "rollback_notes": rollback_notes,
        "sandbox_plan": sandbox_report.get("sandbox_plan"),
        "persisted_record_id": str(sandbox_report.get("persisted_record_id") or ""),
    }


def _coerce_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]
