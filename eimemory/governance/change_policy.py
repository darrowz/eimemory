from __future__ import annotations

from typing import Any


def decide_change_policy(
    *,
    event: str,
    closure_complete: bool = False,
    user_no_full_suite: bool = False,
    judgment_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the release-validation action used by closure and capability probes."""

    normalized = str(event or "").strip().lower()
    if normalized == "bug_fixed":
        report = dict(judgment_report or {})
        repeated = report.get("repeated_failures") if isinstance(report.get("repeated_failures"), list) else []
        return {
            "decision": "add_replay" if repeated else "collect_failure_evidence",
            "validation_required": True,
        }
    if normalized == "code_change":
        complete = bool(closure_complete)
        return {
            "decision": "bump_patch" if complete else "finish_closure_first",
            "closure_required": True,
            "premature_bump": not complete,
        }
    if normalized == "small_module":
        targeted = bool(user_no_full_suite)
        return {
            "test_scope": "targeted" if targeted else "layered",
            "full_suite_requested": False,
        }
    return {"decision": "observe", "validation_required": False}


__all__ = ["decide_change_policy"]
