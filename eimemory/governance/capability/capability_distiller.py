from __future__ import annotations

import json
from typing import Any

from eimemory.governance.learning_eval import REGRESSION_THRESHOLD, SAFETY_THRESHOLD
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def distill_capability_candidate(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    experiment_id: str,
    eval_result: dict[str, Any],
    promotion_target: str,
    summary: str,
    target_capability: str = "",
    candidate_patch: dict[str, Any] | None = None,
    allow_proposal_only: bool = False,
) -> str:
    target_capability = str(target_capability or "").strip()
    if not target_capability:
        raise ValueError("target_capability must be explicitly attributed")
    _validate_eval(eval_result, allow_proposal_only=allow_proposal_only)
    scores = dict(eval_result.get("scores") or {})
    tier = _tier_for_target(promotion_target)
    normalized_patch = dict(candidate_patch or {})
    hypothesis_context = normalized_patch.get("capability_hypothesis")
    hypothesis_context = dict(hypothesis_context) if isinstance(hypothesis_context, dict) else {}
    candidate_bounds = normalized_patch.get("candidate_bounds")
    candidate_bounds = dict(candidate_bounds) if isinstance(candidate_bounds, dict) else {}
    replay_case_ids = normalized_patch.get("replay_case_ids")
    replay_case_ids = [str(value) for value in replay_case_ids if str(value)] if isinstance(replay_case_ids, (list, tuple)) else []
    code_evolution_v2 = normalized_patch.get("code_evolution_v2") is True
    code_evolution_transaction_id = (
        str(normalized_patch.get("transaction_id") or "").strip() if code_evolution_v2 else ""
    )
    code_evolution_proposal_digest = (
        str(normalized_patch.get("proposal_digest") or "").strip() if code_evolution_v2 else ""
    )
    # A generic summary must not deduplicate candidates from two distinct
    # hypotheses or bounded replay sets.  Those links are behavior-relevant
    # evidence, not presentation metadata.
    semantic_key = stable_semantic_key(
        "capability_candidate",
        target_capability,
        promotion_target,
        summary,
        hypothesis_context.get("hypothesis_id"),
        hypothesis_context.get("link_digest"),
        json.dumps(candidate_bounds, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str),
        json.dumps(sorted(replay_case_ids), ensure_ascii=False, separators=(",", ":")),
        code_evolution_transaction_id,
        code_evolution_proposal_digest,
    )
    existing = _existing_candidate_by_semantic_key(runtime, scope=scope, semantic_key=semantic_key)
    if existing:
        return existing
    readable_title = _candidate_title(
        target_capability=target_capability,
        promotion_target=promotion_target,
        summary=summary,
    )
    record = append_learning_record_once(
        runtime,
        kind="capability_candidate",
        title=readable_title,
        summary=summary,
        scope=scope,
        loop_id=loop_id,
        step_name="distill",
        semantic_key=semantic_key,
        authority_tier=tier,
        status="candidate",
        content={
            "experiment_id": experiment_id,
            "eval_result": eval_result,
            "promotion_target": promotion_target,
            "summary": summary,
            "target_capability": target_capability,
            "rollback": "Disable promoted candidate or restore previous artifact version.",
            # Preserve an explicit hypothesis bridge with the immutable
            # candidate.  Feedback validation deliberately refuses to infer
            # this later from a goal title or capability name.
            "candidate_patch": normalized_patch,
            "capability_hypothesis": hypothesis_context,
            "candidate_bounds": candidate_bounds,
            "replay_case_ids": replay_case_ids,
        },
        meta={
            "experiment_id": experiment_id,
            "promotion_target": promotion_target,
            "target_capability": target_capability,
            "authority_tier": tier,
            "safety": scores.get("safety"),
            "regression": scores.get("regression"),
            "capability_hypothesis_id": str(hypothesis_context.get("hypothesis_id") or ""),
        },
    )
    playbook = append_learning_record_once(
        runtime,
        kind="learning_playbook",
        title=f"Playbook: {target_capability}",
        summary=summary,
        scope=scope,
        loop_id=loop_id,
        step_name="playbook",
        semantic_key=stable_semantic_key("playbook", target_capability, summary),
        authority_tier="L0",
        status="active",
        content={"candidate_id": record.record_id, "target_capability": target_capability, "summary": summary},
        meta={"candidate_id": record.record_id, "target_capability": target_capability},
    )
    return record.record_id


def _existing_candidate_by_semantic_key(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    semantic_key: str,
) -> str:
    list_by_meta = getattr(runtime.store, "list_records_by_meta_value", None)
    if callable(list_by_meta):
        records = list_by_meta(
            kinds=["capability_candidate"],
            scope=scope,
            meta_key="semantic_key",
            meta_value=semantic_key,
            limit=1,
        )
        if records:
            return str(records[0].record_id)
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    for record in runtime.store.list_records(kinds=["capability_candidate"], scope=scope_ref, limit=500):
        if str(record.meta.get("semantic_key") or "") == semantic_key:
            return str(record.record_id)
    return ""


def _candidate_title(*, target_capability: str, promotion_target: str, summary: str) -> str:
    capability = str(target_capability or "unclassified")
    artifact = _artifact_label(promotion_target)
    phrase = _short_summary(summary)
    if phrase:
        return f"Capability candidate: {capability} {artifact} - {phrase}"
    return f"Capability candidate: {capability} {artifact}"


def _artifact_label(promotion_target: str) -> str:
    labels = {
        "tool_route": "routing policy",
        "memory_rule": "recall rule",
        "eval_case": "replay case",
        "skill_draft": "skill",
        "sop_draft": "SOP",
        "source_policy": "source policy",
        "code_patch": "code patch",
    }
    return labels.get(str(promotion_target or ""), str(promotion_target or "asset").replace("_", " "))


def _short_summary(summary: str, *, limit: int = 88) -> str:
    value = " ".join(str(summary or "").split())
    generic_prefixes = (
        "generate a policy/sop/eval case and run replay",
        "produce an evidence-backed reusable asset and a replay/eval signal",
    )
    if value.lower() in generic_prefixes:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _validate_eval(eval_result: dict[str, Any], *, allow_proposal_only: bool = False) -> None:
    if eval_result.get("ok") is False:
        raise ValueError("eval ok must not be false")
    verdict = str(eval_result.get("verdict") or "")
    if verdict == "proposal_only":
        gate_bundle = eval_result.get("gate_bundle")
        if not allow_proposal_only or not isinstance(gate_bundle, dict) or gate_bundle.get("proposal_only") is not True or gate_bundle.get("qualifying") is not False:
            raise ValueError("proposal-only eval requires an explicit non-qualifying gate")
        return
    if verdict != "pass":
        raise ValueError("eval verdict must pass")
    scores = dict(eval_result.get("scores") or {})
    if float(scores.get("safety") or 0.0) < SAFETY_THRESHOLD:
        raise ValueError("safety score below threshold")
    if float(scores.get("regression") or 0.0) < REGRESSION_THRESHOLD:
        raise ValueError("regression score below threshold")


def _tier_for_target(target: str) -> str:
    value = str(target or "").lower()
    if value in {"memory_rule", "tool_route", "eval_case", "skill_draft", "sop_draft"}:
        return "L1"
    if value in {"source_policy", "prompt_policy", "system_prompt_patch", "scheduler_policy", "code_patch", "deployment_rollout"}:
        return "L2"
    return "L0"
