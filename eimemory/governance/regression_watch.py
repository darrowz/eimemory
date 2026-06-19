from __future__ import annotations

from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def run_regression_watch(
    runtime: Any,
    *,
    candidate_id: str,
    eval_result: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual",
) -> dict[str, Any]:
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None:
        raise ValueError(f"candidate not found: {candidate_id}")
    scores = dict(eval_result.get("scores") or {})
    regressed = str(eval_result.get("verdict") or "") == "fail" or float(scores.get("regression") or 1.0) < 0.9
    tier = str(candidate.meta.get("authority_tier") or "L0").upper()
    action = "observed"
    if regressed:
        action = "disabled" if tier in {"L0", "L1"} else "rollback_requested"
    if regressed and tier in {"L0", "L1"}:
        candidate.status = "disabled"
        candidate.meta["disabled_reason"] = "regression_watch"
        runtime.store.rewrite(candidate)
    record = append_learning_record_once(
        runtime,
        kind="regression_watch",
        title=f"Regression watch: {candidate.title}",
        summary=f"Regression detected for {candidate.record_id}" if regressed else f"No regression detected for {candidate.record_id}",
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="regression_watch",
        semantic_key=stable_semantic_key("regression", candidate.record_id, eval_result),
        authority_tier=tier,
        status="active",
        content={
            "candidate_id": candidate.record_id,
            "eval_result": eval_result,
            "regressed": bool(regressed),
            "action": action,
            "rollback": candidate.content.get("rollback") or "disable candidate",
        },
        meta={"candidate_id": candidate.record_id, "action": action, "authority_tier": tier, "regressed": bool(regressed)},
    )
    return {"ok": True, "regressed": bool(regressed), "action": action, "record_id": record.record_id}
