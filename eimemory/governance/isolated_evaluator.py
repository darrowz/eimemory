from __future__ import annotations

from dataclasses import asdict
import json
import os
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import RecordEnvelope, ScopeRef


SCHEMA_VERSION = "isolated_evaluator.v1"
DEFAULT_GENERATOR_MODEL = "gpt"
DEFAULT_EVALUATOR_MODEL = "minimax"
DEFAULT_STOP_JUDGE_MODEL = "minimax"


def model_roles(
    *,
    generator_model: str | None = None,
    evaluator_model: str | None = None,
    stop_judge_model: str | None = None,
) -> dict[str, str]:
    return {
        "generator_model": _model_id(generator_model, env_name="EIMEMORY_GENERATOR_MODEL", default=DEFAULT_GENERATOR_MODEL),
        "evaluator_model": _model_id(evaluator_model, env_name="EIMEMORY_EVALUATOR_MODEL", default=DEFAULT_EVALUATOR_MODEL),
        "stop_judge_model": _model_id(stop_judge_model, env_name="EIMEMORY_STOP_JUDGE_MODEL", default=DEFAULT_STOP_JUDGE_MODEL),
    }


def build_evaluation_packet(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str,
    goal: dict[str, Any] | None,
    candidate_kind: str,
    artifact: dict[str, Any] | None,
    generator_claim: str = "",
    replay_gate: dict[str, Any] | None = None,
    real_task_replay: dict[str, Any] | None = None,
    verification_results: list[dict[str, Any]] | None = None,
    generator_model: str | None = None,
    evaluator_model: str | None = None,
    stop_judge_model: str | None = None,
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    roles = model_roles(
        generator_model=generator_model,
        evaluator_model=evaluator_model,
        stop_judge_model=stop_judge_model,
    )
    goal_payload = dict(goal or {})
    artifact_payload = dict(artifact or {})
    replay_payload = dict(real_task_replay or {})
    replay_gate_payload = dict(replay_gate or {})
    verification_payload = [dict(item) for item in verification_results or [] if isinstance(item, dict)]
    evaluator_context = {
        "goal": _goal_context(goal_payload),
        "candidate_kind": str(candidate_kind or ""),
        "artifact": _artifact_context(artifact_payload),
        "replay_gate": replay_gate_payload,
        "real_task_replay": replay_payload,
        "verification_results": verification_payload,
        "evidence_priority": ["real_execution", "replay", "health_canary", "static_check", "judge", "generator_claim"],
    }
    semantic_key = stable_semantic_key(
        "evaluation_packet",
        loop_id,
        candidate_kind,
        roles["generator_model"],
        roles["evaluator_model"],
        roles["stop_judge_model"],
        goal_payload.get("semantic_key") or goal_payload.get("title") or "",
        artifact_payload.get("summary") or artifact_payload.get("policy") or "",
        _evidence_signature(
            generator_claim=generator_claim,
            replay_gate=replay_gate_payload,
            real_task_replay=replay_payload,
            verification_results=verification_payload,
        ),
    )
    record = append_learning_record_once(
        runtime,
        kind="evaluation_packet",
        title=f"Evaluation packet: {candidate_kind or 'candidate'}",
        summary=str(_first_text(goal_payload.get("title"), goal_payload.get("question"), artifact_payload.get("summary"), candidate_kind)),
        scope=scope_ref,
        loop_id=loop_id,
        step_name="evaluation_packet",
        semantic_key=semantic_key,
        authority_tier="L0",
        status="candidate",
        source="eimemory.isolated_evaluator",
        content={
            "schema_version": SCHEMA_VERSION,
            "loop_id": loop_id,
            "model_roles": roles,
            "generator_claim": {
                "isolated": True,
                "text": str(generator_claim or ""),
            },
            "evaluator_context": evaluator_context,
            "debt_metrics": _packet_debt_metrics(evaluator_context=evaluator_context, generator_claim=generator_claim),
        },
        meta={
            "schema_version": SCHEMA_VERSION,
            "generator_model": roles["generator_model"],
            "evaluator_model": roles["evaluator_model"],
            "stop_judge_model": roles["stop_judge_model"],
            "candidate_kind": str(candidate_kind or ""),
        },
    )
    return record


def run_isolated_evaluator(
    runtime: Any,
    packet: RecordEnvelope | str,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "",
) -> RecordEnvelope:
    packet_record = _resolve_record(runtime, packet)
    if packet_record.kind != "evaluation_packet":
        raise ValueError(f"expected evaluation_packet, got {packet_record.kind}")
    content = dict(packet_record.content or {})
    roles = dict(content.get("model_roles") or {})
    evaluator_context = dict(content.get("evaluator_context") or {})
    blocked_reasons = _verdict_blocked_reasons(roles=roles, evaluator_context=evaluator_context)
    verdict = "pass" if not blocked_reasons else "fail"
    debt_metrics = _verdict_debt_metrics(blocked_reasons=blocked_reasons, packet_debt=dict(content.get("debt_metrics") or {}))
    semantic_key = stable_semantic_key("evaluator_verdict", packet_record.record_id, verdict, ",".join(blocked_reasons))
    record = append_learning_record_once(
        runtime,
        kind="evaluator_verdict",
        title=f"Evaluator verdict: {verdict}",
        summary="Independent evaluator passed the packet." if verdict == "pass" else f"Independent evaluator blocked promotion: {', '.join(blocked_reasons)}",
        scope=scope or packet_record.scope,
        loop_id=loop_id or str(content.get("loop_id") or ""),
        step_name="evaluator_verdict",
        semantic_key=semantic_key,
        authority_tier="L0",
        status=verdict,
        source="eimemory.isolated_evaluator",
        content={
            "schema_version": SCHEMA_VERSION,
            "loop_id": loop_id or str(content.get("loop_id") or ""),
            "packet_id": packet_record.record_id,
            "verdict": verdict,
            "promotion_allowed": verdict == "pass",
            "skeptical_default": True,
            "blocked_reasons": blocked_reasons,
            "model_roles": roles,
            "real_execution": _real_execution_summary(evaluator_context),
            "debt_metrics": debt_metrics,
        },
        meta={
            "schema_version": SCHEMA_VERSION,
            "packet_id": packet_record.record_id,
            "verdict": verdict,
            "promotion_allowed": verdict == "pass",
        },
        evidence=[packet_record.record_id],
    )
    return record


def judge_stop_condition(
    runtime: Any,
    verdict: RecordEnvelope | str,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "",
    stop_judge_model: str | None = None,
) -> RecordEnvelope:
    verdict_record = _resolve_record(runtime, verdict)
    if verdict_record.kind != "evaluator_verdict":
        raise ValueError(f"expected evaluator_verdict, got {verdict_record.kind}")
    verdict_content = dict(verdict_record.content or {})
    roles = dict(verdict_content.get("model_roles") or {})
    roles["stop_judge_model"] = _model_id(stop_judge_model, env_name="EIMEMORY_STOP_JUDGE_MODEL", default=roles.get("stop_judge_model") or DEFAULT_STOP_JUDGE_MODEL)
    stop_blocked_reasons = _stop_judge_blocked_reasons(roles)
    blocked_reasons = list(verdict_content.get("blocked_reasons") or []) + stop_blocked_reasons
    decision = _stop_decision(verdict_content, stop_blocked_reasons=stop_blocked_reasons)
    promotion_allowed = decision == "stop" and bool(verdict_content.get("promotion_allowed")) and not stop_blocked_reasons
    semantic_key = stable_semantic_key(
        "stop_judgment",
        verdict_record.record_id,
        roles["stop_judge_model"],
        decision,
        ",".join(blocked_reasons),
    )
    record = append_learning_record_once(
        runtime,
        kind="stop_judgment",
        title=f"Stop judgment: {decision}",
        summary=f"Stop judge decision: {decision}",
        scope=scope or verdict_record.scope,
        loop_id=loop_id or str(verdict_content.get("loop_id") or ""),
        step_name="stop_judgment",
        semantic_key=semantic_key,
        authority_tier="L0",
        status=decision,
        source="eimemory.isolated_evaluator",
        content={
            "schema_version": SCHEMA_VERSION,
            "loop_id": loop_id or str(verdict_content.get("loop_id") or ""),
            "verdict_id": verdict_record.record_id,
            "decision": decision,
            "promotion_allowed": promotion_allowed,
            "blocked_reasons": blocked_reasons,
            "model_roles": roles,
            "debt_metrics": dict(verdict_content.get("debt_metrics") or {}),
        },
        meta={
            "schema_version": SCHEMA_VERSION,
            "verdict_id": verdict_record.record_id,
            "decision": decision,
            "promotion_allowed": promotion_allowed,
            "stop_judge_model": roles["stop_judge_model"],
        },
        evidence=[verdict_record.record_id],
    )
    return record


def run_isolated_evaluator_harness(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str = "isolated_evaluator_smoke",
    goal: dict[str, Any] | None = None,
    candidate_kind: str = "eval_case",
    artifact: dict[str, Any] | None = None,
    generator_claim: str = "Generator claim is isolated from evaluator context.",
    replay_gate: dict[str, Any] | None = None,
    real_task_replay: dict[str, Any] | None = None,
    generator_model: str | None = None,
    evaluator_model: str | None = None,
    stop_judge_model: str | None = None,
) -> dict[str, Any]:
    replay_gate_payload = replay_gate if replay_gate is not None else {"ok": True, "pass_rate": 1.0, "sample_count": 1}
    replay_payload = real_task_replay if real_task_replay is not None else {"ok": True, "verdict": "pass", "pass_rate": 1.0, "pass_count": 1, "fail_count": 0}
    packet = build_evaluation_packet(
        runtime,
        scope=scope,
        loop_id=loop_id,
        goal=goal or {"title": "Isolated evaluator smoke", "target_capability": "governance.evaluation"},
        candidate_kind=candidate_kind,
        artifact=artifact or {"summary": "smoke artifact"},
        generator_claim=generator_claim,
        replay_gate=replay_gate_payload,
        real_task_replay=replay_payload,
        generator_model=generator_model,
        evaluator_model=evaluator_model,
        stop_judge_model=stop_judge_model,
    )
    verdict = run_isolated_evaluator(runtime, packet, scope=scope, loop_id=loop_id)
    judgment = judge_stop_condition(runtime, verdict, scope=scope, loop_id=loop_id, stop_judge_model=stop_judge_model)
    return {
        "ok": True,
        "packet_id": packet.record_id,
        "verdict_id": verdict.record_id,
        "stop_judgment_id": judgment.record_id,
        "verdict": verdict.content.get("verdict"),
        "decision": judgment.content.get("decision"),
        "promotion_allowed": bool(judgment.content.get("promotion_allowed")),
        "model_roles": dict(judgment.content.get("model_roles") or {}),
        "debt_metrics": dict(verdict.content.get("debt_metrics") or {}),
    }


def _verdict_blocked_reasons(*, roles: dict[str, Any], evaluator_context: dict[str, Any]) -> list[str]:
    blocked: list[str] = []
    if str(roles.get("generator_model") or "").strip() == str(roles.get("evaluator_model") or "").strip():
        blocked.append("model_not_isolated")
    real_execution = _real_execution_summary(evaluator_context)
    if real_execution["command_failed_count"] > 0:
        blocked.append("verification_command_failed")
    elif real_execution["replay_status_passed"] and not real_execution["replay_quality_passed"]:
        blocked.append("insufficient_replay_quality")
    elif not real_execution["passed"]:
        blocked.append("missing_real_execution_evidence")
    if _generator_claim_leaked(evaluator_context):
        blocked.append("generator_claim_visible_to_evaluator")
    return blocked


def _real_execution_summary(evaluator_context: dict[str, Any]) -> dict[str, Any]:
    replay_gate = dict(evaluator_context.get("replay_gate") or {})
    replay = dict(evaluator_context.get("real_task_replay") or {})
    verifications = [dict(item) for item in evaluator_context.get("verification_results") or [] if isinstance(item, dict)]
    replay_status_passed = _status_passed(replay_gate) and _status_passed(replay)
    sample_count = _replay_sample_count(replay_gate=replay_gate, replay=replay)
    fail_count = _int_value(_first_present(replay, "fail_count", "failed_count", "failures"))
    pass_rate = _replay_pass_rate(replay_gate=replay_gate, replay=replay, replay_status_passed=replay_status_passed)
    threshold = _float_value(_first_present(replay_gate, "threshold", "min_pass_rate"), default=1.0)
    replay_quality_passed = replay_status_passed and sample_count > 0 and fail_count == 0 and pass_rate >= threshold
    command_total = len(verifications)
    command_passed_count = sum(1 for item in verifications if _verification_result_passed(item))
    command_failed_count = command_total - command_passed_count
    command_passed = command_total > 0 and command_failed_count == 0
    return {
        "passed": bool(replay_quality_passed or command_passed),
        "replay_passed": bool(replay_quality_passed),
        "replay_status_passed": bool(replay_status_passed),
        "replay_quality_passed": bool(replay_quality_passed),
        "command_passed": bool(command_passed),
        "command_total": command_total,
        "command_passed_count": command_passed_count,
        "command_failed_count": command_failed_count,
        "sample_count": sample_count,
        "fail_count": fail_count,
        "pass_rate": pass_rate,
        "threshold": threshold,
    }


def _stop_decision(verdict_content: dict[str, Any], *, stop_blocked_reasons: list[str] | None = None) -> str:
    stop_blocked = set(str(item) for item in stop_blocked_reasons or [])
    if stop_blocked:
        return "quarantine"
    if verdict_content.get("promotion_allowed") is True and not verdict_content.get("blocked_reasons"):
        return "stop"
    blocked = set(str(item) for item in verdict_content.get("blocked_reasons") or [])
    if "model_not_isolated" in blocked or "generator_claim_visible_to_evaluator" in blocked:
        return "quarantine"
    if "missing_real_execution_evidence" in blocked or "insufficient_replay_quality" in blocked:
        return "continue"
    return "require_human"


def _stop_judge_blocked_reasons(roles: dict[str, Any]) -> list[str]:
    generator_model = str(roles.get("generator_model") or "").strip()
    stop_judge_model = str(roles.get("stop_judge_model") or "").strip()
    if generator_model and stop_judge_model and generator_model == stop_judge_model:
        return ["stop_judge_not_isolated"]
    return []


def _packet_debt_metrics(*, evaluator_context: dict[str, Any], generator_claim: str) -> dict[str, int]:
    real_execution = _real_execution_summary(evaluator_context)
    return {
        "verification_debt": 0 if real_execution["passed"] else 1,
        "unverified_generator_claims": 1 if str(generator_claim or "").strip() else 0,
        "comprehension_rot": 0,
        "cognitive_surrender": 0,
        "token_blowout": 0,
    }


def _verdict_debt_metrics(*, blocked_reasons: list[str], packet_debt: dict[str, Any]) -> dict[str, int]:
    debt = {key: _int_value(value) for key, value in packet_debt.items()}
    if not blocked_reasons:
        debt["verification_debt"] = 0
        debt["unverified_generator_claims"] = 0
    return debt


def _goal_context(goal: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": str(goal.get("title") or goal.get("question") or ""),
        "target_capability": str(goal.get("target_capability") or ""),
        "success_criteria": str(goal.get("success_criteria") or ""),
        "semantic_key": str(goal.get("semantic_key") or ""),
    }


def _artifact_context(artifact: dict[str, Any]) -> dict[str, Any]:
    file_updates = artifact.get("file_updates") or artifact.get("files") or []
    if not isinstance(file_updates, list):
        file_updates = []
    return {
        "summary": str(_first_text(artifact.get("summary"), artifact.get("policy"), artifact.get("success_criteria"))),
        "promotion_target": str(artifact.get("promotion_target") or ""),
        "target_capability": str(artifact.get("target_capability") or ""),
        "file_update_count": sum(1 for item in file_updates if isinstance(item, dict)),
        "replay_case_ids": [str(item) for item in artifact.get("replay_case_ids") or []],
    }


def _generator_claim_leaked(evaluator_context: dict[str, Any]) -> bool:
    text = str(evaluator_context.get("generator_claim") or "")
    if text:
        return True
    return "definitely correct" in str(evaluator_context).lower()


def _resolve_record(runtime: Any, record_or_id: RecordEnvelope | str) -> RecordEnvelope:
    if isinstance(record_or_id, RecordEnvelope):
        return record_or_id
    record = runtime.store.get_by_id(str(record_or_id))
    if record is None:
        raise ValueError(f"record not found: {record_or_id}")
    return record


def _model_id(value: str | None, *, env_name: str, default: str) -> str:
    return str(value or os.environ.get(env_name) or default).strip() or default


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload.get(key) is not None:
            return payload.get(key)
    return None


def _evidence_signature(
    *,
    generator_claim: str,
    replay_gate: dict[str, Any],
    real_task_replay: dict[str, Any],
    verification_results: list[dict[str, Any]],
) -> str:
    payload = {
        "generator_claim": str(generator_claim or ""),
        "replay_gate": replay_gate,
        "real_task_replay": real_task_replay,
        "verification_results": verification_results,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _replay_sample_count(*, replay_gate: dict[str, Any], replay: dict[str, Any]) -> int:
    explicit = _first_present(replay_gate, "sample_count", "case_count", "pass_count")
    if explicit is None:
        explicit = _first_present(replay, "sample_count", "case_count")
    if explicit is not None:
        return _int_value(explicit)
    pass_count = _int_value(_first_present(replay, "pass_count", "passed_count", "passes"))
    fail_count = _int_value(_first_present(replay, "fail_count", "failed_count", "failures"))
    return pass_count + fail_count


def _replay_pass_rate(*, replay_gate: dict[str, Any], replay: dict[str, Any], replay_status_passed: bool) -> float:
    rates = [
        _float_value(value)
        for value in (_first_present(replay_gate, "pass_rate"), _first_present(replay, "pass_rate"))
        if value is not None
    ]
    if rates:
        return min(rates)
    return 1.0 if replay_status_passed else 0.0


def _exit_code(item: dict[str, Any]) -> int:
    raw = item["returncode"] if "returncode" in item else item.get("exit_code", 1)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 1


def _verification_result_passed(item: dict[str, Any]) -> bool:
    if item.get("ok") is True:
        return True
    if item.get("ok") is False:
        return False
    return _exit_code(item) == 0


SUCCESS_LABELS = {"pass", "passed", "success", "succeeded", "ok", "true", "green"}
FAILURE_LABELS = {"fail", "failed", "failure", "error", "blocked", "reject", "rejected", "false", "red"}


def _status_passed(payload: dict[str, Any]) -> bool:
    if not payload:
        return False
    explicit_ok = payload.get("ok")
    if explicit_ok is False:
        return False
    explicit_success = payload.get("success")
    if explicit_success is False:
        return False
    for key in ("verdict", "status", "result", "decision"):
        if key not in payload:
            continue
        label = str(payload.get(key) or "").strip().lower()
        if label in FAILURE_LABELS:
            return False
        if label in SUCCESS_LABELS:
            return True
    if explicit_ok is True or explicit_success is True:
        return True
    return False


__all__ = [
    "DEFAULT_EVALUATOR_MODEL",
    "DEFAULT_GENERATOR_MODEL",
    "DEFAULT_STOP_JUDGE_MODEL",
    "SCHEMA_VERSION",
    "build_evaluation_packet",
    "judge_stop_condition",
    "model_roles",
    "run_isolated_evaluator",
    "run_isolated_evaluator_harness",
]
