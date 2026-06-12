from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore


VALIDATION_SOURCE = "eimemory.skill_validation"
REPORT_TYPE = "skill_candidate_validation"
REQUIRED_GOOD_OBSERVATIONS = 3
GOOD_OUTCOMES = {"good", "success", "pass", "passed", "improved", "better"}
BAD_OUTCOMES = {"bad", "fail", "failed", "regressed", "unsafe", "error"}


def validate_skill_candidate(
    store: RuntimeStore,
    *,
    candidate_id: str | None = None,
    scope: ScopeRef | dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Replay a skill_candidate draft through deterministic local sandbox gates."""
    scope_ref = _scope(scope)
    record = _load_candidate_record(store, candidate_id=candidate_id, scope=scope_ref) if candidate is None else None
    candidate_payload = _candidate_payload(record=record, candidate=candidate)
    resolved_candidate_id = str(candidate_id or (record.record_id if record else "") or _dry_candidate_id(candidate_payload, scope_ref))
    current_status = _candidate_status(record, candidate_payload)

    checks = _sandbox_checks(candidate_payload)
    passed = all(item["pass"] for item in checks)
    reasons = [str(item["reason"]) for item in checks if not item["pass"]]
    next_status = "canary" if passed else "quarantined"
    report = {
        "ok": True,
        "candidate_id": resolved_candidate_id,
        "report_type": REPORT_TYPE,
        "proposal_status": next_status,
        "status_transition": {"from": current_status, "to": next_status},
        "pass": passed,
        "pass_rate": _pass_rate(checks),
        "reasons": reasons,
        "checks": checks,
        "stage": "sandbox_replay",
        "required_good_observations": REQUIRED_GOOD_OBSERVATIONS,
    }

    if persist and record is not None:
        _rewrite_candidate_with_validation(store, record, status=next_status, report=report)
        validation_record = _validation_result_record(report, scope=record.scope, candidate_record=record)
        store.append(validation_record)
        report["persisted"] = True
        report["validation_record_id"] = validation_record.record_id
    else:
        report["persisted"] = False
    return report


def record_skill_candidate_observation(
    store: RuntimeStore,
    *,
    candidate_id: str,
    scope: ScopeRef | dict[str, Any] | None = None,
    outcome: str,
    observation_id: str = "",
    observation_kind: str = "real",
    reason: str = "",
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record canary observations and promote only after three good outcomes."""
    scope_ref = _scope(scope)
    record = _load_candidate_record(store, candidate_id=candidate_id, scope=scope_ref)
    previous_status = str(record.status or "candidate")
    validation = _validation_state(record)
    observations = [dict(item) for item in validation.get("observations") or [] if isinstance(item, dict)]
    outcome_status = str(outcome or "").strip().lower()
    is_bad = outcome_status in BAD_OUTCOMES
    is_good = outcome_status in GOOD_OUTCOMES and not is_bad
    obs_id = str(observation_id or _observation_id(candidate_id, observations, outcome_status))

    if obs_id not in {str(item.get("observation_id") or "") for item in observations}:
        observations.append(
            {
                "observation_id": obs_id,
                "observation_kind": str(observation_kind or "real"),
                "outcome": outcome_status,
                "good": bool(is_good),
                "bad": bool(is_bad),
                "reason": str(reason or ""),
                "details": dict(details or {}),
                "observed_at": now_iso(),
            }
        )

    good_count = sum(1 for item in observations if bool(item.get("good")))
    bad_count = sum(1 for item in observations if bool(item.get("bad")))
    total_count = len(observations)
    next_status = previous_status
    passed = is_good and not is_bad
    reasons: list[str] = []

    if is_bad or bad_count > 0:
        next_status = "quarantined"
        passed = False
        reasons.append("bad_observation")
    elif previous_status == "canary" and good_count >= REQUIRED_GOOD_OBSERVATIONS:
        next_status = "active"
    elif previous_status != "canary":
        reasons.append("candidate_not_in_canary")
    else:
        next_status = "canary"

    pass_rate = round(good_count / total_count, 3) if total_count else 0.0
    report = {
        "ok": True,
        "candidate_id": candidate_id,
        "report_type": REPORT_TYPE,
        "proposal_status": next_status,
        "status_transition": {"from": previous_status, "to": next_status},
        "pass": bool(passed and next_status != "quarantined"),
        "pass_rate": pass_rate,
        "reasons": reasons,
        "stage": "canary_observation",
        "good_observation_count": good_count,
        "bad_observation_count": bad_count,
        "required_good_observations": REQUIRED_GOOD_OBSERVATIONS,
        "observation_id": obs_id,
    }
    _rewrite_candidate_with_validation(store, record, status=next_status, report=report, observations=observations)
    validation_record = _validation_result_record(report, scope=record.scope, candidate_record=record)
    store.append(validation_record)
    report["persisted"] = True
    report["validation_record_id"] = validation_record.record_id
    return report


def _sandbox_checks(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    trigger_conditions = _as_list(candidate.get("trigger_conditions"))
    steps = _as_list(candidate.get("steps"))
    acceptance = _as_list(candidate.get("acceptance_criteria"))
    source_trust = _float(candidate.get("source_trust"), default=0.0)
    risk_level = str(candidate.get("risk_level") or "").strip().lower()
    return [
        {"name": "trigger_conditions", "pass": bool(trigger_conditions), "reason": "missing_trigger_conditions"},
        {"name": "steps", "pass": len(steps) >= 2, "reason": "insufficient_steps"},
        {"name": "acceptance_criteria", "pass": bool(acceptance), "reason": "missing_acceptance_criteria"},
        {"name": "source_trust", "pass": source_trust >= 0.6, "reason": "source_trust_below_0_6"},
        {"name": "risk_level", "pass": risk_level != "high", "reason": "risk_level_high"},
    ]


def _rewrite_candidate_with_validation(
    store: RuntimeStore,
    record: RecordEnvelope,
    *,
    status: str,
    report: dict[str, Any],
    observations: list[dict[str, Any]] | None = None,
) -> None:
    existing_validation = _validation_state(record)
    merged_validation = {
        **existing_validation,
        "stage": str(report.get("proposal_status") or status),
        "last_report_type": REPORT_TYPE,
        "last_stage": str(report.get("stage") or ""),
        "last_pass": bool(report.get("pass")),
        "last_pass_rate": float(report.get("pass_rate") or 0.0),
        "last_reasons": list(report.get("reasons") or []),
        "required_good_observations": REQUIRED_GOOD_OBSERVATIONS,
        "updated_at": now_iso(),
    }
    if observations is not None:
        merged_validation["observations"] = observations
        merged_validation["good_observation_count"] = sum(1 for item in observations if bool(item.get("good")))
        merged_validation["bad_observation_count"] = sum(1 for item in observations if bool(item.get("bad")))
    else:
        merged_validation.setdefault("observations", [])
        merged_validation.setdefault("good_observation_count", 0)
        merged_validation.setdefault("bad_observation_count", 0)

    record.status = status
    record.content["status"] = status
    record.content["validation"] = _json_safe(merged_validation)
    record.meta["status"] = status
    record.meta["skill_validation"] = _json_safe(merged_validation)
    _retag(record, status)
    record.touch()
    store.rewrite(record)


def _validation_result_record(report: dict[str, Any], *, scope: ScopeRef, candidate_record: RecordEnvelope) -> RecordEnvelope:
    generated_at = now_iso()
    candidate_id = str(report.get("candidate_id") or candidate_record.record_id)
    summary = (
        f"Skill candidate validation {report.get('proposal_status')}: "
        f"pass_rate={float(report.get('pass_rate') or 0.0):.3f}, reasons={len(report.get('reasons') or [])}."
    )
    return RecordEnvelope(
        record_id=_validation_record_id(candidate_id, generated_at, report),
        kind="replay_result",
        status="active",
        title=f"Skill candidate validation: {candidate_id}",
        summary=summary,
        detail=json.dumps(_json_safe(report), ensure_ascii=False, sort_keys=True),
        content={"report": _json_safe(report)},
        tags=["skill-candidate-validation", str(report.get("proposal_status") or "")],
        links=[LinkRef(relation="validates", target_kind="skill_candidate", target_id=candidate_id)],
        evidence=[candidate_id],
        source=VALIDATION_SOURCE,
        scope=scope,
        time=TimeRef(created_at=generated_at, updated_at=generated_at, occurred_at=generated_at),
        provenance={"report_type": REPORT_TYPE, "candidate_id": candidate_id, "generated_at": generated_at},
        meta={
            "report_type": REPORT_TYPE,
            "candidate_id": candidate_id,
            "proposal_status": str(report.get("proposal_status") or ""),
            "pass": bool(report.get("pass")),
            "pass_rate": float(report.get("pass_rate") or 0.0),
        },
    )


def _load_candidate_record(store: RuntimeStore, *, candidate_id: str | None, scope: ScopeRef) -> RecordEnvelope:
    if not candidate_id:
        raise ValueError("candidate_id is required when candidate payload is not provided")
    record = store.get_by_id(str(candidate_id), scope=scope)
    if record is None:
        raise ValueError(f"skill_candidate not found: {candidate_id}")
    if record.kind != "skill_candidate":
        raise ValueError(f"record is not a skill_candidate: {candidate_id}")
    return record


def _candidate_payload(*, record: RecordEnvelope | None, candidate: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if record is not None:
        payload.update(dict(record.content or {}))
        payload.update({key: value for key, value in dict(record.meta or {}).items() if key not in payload})
        payload.setdefault("status", record.status)
        payload.setdefault("title", record.title)
        payload.setdefault("summary", record.summary)
    if candidate is not None:
        payload.update(dict(candidate))
    return payload


def _candidate_status(record: RecordEnvelope | None, candidate: dict[str, Any]) -> str:
    if record is not None:
        return str(record.status or candidate.get("status") or "candidate")
    return str(candidate.get("status") or "candidate")


def _validation_state(record: RecordEnvelope) -> dict[str, Any]:
    value = record.meta.get("skill_validation") if isinstance(record.meta, dict) else None
    return dict(value) if isinstance(value, dict) else {}


def _pass_rate(checks: list[dict[str, Any]]) -> float:
    if not checks:
        return 0.0
    return round(sum(1 for item in checks if item.get("pass")) / len(checks), 3)


def _scope(scope: ScopeRef | dict[str, Any] | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if str(item).strip()]
    return [value] if str(value).strip() else []


def _float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dry_candidate_id(candidate: dict[str, Any], scope: ScopeRef) -> str:
    payload = json.dumps({"candidate": _json_safe(candidate), "scope": asdict(scope)}, ensure_ascii=False, sort_keys=True)
    return f"dry_skill_candidate_{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _observation_id(candidate_id: str, observations: list[dict[str, Any]], outcome: str) -> str:
    payload = f"{candidate_id}\x1f{len(observations)}\x1f{outcome}"
    return f"skillobs_{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _validation_record_id(candidate_id: str, generated_at: str, report: dict[str, Any]) -> str:
    payload = json.dumps({"candidate_id": candidate_id, "generated_at": generated_at, "report": _json_safe(report)}, sort_keys=True)
    return f"skillval_{sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _retag(record: RecordEnvelope, status: str) -> None:
    tags = [tag for tag in record.tags if tag not in {"candidate", "sandbox_ready", "canary", "active", "quarantined"}]
    tags.extend(["skill-candidate", status])
    record.tags = list(dict.fromkeys(str(tag) for tag in tags if str(tag).strip()))


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, ScopeRef):
        return asdict(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
