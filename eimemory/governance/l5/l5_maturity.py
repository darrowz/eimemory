"""Scope-bound, version-neutral monotonic L5 maturity."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef


CHECKPOINT_REPORT_TYPE = "l5_maturity_checkpoint"
CHECKPOINT_SCHEMA_VERSION = "l5_maturity_checkpoint.v1"
STAGE_ORDER = {
    "L3.5": 0,
    "L4": 1,
    "L4.5": 2,
    "L5": 3,
}


def resolve_maturity_checkpoint(runtime, *, scope) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    checkpoint = runtime.store.latest_record_by_meta_value_exact_scope(
        kind="reflection",
        source="eimemory.l5_maturity",
        status="active",
        scope=scope_ref,
        meta_key="report_type",
        meta_value=CHECKPOINT_REPORT_TYPE,
    )
    if checkpoint is not None:
        content = checkpoint.content if isinstance(checkpoint.content, dict) else {}
        stage = str(content.get("stage") or checkpoint.meta.get("stage") or "")
        if (
            str(content.get("schema_version") or checkpoint.meta.get("schema_version") or "")
            == CHECKPOINT_SCHEMA_VERSION
            and stage in STAGE_ORDER
        ):
            return _checkpoint_payload(checkpoint, stage=stage)
    for stage in reversed(STAGE_ORDER):
        historical = runtime.store.latest_record_by_meta_value_exact_scope(
            kind="reflection",
            source="eimemory.l5_readiness",
            status="active",
            scope=scope_ref,
            meta_key="stage",
            meta_value=stage,
        )
        if historical is None:
            continue
        content = historical.content if isinstance(historical.content, dict) else {}
        report_type = str(
            historical.meta.get("report_type")
            or content.get("report_type")
            or ""
        )
        historical_stage = str(
            content.get("current_stage")
            or content.get("stage")
            or historical.meta.get("stage")
            or ""
        )
        if report_type == "l5_readiness_report" and historical_stage == stage:
            return _checkpoint_payload(historical, stage=stage, bootstrap=True)
    return {
        "stage": "",
        "score": 0.0,
        "record_id": "",
        "created_at": "",
        "downgrade_incident_id": "",
        "bootstrap": False,
    }


def apply_monotonic_maturity(
    runtime,
    *,
    scope,
    observed_stage: str,
    observed_score: float,
    persist: bool,
    loop_id: str,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if observed_stage not in STAGE_ORDER:
        raise ValueError(f"unknown L5 maturity stage: {observed_stage}")
    checkpoint = resolve_maturity_checkpoint(runtime, scope=scope_ref)
    checkpoint_stage = str(checkpoint.get("stage") or "")
    checkpoint_rank = STAGE_ORDER.get(checkpoint_stage, -1)
    observed_rank = STAGE_ORDER[observed_stage]
    transition = "held"
    downgrade_incident_id = str(checkpoint.get("downgrade_incident_id") or "")
    effective_stage = checkpoint_stage if checkpoint_rank >= observed_rank else observed_stage
    effective_score = (
        float(checkpoint.get("score") or _score_for_stage(checkpoint_stage))
        if checkpoint_rank >= observed_rank
        else float(observed_score)
    )
    selected_record_id = str(checkpoint.get("record_id") or "")
    fatal_incident = _newest_valid_fatal_incident(
        runtime,
        scope=scope_ref,
        checkpoint=checkpoint,
    )
    if fatal_incident is not None:
        effective_stage = fatal_incident["target_stage"]
        effective_score = _score_for_stage(effective_stage)
        transition = "fatal_downgrade"
        downgrade_incident_id = fatal_incident["record_id"]
        if persist:
            stored = _persist_checkpoint(
                runtime,
                scope=scope_ref,
                stage=effective_stage,
                score=effective_score,
                transition=transition,
                prior_checkpoint_record_id=selected_record_id,
                downgrade_incident=fatal_incident,
                loop_id=loop_id,
            )
            selected_record_id = stored.record_id
    elif _active_fatal_hold(
        runtime,
        scope=scope_ref,
        checkpoint=checkpoint,
    ):
        effective_stage = checkpoint_stage
        effective_score = float(
            checkpoint.get("score") or _score_for_stage(checkpoint_stage)
        )
        transition = "held"
        downgrade_incident_id = str(checkpoint.get("downgrade_incident_id") or "")
    elif observed_rank > checkpoint_rank:
        transition = "advanced"
        downgrade_incident_id = ""
        if persist and observed_stage != "L3.5":
            stored = _persist_checkpoint(
                runtime,
                scope=scope_ref,
                stage=effective_stage,
                score=effective_score,
                transition=transition,
                prior_checkpoint_record_id=selected_record_id,
                downgrade_incident=None,
                loop_id=loop_id,
            )
            selected_record_id = stored.record_id
    regression_warning = ""
    if observed_rank < STAGE_ORDER[effective_stage]:
        regression_warning = (
            f"observed release stage {observed_stage} is below accumulated "
            f"maturity {effective_stage}"
        )
    return {
        "observed_stage": observed_stage,
        "observed_score": float(observed_score),
        "current_stage": effective_stage,
        "readiness_score": effective_score,
        "maturity_transition": transition,
        "maturity_checkpoint_record_id": selected_record_id,
        "downgrade_incident_id": downgrade_incident_id,
        "regression_warning": regression_warning,
    }


def _newest_valid_fatal_incident(
    runtime,
    *,
    scope: ScopeRef,
    checkpoint: dict[str, Any],
) -> dict[str, Any] | None:
    checkpoint_stage = str(checkpoint.get("stage") or "")
    if checkpoint_stage not in STAGE_ORDER:
        return None
    checkpoint_time = _timestamp(str(checkpoint.get("created_at") or ""))
    valid = []
    for record in runtime.store.list_records(
        kinds=["incident"],
        scope=scope,
        status="active",
        limit=500,
    ):
        payload = {}
        if isinstance(record.meta, dict):
            payload.update(record.meta)
        if isinstance(record.content, dict):
            payload.update(record.content)
        target_stage = str(payload.get("target_stage") or "")
        evidence_record_ids = payload.get("evidence_record_ids")
        if not (
            str(payload.get("incident_type") or "") == "l5_fatal_regression"
            and str(payload.get("severity") or "") == "critical"
            and payload.get("fatal") is True
            and str(payload.get("status") or "") == "confirmed"
            and target_stage in STAGE_ORDER
            and STAGE_ORDER[target_stage] < STAGE_ORDER[checkpoint_stage]
            and isinstance(evidence_record_ids, list)
            and bool([item for item in evidence_record_ids if str(item or "").strip()])
            and bool(str(payload.get("confirmed_by") or "").strip())
            and _timestamp(record.time.created_at) > checkpoint_time
        ):
            continue
        valid.append(
            {
                "record_id": record.record_id,
                "created_at": record.time.created_at,
                "target_stage": target_stage,
                "confirmed_by": str(payload["confirmed_by"]),
                "evidence_record_ids": [
                    str(item)
                    for item in evidence_record_ids
                    if str(item or "").strip()
                ],
            }
        )
    return max(valid, key=lambda item: _timestamp(item["created_at"])) if valid else None


def _active_fatal_hold(
    runtime,
    *,
    scope: ScopeRef,
    checkpoint: dict[str, Any],
) -> bool:
    incident_id = str(checkpoint.get("downgrade_incident_id") or "")
    if not incident_id:
        return False
    incident = runtime.store.get_by_id(incident_id, scope=scope)
    if incident is None or incident.kind != "incident" or incident.status != "active":
        return False
    payload = {}
    if isinstance(incident.meta, dict):
        payload.update(incident.meta)
    if isinstance(incident.content, dict):
        payload.update(incident.content)
    return bool(
        str(payload.get("incident_type") or "") == "l5_fatal_regression"
        and str(payload.get("severity") or "") == "critical"
        and payload.get("fatal") is True
        and str(payload.get("status") or "") == "confirmed"
        and str(payload.get("target_stage") or "") == str(checkpoint.get("stage") or "")
        and isinstance(payload.get("evidence_record_ids"), list)
        and bool(payload["evidence_record_ids"])
        and bool(str(payload.get("confirmed_by") or "").strip())
    )


def _persist_checkpoint(
    runtime,
    *,
    scope: ScopeRef,
    stage: str,
    score: float,
    transition: str,
    prior_checkpoint_record_id: str,
    downgrade_incident: dict[str, Any] | None,
    loop_id: str,
) -> RecordEnvelope:
    incident_id = str((downgrade_incident or {}).get("record_id") or "")
    identity = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "scope": asdict(scope),
        "stage": stage,
        "prior_checkpoint_record_id": prior_checkpoint_record_id,
        "downgrade_incident_id": incident_id,
    }
    semantic_key = sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    for existing in runtime.store.list_records(
        kinds=["reflection"],
        scope=scope,
        status="active",
        limit=1000,
    ):
        if (
            existing.source == "eimemory.l5_maturity"
            and str(existing.meta.get("semantic_key") or "") == semantic_key
        ):
            return existing
    content = {
        "report_type": CHECKPOINT_REPORT_TYPE,
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "stage": stage,
        "score": float(score),
        "transition": transition,
        "scope": asdict(scope),
        "semantic_key": semantic_key,
        "prior_checkpoint_record_id": prior_checkpoint_record_id,
        "downgrade_incident_id": incident_id,
        "confirmed_by": str((downgrade_incident or {}).get("confirmed_by") or ""),
        "evidence_record_ids": list(
            (downgrade_incident or {}).get("evidence_record_ids") or []
        ),
        "loop_id": str(loop_id or "l5_readiness"),
    }
    record = RecordEnvelope.create(
        kind="reflection",
        title=f"L5 maturity checkpoint: {stage}",
        summary=f"{transition} to {stage}",
        scope=scope,
        source="eimemory.l5_maturity",
        content=content,
        meta={
            "report_type": CHECKPOINT_REPORT_TYPE,
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "stage": stage,
            "semantic_key": semantic_key,
            "downgrade_incident_id": incident_id,
        },
        evidence=list(content["evidence_record_ids"]),
    )
    return runtime.store.append(record)


def _checkpoint_payload(
    record: RecordEnvelope,
    *,
    stage: str,
    bootstrap: bool = False,
) -> dict[str, Any]:
    content = record.content if isinstance(record.content, dict) else {}
    return {
        "stage": stage,
        "score": float(content.get("score") or content.get("readiness_score") or _score_for_stage(stage)),
        "record_id": record.record_id,
        "created_at": record.time.created_at,
        "downgrade_incident_id": str(content.get("downgrade_incident_id") or ""),
        "bootstrap": bootstrap,
    }


def _score_for_stage(stage: str) -> float:
    return {
        "L3.5": 0.2,
        "L4": 0.6,
        "L4.5": 0.8,
        "L5": 1.0,
    }.get(stage, 0.0)


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
