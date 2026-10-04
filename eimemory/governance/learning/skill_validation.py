from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.knowledge.safety import evaluate_knowledge_safety
from eimemory.knowledge.source_trust import revalidate_source_trust_decision, source_trust_decision_from_payload
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore


VALIDATION_SOURCE = "eimemory.skill_validation"
REPORT_TYPE = "skill_candidate_validation"
PERSISTENCE_RECEIPT_SCHEMA = "skill_observation_persistence.v2"
REQUIRED_GOOD_OBSERVATIONS = 3
FAILURE_RATE_THRESHOLD = 0.34
REAL_OBSERVATION_KINDS = {"real", "operator"}
GOOD_OUTCOMES = {"good", "success", "pass", "passed", "improved", "better"}
BAD_OUTCOMES = {"bad", "fail", "failed", "regressed", "unsafe", "error"}


def validate_skill_candidate(
    store: RuntimeStore,
    *,
    candidate_id: str | None = None,
    scope: ScopeRef | dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
    persist: bool = True,
    source_registry: Any = None,
) -> dict[str, Any]:
    """Replay a skill_candidate draft through deterministic local sandbox gates."""
    scope_ref = _scope(scope)
    record = _load_candidate_record(store, candidate_id=candidate_id, scope=scope_ref, exact_scope=True) if candidate is None else None
    try:
        candidate_payload = _candidate_payload(record=record, candidate=candidate)
    except (TypeError, ValueError, RecursionError):
        return {
            "ok": False,
            "report_type": REPORT_TYPE,
            "pass": False,
            "stage": "sandbox_input",
            "reasons": ["unsupported_skill_safety_input"],
            "persisted": False,
        }
    resolved_candidate_id = str(candidate_id or (record.record_id if record else "") or _dry_candidate_id(candidate_payload, scope_ref))
    current_status = _candidate_status(record, candidate_payload)

    checks = _sandbox_checks(candidate_payload, source_registry=source_registry)
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
    """Record observations with sequential retries; writes are not one transaction."""
    scope_ref = _scope(scope)
    record = _load_candidate_record(store, candidate_id=candidate_id, scope=scope_ref, exact_scope=True)
    previous_status = str(record.status or "candidate")
    validation = _validation_state(record)
    observations = [dict(item) for item in validation.get("observations") or [] if isinstance(item, dict)]
    outcome_status = str(outcome or "").strip().lower()
    is_bad = outcome_status in BAD_OUTCOMES
    is_good = outcome_status in GOOD_OUTCOMES and not is_bad
    obs_id = str(observation_id or _observation_id(candidate_id, observations, outcome_status))

    observation_kind_value = str(observation_kind or "real").strip().lower() or "real"
    request = {
        "outcome": outcome_status,
        "observation_kind": observation_kind_value,
        "reason": str(reason or ""),
        "details": dict(details or {}),
    }
    existing_observation = next((item for item in observations if str(item.get("observation_id") or "") == obs_id), None)
    if existing_observation is not None:
        if _observation_request_key(existing_observation) != _observation_request_key(request):
            return {
                "ok": False,
                "error": "observation_id_conflict",
                "report_type": REPORT_TYPE,
                "candidate_id": candidate_id,
                "observation_id": obs_id,
                "proposal_status": previous_status,
                "status_transition": {"from": previous_status, "to": previous_status},
                "pass": False,
                "duplicate": True,
                "persisted": False,
                "persistence_receipt_schema": PERSISTENCE_RECEIPT_SCHEMA,
                "write_attempted": False,
                "candidate_write_attempted": False,
                "validation_write_attempted": False,
                "commit_uncertain": False,
                "commit_status_scope": "current_attempt",
            }
        return _observation_retry_report(store, record, existing_observation, scope=scope_ref)
    observed_at = now_iso()
    if obs_id not in {str(item.get("observation_id") or "") for item in observations}:
        observations.append(
            {
                "observation_id": obs_id,
                "observation_kind": observation_kind_value,
                "outcome": outcome_status,
                "good": bool(is_good),
                "bad": bool(is_bad),
                "reason": request["reason"],
                "details": request["details"],
                "observed_at": observed_at,
            }
        )

    good_count = sum(1 for item in observations if bool(item.get("good")))
    bad_count = sum(1 for item in observations if bool(item.get("bad")))
    real_good_count = sum(1 for item in observations if bool(item.get("good")) and _is_real_observation(item))
    real_bad_count = sum(1 for item in observations if bool(item.get("bad")) and _is_real_observation(item))
    total_count = len(observations)
    failure_rate = round(bad_count / total_count, 3) if total_count else 0.0
    rollback_evidence_ids = [
        str(item.get("observation_id") or "")
        for item in observations
        if bool(item.get("bad")) and _is_real_observation(item) and str(item.get("observation_id") or "").strip()
    ]
    last_bad_at = _last_bad_at(observations)
    next_status = previous_status
    passed = is_good and not is_bad
    reasons: list[str] = []
    unsafe_real_outcome = outcome_status == "unsafe" and observation_kind_value in REAL_OBSERVATION_KINDS

    if previous_status == "active" and unsafe_real_outcome:
        next_status = "rolled_back"
        passed = False
        reasons.append("unsafe_outcome")
    elif previous_status == "active" and real_bad_count >= 2:
        next_status = "rolled_back"
        passed = False
        reasons.append("repeated_bad_real_observations")
    elif previous_status == "active":
        next_status = "active"
        if is_bad:
            passed = False
            reasons.append("bad_observation_recorded")
    elif is_bad or bad_count > 0:
        next_status = "quarantined"
        passed = False
        reasons.append("bad_observation")
    elif previous_status == "canary" and real_good_count >= REQUIRED_GOOD_OBSERVATIONS and failure_rate < FAILURE_RATE_THRESHOLD:
        next_status = "active"
    elif previous_status == "canary" and good_count >= REQUIRED_GOOD_OBSERVATIONS and real_good_count < REQUIRED_GOOD_OBSERVATIONS:
        next_status = "canary"
        reasons.append("requires_real_operator_observations")
    elif previous_status == "canary" and real_good_count >= REQUIRED_GOOD_OBSERVATIONS:
        next_status = "canary"
        reasons.append("failure_rate_above_threshold")
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
        "failure_rate": failure_rate,
        "reasons": reasons,
        "stage": "canary_observation",
        "good_observation_count": good_count,
        "bad_observation_count": bad_count,
        "pass_count": good_count,
        "fail_count": bad_count,
        "real_good_count": real_good_count,
        "real_bad_count": real_bad_count,
        "last_bad_at": last_bad_at,
        "rollback_evidence_ids": rollback_evidence_ids if next_status in {"quarantined", "rolled_back"} else [],
        "required_good_observations": REQUIRED_GOOD_OBSERVATIONS,
        "observation_id": obs_id,
        "observation_kind": observation_kind_value,
        "failure_rate_threshold": FAILURE_RATE_THRESHOLD,
    }
    validation_record = _validation_result_record(report, scope=record.scope, candidate_record=record)
    observations[-1]["validation_record_id"] = validation_record.record_id
    try:
        _rewrite_candidate_with_validation(store, record, status=next_status, report=report, observations=observations)
    except Exception as exc:
        return _observation_write_failure(store, report, scope=scope_ref, validation_record_id=validation_record.record_id, stage="candidate_rewrite", error=exc, source_id=record.source_id)
    try:
        store.append(validation_record)
    except Exception as exc:
        return _observation_write_failure(store, report, scope=scope_ref, validation_record_id=validation_record.record_id, stage="validation_append", error=exc, source_id=record.source_id)
    report["persisted"] = True
    report["validation_record_id"] = validation_record.record_id
    report.update({
        "persistence_receipt_schema": PERSISTENCE_RECEIPT_SCHEMA,
        "write_attempted": True,
        "candidate_write_attempted": True,
        "validation_write_attempted": True,
        "write_acknowledged": {"candidate": True, "validation": True},
        "commit_uncertain": False,
        "commit_status_scope": "current_attempt",
    })
    return report



def _sandbox_checks(candidate: dict[str, Any], *, source_registry: Any = None) -> list[dict[str, Any]]:
    try:
        safety_payload = _skill_safety_payload(candidate)
    except (TypeError, ValueError, RecursionError):
        return [{"name": "knowledge_safety_input", "pass": False, "reason": "unsupported_skill_safety_input"}]
    trigger_conditions = _as_list(candidate.get("trigger_conditions"))
    steps = _as_list(candidate.get("steps"))
    acceptance = _as_list(candidate.get("acceptance_criteria"))
    trust_decision = (
        revalidate_source_trust_decision(candidate, registry=source_registry)
        if source_registry is not None
        else source_trust_decision_from_payload(candidate)
    )
    source_trust = float(trust_decision.score) if trust_decision is not None else 0.0
    risk_level = str(candidate.get("risk_level") or "").strip().lower()
    knowledge_safety = evaluate_knowledge_safety(
        safety_payload,
        task="capability",
        registry=source_registry,
    )
    return [
        {"name": "trigger_conditions", "pass": bool(trigger_conditions), "reason": "missing_trigger_conditions"},
        {"name": "steps", "pass": len(steps) >= 2, "reason": "insufficient_steps"},
        {"name": "acceptance_criteria", "pass": bool(acceptance), "reason": "missing_acceptance_criteria"},
        {"name": "source_trust", "pass": source_trust >= 0.6, "reason": "source_trust_below_0_6"},
        {"name": "risk_level", "pass": risk_level != "high", "reason": "risk_level_high"},
        {
            "name": "knowledge_safety",
            "pass": bool(knowledge_safety.get("capability_allowed")),
            "reason": "knowledge_safety_not_capability_allowed",
            "report": knowledge_safety,
        },
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
        good_count = sum(1 for item in observations if bool(item.get("good")))
        bad_count = sum(1 for item in observations if bool(item.get("bad")))
        total_count = len(observations)
        merged_validation["good_observation_count"] = good_count
        merged_validation["bad_observation_count"] = bad_count
        merged_validation["pass_count"] = good_count
        merged_validation["fail_count"] = bad_count
        merged_validation["real_good_count"] = sum(1 for item in observations if bool(item.get("good")) and _is_real_observation(item))
        merged_validation["real_bad_count"] = sum(1 for item in observations if bool(item.get("bad")) and _is_real_observation(item))
        merged_validation["failure_rate"] = round(bad_count / total_count, 3) if total_count else 0.0
        merged_validation["last_bad_at"] = _last_bad_at(observations)
        merged_validation["rollback_evidence_ids"] = list(report.get("rollback_evidence_ids") or [])
    else:
        merged_validation.setdefault("observations", [])
        merged_validation.setdefault("good_observation_count", 0)
        merged_validation.setdefault("bad_observation_count", 0)
        merged_validation.setdefault("pass_count", 0)
        merged_validation.setdefault("fail_count", 0)
        merged_validation.setdefault("real_good_count", 0)
        merged_validation.setdefault("real_bad_count", 0)
        merged_validation.setdefault("failure_rate", 0.0)
        merged_validation.setdefault("last_bad_at", "")
        merged_validation.setdefault("rollback_evidence_ids", [])

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
        source_id=candidate_record.source_id,
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


def _load_candidate_record(store: RuntimeStore, *, candidate_id: str | None, scope: ScopeRef, exact_scope: bool = False) -> RecordEnvelope:
    if not candidate_id:
        raise ValueError("candidate_id is required when candidate payload is not provided")
    record = store.get_by_id(str(candidate_id), scope=scope, exact_scope=True) if exact_scope else store.get_by_id(str(candidate_id), scope=scope)
    if record is None:
        raise ValueError(f"skill_candidate not found: {candidate_id}")
    if record.kind != "skill_candidate":
        raise ValueError(f"record is not a skill_candidate: {candidate_id}")
    return record



def _candidate_payload(*, record: RecordEnvelope | None, candidate: dict[str, Any] | None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if record is not None:
        # Reject unsupported data before dict/str/default-ID normalization.
        # Retain original content/meta too, including overlapping unknown keys.
        record_input = {
            "title": record.title,
            "summary": record.summary,
            "detail": record.detail,
            "status": record.status,
            "content": record.content,
            "meta": record.meta,
        }
        _skill_safety_payload(record_input)
        payload.update(dict(record.content or {}))
        payload.update({key: value for key, value in dict(record.meta or {}).items() if key not in payload})
        payload.setdefault("status", record.status)
        payload.setdefault("title", record.title)
        payload.setdefault("summary", record.summary)
    if candidate is not None:
        _skill_safety_payload(candidate)
        payload.update(dict(candidate))
    if record is not None:
        # Content fields must not hide the original record's complete prose.
        # Use a fresh key so even an unknown caller field is preserved intact.
        source_key = "_validation_source_record"
        while source_key in payload:
            source_key = "_" + source_key
        payload[source_key] = record_input
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


def _is_real_observation(observation: dict[str, Any]) -> bool:
    return str(observation.get("observation_kind") or "").strip().lower() in REAL_OBSERVATION_KINDS


def _last_bad_at(observations: list[dict[str, Any]]) -> str:
    bad_times = [str(item.get("observed_at") or "") for item in observations if bool(item.get("bad")) and str(item.get("observed_at") or "")]
    return bad_times[-1] if bad_times else ""


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
    tags = [tag for tag in record.tags if tag not in {"candidate", "sandbox_ready", "canary", "active", "quarantined", "rolled_back"}]
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


def _skill_safety_payload(candidate: dict[str, Any]) -> dict[str, Any]:
    """Screen every JSON field without executing, truncating, or mutating it.

    JSON preserves field order and data types. The additional raw string leaves
    retain literal whitespace/escapes for detectors that consume plain text.
    Only exact JSON builtins are supported: no arbitrary ``str``/iteration hooks.
    """
    if type(candidate) is not dict:
        raise TypeError("skill safety input must be a JSON object")
    strings: list[str] = []
    _validate_skill_safety_value(candidate, active=set(), strings=strings)
    serialized = json.dumps(candidate, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    payload = dict(candidate)
    payload["text"] = "\n".join([*strings, serialized])
    return payload


def _validate_skill_safety_value(value: Any, *, active: set[int], strings: list[str]) -> None:
    value_type = type(value)
    if value_type is str:
        strings.append(value)
        return
    if value is None or value_type is bool or value_type is int or value_type is float:
        # json.dumps(..., allow_nan=False) rejects non-finite floats below.
        return
    if value_type is not dict and value_type is not list:
        raise TypeError("unsupported skill safety input type")
    identity = id(value)
    if identity in active:
        raise ValueError("cyclic skill safety input")
    active.add(identity)
    try:
        if value_type is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise TypeError("skill safety object keys must be strings")
                strings.append(key)
                _validate_skill_safety_value(item, active=active, strings=strings)
        else:
            for item in value:
                _validate_skill_safety_value(item, active=active, strings=strings)
    finally:
        active.remove(identity)


def _observation_request_key(observation: dict[str, Any]) -> str:
    """Compare normalized facts, excluding server timestamps and receipt fields."""
    payload = {
        "outcome": str(observation.get("outcome") or "").strip().lower(),
        "observation_kind": str(observation.get("observation_kind") or "real").strip().lower() or "real",
        "reason": str(observation.get("reason") or ""),
        "details": dict(observation.get("details") or {}),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _observation_write_failure(store: RuntimeStore, report: dict[str, Any], *, scope: ScopeRef, validation_record_id: str, stage: str, error: Exception, source_id: str) -> dict[str, Any]:
    """Read back both writes after an error; never pretend they were atomic."""
    candidate_id = str(report["candidate_id"])
    observation_id = str(report["observation_id"])
    candidate_observed: bool | None = None
    current_status: str | None = None
    try:
        stored = store.get_by_id(candidate_id, scope=scope, exact_scope=True)
        current_status = str(stored.status) if stored is not None else None
        candidate_observed = bool(stored is not None and stored.source_id == source_id and stored.kind == "skill_candidate" and any(
            str(item.get("observation_id") or "") == observation_id
            and str(item.get("validation_record_id") or "") == validation_record_id
            for item in _validation_state(stored).get("observations") or []
            if isinstance(item, dict)
        ))
    except Exception:
        pass
    receipt_observed = _observation_receipt_exists(store, candidate_id=candidate_id, observation_id=observation_id, validation_record_id=validation_record_id, scope=scope, source_id=source_id)
    validation_attempted = stage == "validation_append"
    # A completed first call is positive acknowledgement; a delayed/negative
    # readback cannot erase it. Absence after an attempted write is not proof
    # that its commit did not happen.
    candidate_acknowledged = validation_attempted
    candidate_committed = candidate_acknowledged or candidate_observed is True
    both_committed = candidate_committed and receipt_observed is True
    commit_uncertain = not candidate_committed or (validation_attempted and receipt_observed is not True)
    return {
        "ok": False,
        "report_type": REPORT_TYPE,
        "candidate_id": candidate_id,
        "observation_id": observation_id,
        "pass": False,
        "stage": "observation_persistence",
        "error": "observation_persistence_failed",
        "failure_stage": stage,
        "exception_type": type(error).__name__,
        "attempted_report": dict(report),
        "proposal_status": current_status,
        "status_transition": {"from": report["status_transition"]["from"], "to": current_status},
        "persistence_receipt_schema": PERSISTENCE_RECEIPT_SCHEMA,
        "write_attempted": True,
        "candidate_write_attempted": True,
        "validation_write_attempted": validation_attempted,
        "write_acknowledged": {"candidate": candidate_acknowledged, "validation": False},
        "readback_observed": {"candidate_observation": candidate_observed, "validation_record": receipt_observed},
        "candidate_observation_present": True if candidate_committed else None,
        "original_validation_record_present": True if receipt_observed is True else None,
        "persisted": True if both_committed else None,
        "partial": False if both_committed else (True if candidate_committed and not validation_attempted else None),
        "commit_uncertain": commit_uncertain,
        "commit_status_scope": "current_attempt",
        "retry_safety": "not_established",
        "validation_record_id": validation_record_id,
    }


def _observation_retry_report(store: RuntimeStore, record: RecordEnvelope, observation: dict[str, Any], *, scope: ScopeRef) -> dict[str, Any]:
    status = str(record.status or "candidate")
    validation_record_id = str(observation.get("validation_record_id") or "")
    receipt_exists = _observation_receipt_exists(
        store,
        candidate_id=record.record_id,
        observation_id=str(observation.get("observation_id") or ""),
        validation_record_id=validation_record_id,
        scope=scope, source_id=record.source_id,
    )
    report = {
        "ok": receipt_exists is True,
        "report_type": REPORT_TYPE,
        "candidate_id": record.record_id,
        "observation_id": str(observation.get("observation_id") or ""),
        "observation_kind": str(observation.get("observation_kind") or ""),
        "proposal_status": status,
        "status_transition": {"from": status, "to": status},
        "pass": bool(observation.get("good")) and status not in {"quarantined", "rolled_back"} and receipt_exists is True,
        "stage": "observation_retry",
        "duplicate": True,
        "persisted": False,
        "persistence_receipt_schema": PERSISTENCE_RECEIPT_SCHEMA,
        "write_attempted": False,
        "candidate_write_attempted": False,
        "validation_write_attempted": False,
        "write_acknowledged": {"candidate": False, "validation": False},
        "candidate_observation_present": True,
        "original_validation_record_present": True if receipt_exists is True else None,
        "original_persisted": True if receipt_exists is True else None,
        "readback_observed": {"candidate_observation": True, "validation_record": receipt_exists},
        "partial": False if receipt_exists is True else None,
        "commit_uncertain": receipt_exists is not True,
        "commit_status_scope": "original_observation",
        "retry_safety": "not_established",
        "validation_record_id": validation_record_id,
    }
    if receipt_exists is not True:
        report["error"] = "observation_prior_write_unverified"
    return report


def _observation_receipt_exists(store: RuntimeStore, *, candidate_id: str, observation_id: str, validation_record_id: str, scope: ScopeRef, source_id: str) -> bool | None:
    if not validation_record_id:
        return None  # Legacy observations have no per-observation write receipt.
    try:
        receipt = store.get_by_id(validation_record_id, scope=scope, exact_scope=True)
    except Exception:
        return None
    if receipt is None:
        return False
    report = receipt.content.get("report") if isinstance(receipt.content, dict) else None
    return bool(
        receipt.source_id == source_id
        and receipt.status == "active"
        and receipt.kind == "replay_result"
        and receipt.source == VALIDATION_SOURCE
        and receipt.meta.get("report_type") == REPORT_TYPE
        and receipt.meta.get("candidate_id") == candidate_id
        and isinstance(report, dict)
        and report.get("candidate_id") == candidate_id
        and report.get("report_type") == REPORT_TYPE
        and report.get("observation_id") == observation_id
    )
