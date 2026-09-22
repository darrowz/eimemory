"""Pure promotion gate helpers extracted from promotion_manager (god-file slice).

Behavior-preserving move: callers import from promotion_manager which re-exports.
"""
from __future__ import annotations

from math import isfinite
from typing import Any

from eimemory.models.records import RecordEnvelope

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
    return _score_value(scores, "evidence", default=0.0) >= 0.9


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
    if bool(shadow.get("notready")) or bool(injection.get("notready")):
        return False
    return bool(shadow.get("passed")) and bool(injection.get("passed"))


def _real_task_replay_gate(gate_bundle: dict[str, Any]) -> bool:
    report = gate_bundle.get("real_task_replay") or gate_bundle.get("replay_report") or gate_bundle.get("replay")
    if not isinstance(report, dict):
        return False
    if not bool(report.get("ok")):
        return False
    verdict = str(report.get("verdict") or "").strip().lower()
    sample_count = _int_value(report.get("sample_count") or report.get("case_count") or report.get("pass_count"), default=0)
    if verdict != "pass" or sample_count <= 0:
        return False
    pass_rate = _float_value(report.get("pass_rate"), default=0.0)
    threshold = _float_value(report.get("threshold"), default=0.6)
    return pass_rate >= threshold


def _closed_loop_gate(gate_bundle: dict[str, Any]) -> bool:
    closed_loop = gate_bundle.get("closed_loop") or gate_bundle.get("loop_closure") or {}
    if not isinstance(closed_loop, dict):
        return False
    doctor = closed_loop.get("doctor") or {}
    smoke = closed_loop.get("smoke") or {}
    if not isinstance(doctor, dict) or not isinstance(smoke, dict):
        return False
    return doctor.get("ok") is True and smoke.get("ok") is True


def _score_value(scores: dict[str, Any], key: str, *, default: float) -> float:
    value = scores.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    # NaN makes `number < threshold` false. Reject it, infinities, and
    # values outside the normalized score contract before gate comparisons.
    return number if isfinite(number) and 0.0 <= number <= 1.0 else 0.0


def _int_value(value: Any, *, default: int = 0) -> int:
    if value is None:
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _float_value(value: Any, *, default: float = 0.0) -> float:
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)

