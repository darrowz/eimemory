from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from eimemory.governance.learning_eval import REGRESSION_THRESHOLD, SAFETY_THRESHOLD
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef

POLICY_TARGETS = {"tool_route", "prompt_policy", "system_prompt_patch"}
PLAYBOOK_TARGETS = {"eval_case", "skill_draft", "sop_draft", "source_policy"}
CODE_ASSET_TARGETS = {"code_patch"}
UNSUPPORTED_ACTIVE_TARGETS = {"deployment_rollout", "scheduler_policy"}


def promote_candidate(
    runtime: Any,
    *,
    candidate_id: str,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "manual",
    apply: bool = True,
    eval_result: dict[str, Any] | None = None,
    health: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None or candidate.kind != "capability_candidate":
        raise ValueError(f"capability candidate not found: {candidate_id}")
    tier = str(candidate.meta.get("authority_tier") or candidate.content.get("authority_tier") or "L0").upper()
    if tier == "L3":
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="blocked", action="blocked_l3", eval_result=eval_result or {}, health=health or {})
        return {"ok": False, "applied": False, "blocked_reason": "l3_requires_approval", "promotion_request_id": request_id}
    eval_payload = eval_result or candidate.content.get("eval_result") or {}
    health_payload = health or {"ok": True}
    gate = _rollout_gate(eval_payload, health_payload, tier=tier, candidate=candidate)
    if not gate["ok"]:
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="blocked", action="gate_failed", eval_result=eval_payload, health=health_payload, gate=gate)
        return {"ok": False, "applied": False, "blocked_reason": ",".join(gate["blocked_reasons"]), "promotion_request_id": request_id}
    if not apply:
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="candidate", action="dry_run", eval_result=eval_payload, health=health_payload, gate=gate)
        return {"ok": True, "applied": False, "dry_run": True, "promotion_request_id": request_id}

    side_effect = _apply_candidate(runtime, candidate, scope=scope, loop_id=loop_id, eval_result=eval_payload, gate=gate)
    if not side_effect.get("ok"):
        request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="blocked", action="adapter_failed", eval_result=eval_payload, health=health_payload, gate=gate, side_effect=side_effect)
        return {
            "ok": False,
            "applied": False,
            "blocked_reason": str(side_effect.get("blocked_reason") or "rollout_adapter_failed"),
            "promotion_request_id": request_id,
            "side_effect": side_effect,
        }

    candidate.status = "promoted"
    candidate.meta["promoted_by"] = "eimemory.autonomous_learning"
    candidate.meta["promotion_tier"] = tier
    candidate.meta["applied_artifact_ids"] = list(side_effect.get("applied_artifact_ids") or [])
    runtime.store.rewrite(candidate)
    request_id = _promotion_record(runtime, candidate, scope=scope, loop_id=loop_id, status="promoted", action="applied", eval_result=eval_payload, health=health_payload, gate=gate, side_effect=side_effect)
    return {
        "ok": True,
        "applied": True,
        "authority_tier": tier,
        "candidate_id": candidate_id,
        "promotion_request_id": request_id,
        "side_effect": side_effect,
        "applied_artifact_ids": list(side_effect.get("applied_artifact_ids") or []),
        "rollback": candidate.content.get("rollback") or "disable candidate",
    }


def _rollout_gate(eval_result: dict[str, Any], health: dict[str, Any], *, tier: str, candidate: RecordEnvelope) -> dict[str, Any]:
    scores = dict(eval_result.get("scores") or {})
    blocked = []
    if str(eval_result.get("verdict") or "pass") != "pass":
        blocked.append("eval_not_pass")
    if float(scores.get("safety") or (1.0 if tier in {"L0", "L1"} else 0.0)) < (0.95 if tier == "L2" else SAFETY_THRESHOLD):
        blocked.append("safety_gate")
    if float(scores.get("regression") or (1.0 if tier in {"L0", "L1"} else 0.0)) < (0.95 if tier == "L2" else REGRESSION_THRESHOLD):
        blocked.append("regression_gate")
    if tier == "L2" and not health.get("ok", False):
        blocked.append("health_gate")
    gate_bundle = _gate_bundle(candidate, eval_result)
    if tier == "L2":
        if not gate_bundle:
            blocked.append("gate_bundle_missing")
        if not _evidence_gate(gate_bundle, scores):
            blocked.append("evidence_gate")
        if not _rollback_gate(gate_bundle):
            blocked.append("rollback_gate")
        if not _canary_gate(gate_bundle):
            blocked.append("canary_gate")
        if int(gate_bundle.get("timeout_seconds") or 0) <= 0:
            blocked.append("timeout_gate")
        if not bool((gate_bundle.get("audit") or {}).get("enabled")):
            blocked.append("audit_gate")
        target = _promotion_target(candidate)
        if target in {"prompt_policy", "system_prompt_patch"} and not _prompt_safety_gate(gate_bundle):
            blocked.append("prompt_safety_gate")
    return {"ok": not blocked, "blocked_reasons": blocked, "gate_bundle": gate_bundle}


def _apply_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    target = _promotion_target(candidate)
    patch = _candidate_patch(runtime, candidate, scope=scope)
    if target in UNSUPPORTED_ACTIVE_TARGETS:
        return {"ok": False, "blocked_reason": f"unsupported_rollout_adapter:{target}", "promotion_target": target}
    if target in POLICY_TARGETS:
        return _apply_policy_candidate(runtime, candidate, patch, scope=scope, loop_id=loop_id, eval_result=eval_result, gate=gate)
    if target in CODE_ASSET_TARGETS:
        return _apply_code_patch_candidate(runtime, candidate, patch, scope=scope, loop_id=loop_id, eval_result=eval_result, gate=gate)
    if target == "memory_rule":
        return _apply_memory_rule_candidate(runtime, candidate, patch, scope=scope)
    if target in PLAYBOOK_TARGETS or target in {"", "unknown"}:
        return _apply_playbook_candidate(runtime, candidate, patch, scope=scope, loop_id=loop_id, eval_result=eval_result, gate=gate)
    return {"ok": False, "blocked_reason": f"unsupported_rollout_adapter:{target}", "promotion_target": target}


def _apply_policy_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    if not hasattr(runtime, "upsert_intent_pattern"):
        return {"ok": False, "blocked_reason": "intent_pattern_adapter_unavailable"}
    pattern_id = str(patch.get("id") or f"al-{stable_semantic_key('intent_pattern', candidate.record_id)[:20]}")
    target_capability = str(candidate.meta.get("target_capability") or candidate.content.get("target_capability") or patch.get("target_capability") or "proactive.judgment")
    policy_lines = _list_text(patch.get("execution_policy")) or _list_text(patch.get("policy")) or [candidate.summary]
    pattern = str(patch.get("pattern") or patch.get("user_phrase") or target_capability or candidate.summary).strip()
    payload = {
        "id": pattern_id,
        "pattern": pattern,
        "default_event_type": str(patch.get("default_event_type") or patch.get("event_type") or _event_type_for_capability(target_capability)),
        "interpreted_intent": str(patch.get("interpreted_intent") or candidate.summary or patch.get("summary") or "Apply learned execution policy."),
        "execution_policy": policy_lines,
        "first_questions": _list_text(patch.get("first_questions")),
        "ask_first_boundaries": _list_text(patch.get("ask_first_boundaries")),
        "success_criteria": str(patch.get("success_criteria") or patch.get("summary") or candidate.summary),
        "confidence": min(0.95, max(0.75, float((eval_result.get("scores") or {}).get("confidence") or 0.8))),
        "source_opportunity_id": candidate.record_id,
        "source_opportunity": {
            "opportunity_id": candidate.record_id,
            "opportunity_type": "autonomous_learning_policy",
            "promotion_target": _promotion_target(candidate),
            "loop_id": loop_id,
        },
        "trust_report": {"ok": True, "gate": gate},
        "replay_report": {"ok": True, "eval_result": eval_result},
        "is_auto": True,
    }
    result = runtime.upsert_intent_pattern(payload, scope=_scope_dict(scope or candidate.scope))
    if str(result.get("status") or "active") != "active":
        return {
            "ok": False,
            "blocked_reason": str(result.get("_promotion_budget_decision") or "policy_not_active"),
            "promotion_target": _promotion_target(candidate),
            "applied_artifact_ids": [],
            "adapter_result": result,
        }
    return {
        "ok": True,
        "promotion_target": _promotion_target(candidate),
        "adapter": "intent_pattern",
        "applied_artifact_ids": [str(result.get("id") or pattern_id)],
        "adapter_result": result,
    }


def _apply_memory_rule_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    if not hasattr(runtime, "evolution") or not hasattr(runtime.evolution, "store_rule"):
        return {"ok": False, "blocked_reason": "rule_adapter_unavailable"}
    rule = runtime.evolution.store_rule(
        title=str(patch.get("title") or candidate.title),
        summary=str(patch.get("summary") or candidate.summary),
        task_type=str(patch.get("task_type") or patch.get("target_capability") or candidate.meta.get("target_capability") or "memory.recall"),
        retrieval_policy=dict(patch.get("retrieval_policy") or {"learned_policy": candidate.summary}),
        response_policy=dict(patch.get("response_policy") or {}),
        scope=_scope_dict(scope or candidate.scope),
        status="active",
    )
    return {"ok": True, "promotion_target": "memory_rule", "adapter": "rule", "applied_artifact_ids": [rule.record_id]}


def _apply_code_patch_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    root = Path(runtime.store.root) / "state" / "autonomous_learning" / "reviewable_patches"
    root.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in candidate.record_id)
    patch_path = root / f"{safe_id}.patch"
    metadata_path = root / f"{safe_id}.json"
    diff_text = str(patch.get("diff") or patch.get("reviewable_diff") or "").strip()
    if not diff_text:
        diff_text = _reviewable_diff_text(candidate, patch)
    patch_path.write_text(diff_text, encoding="utf-8")
    metadata = {
        "candidate_id": candidate.record_id,
        "loop_id": loop_id,
        "promotion_target": "code_patch",
        "target_capability": str(candidate.meta.get("target_capability") or patch.get("target_capability") or "code.implementation"),
        "summary": str(patch.get("summary") or candidate.summary),
        "artifact_path": str(patch_path),
        "eval_result": eval_result,
        "gate": gate,
        "review_status": "ready_for_machine_review",
        "production_applied": False,
        "next_action": "open_review_branch_or_pr_from_patch_artifact",
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    record = append_learning_record_once(
        runtime,
        kind="learning_playbook",
        title=f"Reviewable code patch: {candidate.title}",
        summary=f"Prepared reviewable code patch artifact for {candidate.record_id}",
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="code_patch_asset",
        semantic_key=stable_semantic_key("reviewable_code_patch", candidate.record_id, diff_text),
        authority_tier="L2",
        status="active",
        content={"candidate_id": candidate.record_id, "patch_artifact": metadata, "diff_excerpt": diff_text[:1200]},
        meta={
            "candidate_id": candidate.record_id,
            "promotion_target": "code_patch",
            "artifact_path": str(patch_path),
            "metadata_path": str(metadata_path),
            "production_applied": False,
        },
    )
    return {
        "ok": True,
        "promotion_target": "code_patch",
        "adapter": "reviewable_code_patch",
        "applied_artifact_ids": [record.record_id, str(patch_path)],
        "artifact_path": str(patch_path),
        "metadata_path": str(metadata_path),
        "production_applied": False,
    }


def _apply_playbook_candidate(
    runtime: Any,
    candidate: RecordEnvelope,
    patch: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    eval_result: dict[str, Any],
    gate: dict[str, Any],
) -> dict[str, Any]:
    record = append_learning_record_once(
        runtime,
        kind="learning_playbook",
        title=f"Activated playbook: {candidate.title}",
        summary=str(patch.get("summary") or candidate.summary),
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="promotion_apply",
        semantic_key=stable_semantic_key("activated_playbook", candidate.record_id, patch),
        authority_tier=str(candidate.meta.get("authority_tier") or "L0"),
        status="active",
        content={"candidate_id": candidate.record_id, "patch": patch, "eval_result": eval_result, "gate": gate},
        meta={"candidate_id": candidate.record_id, "promotion_target": _promotion_target(candidate)},
    )
    return {"ok": True, "promotion_target": _promotion_target(candidate), "adapter": "learning_playbook", "applied_artifact_ids": [record.record_id]}


def _reviewable_diff_text(candidate: RecordEnvelope, patch: dict[str, Any]) -> str:
    summary = str(patch.get("summary") or candidate.summary or "Autonomous learning code patch candidate")
    policy = str(patch.get("policy") or patch.get("success_criteria") or summary)
    capability = str(patch.get("target_capability") or candidate.meta.get("target_capability") or "code.implementation")
    lines = [
        "diff --git a/AUTONOMOUS_LEARNING_CANDIDATE.md b/AUTONOMOUS_LEARNING_CANDIDATE.md",
        "new file mode 100644",
        "index 0000000..0000000",
        "--- /dev/null",
        "+++ b/AUTONOMOUS_LEARNING_CANDIDATE.md",
        "@@ -0,0 +1,12 @@",
        "+# Autonomous Learning Code Candidate",
        f"+Candidate: {candidate.record_id}",
        f"+Capability: {capability}",
        f"+Summary: {summary}",
        "+",
        "+This patch artifact is generated for machine/code review only.",
        "+It is not applied to production automatically.",
        "+",
        "+Proposed action:",
        f"+{policy}",
        "+",
        "+Verification:",
        "+- python -m compileall eimemory scripts",
        "+- python -m pytest -q",
    ]
    return "\n".join(lines) + "\n"


def _candidate_patch(runtime: Any, candidate: RecordEnvelope, *, scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    content = candidate.content if isinstance(candidate.content, dict) else {}
    direct = content.get("candidate_patch") if isinstance(content.get("candidate_patch"), dict) else {}
    if direct:
        return dict(direct)
    experiment_id = str(content.get("experiment_id") or candidate.meta.get("experiment_id") or "")
    if experiment_id:
        experiment = runtime.store.get_by_id(experiment_id, scope=scope or candidate.scope)
        if experiment is not None and isinstance(experiment.content, dict):
            patch = experiment.content.get("candidate_patch")
            if isinstance(patch, dict):
                return dict(patch)
    return {
        "summary": str(content.get("summary") or candidate.summary),
        "target_capability": str(content.get("target_capability") or candidate.meta.get("target_capability") or ""),
        "policy": str(content.get("summary") or candidate.summary),
    }


def _gate_bundle(candidate: RecordEnvelope, eval_result: dict[str, Any]) -> dict[str, Any]:
    for value in (
        eval_result.get("gate_bundle"),
        candidate.content.get("gate_bundle") if isinstance(candidate.content, dict) else None,
        (candidate.content.get("eval_result") or {}).get("gate_bundle") if isinstance(candidate.content, dict) and isinstance(candidate.content.get("eval_result"), dict) else None,
    ):
        if isinstance(value, dict):
            return dict(value)
    return {}


def _evidence_gate(gate_bundle: dict[str, Any], scores: dict[str, Any]) -> bool:
    evidence = gate_bundle.get("evidence")
    tiers = [str(item.get("tier") or "").upper() for item in evidence if isinstance(item, dict)] if isinstance(evidence, list) else []
    if any(tier in {"T0", "T1"} for tier in tiers):
        return True
    if sum(1 for tier in tiers if tier in {"T2", "T3"}) >= 2:
        return True
    return float(scores.get("evidence") or 0.0) >= 0.9


def _rollback_gate(gate_bundle: dict[str, Any]) -> bool:
    rollback = gate_bundle.get("rollback") if isinstance(gate_bundle.get("rollback"), dict) else {}
    return bool(rollback.get("executable") or rollback.get("available"))


def _canary_gate(gate_bundle: dict[str, Any]) -> bool:
    canary = gate_bundle.get("canary") if isinstance(gate_bundle.get("canary"), dict) else {}
    blast_radius = str(canary.get("blast_radius") or "").lower()
    return bool(canary.get("passed")) and blast_radius in {"single_scope", "single_workspace", "service_local", "low"}


def _prompt_safety_gate(gate_bundle: dict[str, Any]) -> bool:
    shadow = gate_bundle.get("prompt_shadow_eval") if isinstance(gate_bundle.get("prompt_shadow_eval"), dict) else {}
    injection = gate_bundle.get("prompt_injection_check") if isinstance(gate_bundle.get("prompt_injection_check"), dict) else {}
    return bool(shadow.get("passed")) and bool(injection.get("passed"))


def _promotion_target(candidate: RecordEnvelope) -> str:
    return str(candidate.meta.get("promotion_target") or candidate.content.get("promotion_target") or "").strip().lower()


def _scope_dict(scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    if isinstance(scope, ScopeRef):
        return asdict(scope)
    return dict(scope or {})


def _event_type_for_capability(capability: str) -> str:
    value = capability.lower()
    if "routing" in value or "tool" in value:
        return "tool_routing"
    if "recall" in value or "memory" in value:
        return "memory_recall"
    if "code" in value:
        return "code_implementation"
    return "communication"


def _list_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return []


def _promotion_record(
    runtime: Any,
    candidate: RecordEnvelope,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    status: str,
    action: str,
    eval_result: dict[str, Any],
    health: dict[str, Any],
    gate: dict[str, Any] | None = None,
    side_effect: dict[str, Any] | None = None,
) -> str:
    semantic_key = stable_semantic_key("promotion", candidate.record_id, action, status)
    record = append_learning_record_once(
        runtime,
        kind="promotion_request",
        title=f"Promotion {action}: {candidate.title}",
        summary=candidate.summary,
        scope=scope or candidate.scope,
        loop_id=loop_id,
        step_name="promotion",
        semantic_key=semantic_key,
        authority_tier=str(candidate.meta.get("authority_tier") or "L0"),
        status=status,
        content={
            "candidate_id": candidate.record_id,
            "action": action,
            "eval_result": eval_result,
            "health": health,
            "gate": gate or {},
            "side_effect": side_effect or {},
            "rollback": candidate.content.get("rollback") or "disable candidate",
        },
        meta={
            "candidate_id": candidate.record_id,
            "action": action,
            "gate_ok": bool((gate or {"ok": status == "promoted"}).get("ok")),
            "side_effect_ok": bool((side_effect or {}).get("ok")),
        },
    )
    return record.record_id
