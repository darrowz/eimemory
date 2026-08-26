from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Callable, cast
from uuid import uuid4

from eimemory.events import normalize_scope
from eimemory.governance.policy_rollout import extract_pattern_ids_from_outcome, next_rollout_id, now_utc
from eimemory.governance.rollout_lifecycle import record_lifecycle_event
from eimemory.governance.code_evolution_observation import (
    OBSERVATION_HOURS as CODE_EVOLUTION_OBSERVATION_HOURS,
    OBSERVATION_OFFSETS as CODE_EVOLUTION_OBSERVATION_OFFSETS,
    observation_phase as _shared_observation_phase,
    parse_observation_time as _shared_parse_observation_time,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


REQUIRED_OBSERVATIONS = 3
WATCH_STATUS = "shadow_observe"


def initialize_promotion_watch(
    runtime: Any,
    *,
    candidate: RecordEnvelope,
    scope: dict[str, Any] | ScopeRef | None,
    promotion_request_id: str,
    applied_pattern_ids: list[str],
) -> dict[str, Any]:
    initialized: list[dict[str, Any]] = []
    for pattern_id in applied_pattern_ids:
        pattern = _load_pattern(runtime, pattern_id=str(pattern_id), scope=scope or candidate.scope)
        if not pattern:
            continue
        watch = _initial_watch(
            candidate_id=candidate.record_id,
            promotion_request_id=promotion_request_id,
            pattern_id=str(pattern_id),
        )
        pattern["status"] = "shadow"
        pattern["post_promotion_watch"] = watch
        _write_pattern(runtime, pattern, scope=scope or candidate.scope)
        initialized.append({"pattern_id": str(pattern_id), "status": WATCH_STATUS})
    return {"status": WATCH_STATUS, "patterns": initialized, "required_observations": REQUIRED_OBSERVATIONS}


def record_outcome_observations(
    runtime: Any,
    *,
    event_id: str,
    outcome_payload: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None = None,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    attribution = _outcome_policy_attribution(runtime, event_id=event_id, outcome_payload=outcome_payload, scope=scope)
    for pattern_id in attribution["pattern_ids"]:
        pattern = _load_pattern(runtime, pattern_id=pattern_id, scope=scope)
        watch = dict((pattern or {}).get("post_promotion_watch") or {})
        if watch.get("status") != WATCH_STATUS:
            continue
        details = {
            "outcome_id": str(outcome_payload.get("id") or ""),
            "outcome_event_id": str(event_id or outcome_payload.get("event_id") or ""),
            "outcome_trace_id": str(outcome_payload.get("trace_id") or ""),
            "audit_record_id": str(attribution.get("audit_record_id") or ""),
            "selected_records": list(attribution.get("selected_records") or []),
        }
        reports.append(
            record_promotion_observation(
                runtime,
                pattern_id=pattern_id,
                scope=scope,
                event_id=event_id,
                hit=True,
                improved=_improved_from_outcome(outcome_payload),
                outcome=str(outcome_payload.get("outcome") or ""),
                reason=str(outcome_payload.get("reason") or outcome_payload.get("correction_from_user") or ""),
                details=details,
            )
        )
    return reports


def observe_code_evolution_transaction(
    runtime: Any,
    *,
    transaction_id: str,
) -> dict[str, Any]:
    """Sample and record one observation from protected live authorities."""

    from eimemory.governance.code_evolution_effects import sample_code_evolution_observation
    from eimemory.governance.code_evolution_transaction import CodeEvolutionTransactionManager

    manager = CodeEvolutionTransactionManager(
        runtime,
        owner_id=f"learn-watch:observation:{uuid4().hex}",
    )
    transaction = manager.store.get_transaction(transaction_id)
    if transaction is None:
        return {"ok": False, "status": "not_found", "transaction_id": transaction_id}
    sample = sample_code_evolution_observation(runtime, transaction=transaction)
    if sample.get("ok") is False:
        return {
            "ok": False,
            "status": "observation_authority_unavailable",
            "transaction_id": transaction_id,
            "reason": str(sample.get("reason") or "observation_sample_unavailable"),
        }
    if sample.get("duplicate_phase") is True:
        return {
            "ok": True,
            "transaction_id": transaction_id,
            "status": "duplicate",
            "sample_key": str(sample.get("sample_key") or ""),
        }
    return _record_code_evolution_observation_sample(
        runtime,
        transaction_id=transaction_id,
        sample=sample,
        owner_id=manager.owner_id,
    )


def _record_code_evolution_observation_sample(
    runtime: Any,
    *,
    transaction_id: str,
    sample: dict[str, Any],
    owner_id: str = "",
    observed_at: str = "",
) -> dict[str, Any]:
    """Append one lease-owned 48-hour transaction observation.

    Samples are ledger step events, not a parallel watch database.  The
    deterministic sample key makes timer restarts and duplicate deliveries
    idempotent while every external recovery edge remains intent-first.
    """

    from eimemory.governance.code_evolution_transaction import (
        CodeEvolutionTransactionManager,
    )
    from eimemory.storage.code_evolution_store import CodeEvolutionConflict, digest_json, utc_now

    if not isinstance(sample, dict):
        return {"ok": False, "status": "blocked", "reason": "observation_sample_invalid"}
    required = (
        "commit",
        "release_identity",
        "provider_advertisement_digest",
        "deployment_receipt_digest",
        "incident_measure",
    )
    missing = [
        field
        for field in required
        if field != "incident_measure" and not str(sample.get(field) or "").strip()
    ]
    if "incident_measure" not in sample or not isinstance(sample.get("incident_measure"), dict):
        missing.append("incident_measure")
    if missing:
        return {"ok": False, "status": "blocked", "reason": "observation_sample_fields_missing", "missing": missing}
    manager = CodeEvolutionTransactionManager(
        runtime,
        owner_id=owner_id or f"learn-watch:{transaction_id}",
    )
    lease_acquired = False
    try:
        transaction = manager.store.get_transaction(transaction_id)
        if transaction is None:
            return {"ok": False, "status": "not_found", "transaction_id": transaction_id}
        if bool(transaction.get("terminal")):
            return {"ok": True, "status": "terminal", "transaction_id": transaction_id}
        manager.acquire_lease(transaction_id)
        lease_acquired = True
        transaction = manager.store.get_transaction(transaction_id) or transaction
        current_state = str(transaction.get("current_state") or "")
        if current_state != "OBSERVING":
            return {
                "ok": False,
                "status": "observation_state_invalid",
                "transaction_id": transaction_id,
                "state": current_state,
            }
        checked_at = str(observed_at or sample.get("observed_at") or utc_now())
        receipt_digest = str(
            sample.get("deployment_receipt_digest")
            or sample.get("receipt_digest")
            or ""
        ).strip().lower()
        if len(receipt_digest) != 64 or any(char not in "0123456789abcdef" for char in receipt_digest):
            return {
                "ok": False,
                "status": "observation_receipt_digest_invalid",
                "transaction_id": transaction_id,
            }
        normalized_sample = {
            "transaction_id": transaction_id,
            "observed_at": checked_at,
            "commit": str(sample.get("commit") or ""),
            "release_identity": str(sample.get("release_identity") or ""),
            "service_health": dict(sample.get("service_health") or {}),
            "provider_advertisement_digest": str(sample.get("provider_advertisement_digest") or ""),
            "deployment_receipt_digest": receipt_digest,
            "incident_measure": dict(sample.get("incident_measure") or {}),
            "health_ok": sample.get("health_ok") is True,
            "incident_regressed": sample.get("incident_regressed") is True,
            "hard_failure": sample.get("hard_failure") is True,
        }
        payload = dict(transaction.get("payload") or {})
        expected_receipt_digest = str(
            payload.get("deployment_receipt_digest")
            or payload.get("receipt_digest")
            or ""
        ).strip().lower()
        expected_advertisement_digest = str(
            transaction.get("advertisement_digest")
            or payload.get("provider_advertisement_digest")
            or ""
        ).strip().lower()
        expected_commit = str(
            transaction.get("deployed_commit")
            or transaction.get("candidate_commit")
            or payload.get("deployed_commit")
            or payload.get("candidate_commit")
            or ""
        ).strip()
        if payload.get("candidate_pushed_and_deployed") is not True:
            return {
                "ok": False,
                "status": "observation_evidence_unproven",
                "transaction_id": transaction_id,
                "reason": "candidate_deployment_receipt_missing",
            }
        if expected_receipt_digest and receipt_digest != expected_receipt_digest:
            return {
                "ok": False,
                "status": "observation_evidence_mismatch",
                "transaction_id": transaction_id,
                "reason": "deployment_receipt_digest_mismatch",
            }
        if expected_advertisement_digest and normalized_sample["provider_advertisement_digest"] != expected_advertisement_digest:
            return {
                "ok": False,
                "status": "observation_evidence_mismatch",
                "transaction_id": transaction_id,
                "reason": "provider_advertisement_digest_mismatch",
            }
        if expected_commit and normalized_sample["commit"] != expected_commit:
            return {
                "ok": False,
                "status": "observation_evidence_mismatch",
                "transaction_id": transaction_id,
                "reason": "deployed_commit_mismatch",
            }
        sample_key = str(sample.get("sample_key") or "").strip()
        if not sample_key:
            sample_key = sha256(digest_json(normalized_sample).encode("utf-8")).hexdigest()
        events = manager.store.list_step_events(transaction_id, limit=2_000)
        persisted_sample = next(
            (
                item
                for item in payload.get("observation_samples") or ()
                if isinstance(item, dict) and str(item.get("sample_key") or "") == sample_key
            ),
            None,
        )
        if persisted_sample is not None:
            if payload.get("observation_failure") is True:
                transaction = manager.begin_intent(
                    transaction_id,
                    step="rollback",
                    intent_state="ROLLBACK_INTENT",
                    input_data={"sample_key": sample_key, "reason": "observation_failure_recovery"},
                )
                return {
                    "ok": True,
                    "status": "rollback_required",
                    "transaction_id": transaction_id,
                    "sample_key": sample_key,
                    "transaction": transaction,
                }
            if payload.get("observation_valid") is True:
                sedimentation_intent = next(
                    (
                        item
                        for item in events
                        if item.get("step") == "sedimentation" and item.get("phase") == "intent"
                    ),
                    None,
                )
                if sedimentation_intent is None:
                    return {
                        "ok": False,
                        "status": "observation_atomic_intent_missing",
                        "transaction_id": transaction_id,
                    }
                return _execute_code_evolution_sedimentation(
                    runtime,
                    manager=manager,
                    transaction=transaction,
                    checked_at=str(sedimentation_intent.get("created_at") or checked_at),
                    sample_key=sample_key,
                    intent_sequence=int(sedimentation_intent.get("sequence") or 0),
                )
            if str(persisted_sample.get("input_digest") or "") != digest_json(normalized_sample):
                return {
                    "ok": False,
                    "status": "observation_sample_identity_conflict",
                    "transaction_id": transaction_id,
                    "sample_key": sample_key,
                }
            return {
                "ok": True,
                "status": "duplicate",
                "transaction_id": transaction_id,
                "sample_key": sample_key,
            }
        observations = list(payload.get("observation_samples") or [])
        observations.append(
            {
                "sample_key": sample_key,
                "input_digest": digest_json(normalized_sample),
                "observed_at": checked_at,
                "event_sequence": 0,
                "health_ok": normalized_sample["health_ok"],
                "incident_regressed": normalized_sample["incident_regressed"],
                "hard_failure": normalized_sample["hard_failure"],
                "commit": normalized_sample["commit"],
                "release_identity": normalized_sample["release_identity"],
                "provider_advertisement_digest": normalized_sample["provider_advertisement_digest"],
                "deployment_receipt_digest": normalized_sample["deployment_receipt_digest"],
                "incident_measure": normalized_sample["incident_measure"],
            }
        )
        observations = observations[-16:]
        start = _parse_observation_time(str(transaction.get("observation_started_at") or ""))
        if start is None:
            start = _parse_observation_time(checked_at)
        deadline = _parse_observation_time(str(transaction.get("observation_deadline") or ""))
        if deadline is None and start is not None:
            deadline = start + timedelta(hours=CODE_EVOLUTION_OBSERVATION_HOURS)
        phases = {
            _observation_phase(start, _parse_observation_time(str(item.get("observed_at") or "")))
            for item in observations
            if start is not None and _parse_observation_time(str(item.get("observed_at") or "")) is not None
        }
        # Noncritical availability degradation requires two consecutive
        # samples.  Explicit hard failures and incident regression remain
        # immediate rollback triggers.
        hard_failure = bool(
            normalized_sample["hard_failure"]
            or normalized_sample["incident_regressed"]
        )
        degraded_tail = [
            not bool(item.get("health_ok"))
            for item in observations[-2:]
        ]
        consecutive_degraded = len(degraded_tail) == 2 and all(degraded_tail)
        observation_valid = bool(
            not hard_failure
            and not consecutive_degraded
            and deadline is not None
            and _parse_observation_time(checked_at) is not None
            and _parse_observation_time(checked_at) >= deadline
            and set(CODE_EVOLUTION_OBSERVATION_OFFSETS) <= phases
        )
        next_action = "rollback" if hard_failure or consecutive_degraded else "sedimentation" if observation_valid else ""
        persisted_payload = dict(payload)
        persisted_payload.update(
            {
                "observation_samples": observations,
                "observation_sample_keys": [str(item.get("sample_key") or "") for item in observations],
                "observation_valid": observation_valid,
                "observation_digest": digest_json(observations) if observation_valid else str(payload.get("observation_digest") or ""),
                "observation_failure": hard_failure or consecutive_degraded,
                "observation_consecutive_degraded": consecutive_degraded,
            }
        )
        committed = manager.store.commit_observation_result(
            transaction_id,
            owner=manager.owner_id,
            sample_key=sample_key,
            normalized_sample=normalized_sample,
            transaction_payload=persisted_payload,
            observation_started_at=start.isoformat(timespec="seconds") if start is not None else "",
            observation_deadline=deadline.isoformat(timespec="seconds") if deadline is not None else "",
            next_action=next_action,
            created_at=checked_at,
        )
        transaction = committed["transaction"]
        if hard_failure or consecutive_degraded:
            return {
                "ok": True,
                "status": "rollback_required",
                "transaction_id": transaction_id,
                "sample_key": sample_key,
                "consecutive_degraded": consecutive_degraded,
                "transaction": transaction,
            }
        if observation_valid:
            sedimentation_intent = committed.get("next_intent") or {}
            return _execute_code_evolution_sedimentation(
                runtime,
                manager=manager,
                transaction=transaction,
                checked_at=checked_at,
                sample_key=sample_key,
                intent_sequence=int(sedimentation_intent.get("sequence") or 0),
            )
        return {
            "ok": True,
            "status": "observing",
            "transaction_id": transaction_id,
            "sample_key": sample_key,
            "observation_valid": False,
            "required_phases": sorted(phases),
        }
    except CodeEvolutionConflict:
        return {"ok": False, "status": "lease_or_sample_conflict", "transaction_id": transaction_id}
    finally:
        if lease_acquired:
            try:
                manager.release_lease(transaction_id)
            except CodeEvolutionConflict:
                # A terminal transition or another reconciler may have
                # already cleared the lease; the durable result remains the
                # authority and does not need a second recovery attempt.
                pass


def _execute_code_evolution_sedimentation(
    runtime: Any,
    *,
    manager: Any,
    transaction: dict[str, Any],
    checked_at: str,
    sample_key: str,
    intent_sequence: int,
) -> dict[str, Any]:
    """Atomically append and reconcile one deterministic terminal outcome."""

    from eimemory.governance.code_evolution_transaction import reconcile_sedimentation
    from eimemory.storage.code_evolution_store import digest_json

    transaction_id = str(transaction.get("transaction_id") or "")
    payload_value = transaction.get("payload")
    payload: dict[str, Any] = dict(payload_value) if isinstance(payload_value, dict) else {}
    scope_ref = ScopeRef.from_dict(payload.get("scope") if isinstance(payload.get("scope"), dict) else {})
    semantic_payload = {
        "schema_version": "code_evolution_terminal_outcome.v1",
        "transaction_id": transaction_id,
        "incident_digest": str(transaction.get("incident_digest") or ""),
        "provider_digest": str(transaction.get("implementation_digest") or ""),
        "policy_digest": str(transaction.get("policy_digest") or ""),
        "authorization_digest": str(transaction.get("authorization_digest") or ""),
        "base_commit": str(transaction.get("base_commit") or ""),
        "candidate_commit": str(transaction.get("candidate_commit") or ""),
        "deployed_commit": str(transaction.get("deployed_commit") or ""),
        "deployment_receipt_digest": str(payload.get("deployment_receipt_digest") or ""),
        "observation_digest": str(payload.get("observation_digest") or ""),
        "outcome": "succeeded_sedimented",
    }
    semantic_digest = digest_json(semantic_payload)
    trace_id = f"code-evolution-{transaction_id}"
    idempotency_key = f"code-evolution-sedimentation:{transaction_id}"
    business_meta = {
        "report_type": "outcome_trace",
        "schema_version": "outcome_trace.v1",
        "trace_id": trace_id,
        "idempotency_key": idempotency_key,
        "semantic_key": idempotency_key,
        "semantic_digest": semantic_digest,
        "primary_label": "success",
        "blame_layer": "none",
        "outcome_status": "success",
        "code_evolution_transaction_id": transaction_id,
    }
    record = RecordEnvelope.create(
        kind="reflection",
        title=f"Code evolution terminal outcome: {transaction_id}",
        summary="succeeded_sedimented: verified code-evolution transaction",
        detail=json.dumps(semantic_payload, ensure_ascii=True, sort_keys=True),
        content={
            "schema_version": "outcome_trace.v1",
            "payload": {
                "trace_id": trace_id,
                "idempotency_key": idempotency_key,
                "outcome": {"status": "success", "rehearsal": False},
                "recorded_at": checked_at,
                "code_evolution": semantic_payload,
            },
            "diagnosis": {"primary_label": "success", "blame_layer": "none", "signals": []},
        },
        tags=["experience", "outcome_trace", "code-evolution", "success"],
        source="eimemory.experience.outcome_trace",
        scope=scope_ref,
        provenance={**business_meta, "source": "eimemory.code_evolution.sedimentation"},
        meta={**business_meta, "business_meta": business_meta},
    )
    record_store = getattr(runtime, "store", runtime)
    atomic_append = getattr(record_store, "append_outcome_trace_if_absent", None)
    if not callable(atomic_append):
        return {"ok": False, "status": "sedimentation_store_unavailable", "transaction_id": transaction_id}
    try:
        append_once = cast(Callable[..., tuple[RecordEnvelope, bool]], atomic_append)
        stored, idempotent = append_once(
            record,
            scope=scope_ref,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
    except Exception:
        return {"ok": False, "status": "sedimentation_append_failed", "transaction_id": transaction_id}

    stored_digest = str(stored.meta.get("semantic_digest") or "")
    if stored_digest != semantic_digest:
        decision = reconcile_sedimentation({"conflicting_outcome_exists": True})
    else:
        manager.update_metadata(
            transaction_id,
            payload_updates={
                "sedimentation_record_id": stored.record_id,
                "sedimentation_digest": semantic_digest,
            },
        )
        decision = reconcile_sedimentation(
            {
                "matching_outcome_exists": True,
                "record_id": stored.record_id,
                "semantic_digest": semantic_digest,
            }
        )
    terminal = manager.reconcile(
        transaction_id,
        step="sedimentation",
        decision=decision,
        success_state="SUCCEEDED_SEDIMENTED" if decision.status == "succeeded" else None,
    )
    return {
        "ok": decision.status == "succeeded",
        "status": "succeeded_sedimented" if decision.status == "succeeded" else "recovery_quarantined",
        "transaction_id": transaction_id,
        "sample_key": sample_key,
        "observation_valid": True,
        "sedimentation_intent_sequence": intent_sequence,
        "sedimentation_record_id": stored.record_id,
        "sedimentation_digest": semantic_digest,
        "idempotent": bool(idempotent),
        "transaction": terminal,
    }


def resume_code_evolution_transactions(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    owner_id: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    """Resume typed intent reconciliation through the same transaction owner."""

    from eimemory.governance.code_evolution_transaction import (
        CodeEvolutionTransactionManager,
        FORWARD_EFFECT_STATES,
        effect_execution_authorized,
        reconcile_rollback,
        recover_transaction,
    )

    run_owner_id = f"{owner_id or 'learn-watch:reconciler'}:{uuid4().hex}"
    manager = CodeEvolutionTransactionManager(runtime, owner_id=run_owner_id)
    scope_ref = _scope(scope)
    reports: list[dict[str, Any]] = []
    for transaction in manager.store.list_transactions(limit=max(1, min(500, int(limit)))):
        if transaction.get("terminal"):
            continue
        payload = transaction.get("payload") if isinstance(transaction.get("payload"), dict) else {}
        tx_scope = payload.get("scope") if isinstance(payload.get("scope"), dict) else {}
        if tx_scope and tx_scope != asdict(scope_ref):
            continue
        transaction_id = str(transaction.get("transaction_id") or "")
        state = str(transaction.get("current_state") or "")
        if state in FORWARD_EFFECT_STATES and state not in {"COMMIT_INTENT", "PUSH_INTENT", "DEPLOY_INTENT"}:
            if not effect_execution_authorized(transaction):
                reports.append({"transaction_id": transaction_id, "status": "effect_execution_not_authorized", "state": state})
                continue
            from eimemory.governance.code_evolution_effects import execute_code_evolution_effects

            reports.append(
                execute_code_evolution_effects(
                    runtime,
                    transaction_id=transaction_id,
                    owner_id=run_owner_id,
                )
            )
            continue
        if state not in {"COMMIT_INTENT", "PUSH_INTENT", "DEPLOY_INTENT", "ROLLBACK_INTENT", "OBSERVING"}:
            reports.append({"transaction_id": transaction_id, "status": "no_external_intent", "state": state})
            continue
        if state == "OBSERVING":
            from eimemory.governance.code_evolution_effects import sample_code_evolution_observation

            sample = sample_code_evolution_observation(runtime, transaction=transaction)
            if sample.get("ok") is False:
                reports.append(
                    {
                        "transaction_id": transaction_id,
                        "status": "observation_authority_unavailable",
                        "reason": str(sample.get("reason") or "observation_sample_unavailable"),
                    }
                )
            elif sample.get("duplicate_phase") is True:
                reports.append(
                    {
                        "ok": True,
                        "transaction_id": transaction_id,
                        "status": "duplicate",
                        "sample_key": str(sample.get("sample_key") or ""),
                    }
                )
            else:
                reports.append(
                    _record_code_evolution_observation_sample(
                        runtime,
                        transaction_id=transaction_id,
                        sample=sample,
                        owner_id=run_owner_id,
                    )
                )
            continue
        from eimemory.governance.code_evolution_effects import read_code_evolution_external_state

        external = read_code_evolution_external_state(runtime, transaction=transaction)
        if not external:
            reports.append({"transaction_id": transaction_id, "status": "awaiting_external_reconciliation", "state": state})
            continue
        if state == "ROLLBACK_INTENT":
            decision = reconcile_rollback(external)
            try:
                if decision.status == "rolled_back_healthy":
                    reports.append(recover_transaction(manager, transaction_id, external_state=external))
                else:
                    from eimemory.governance.code_evolution_effects import execute_code_evolution_rollback

                    reports.append(execute_code_evolution_rollback(runtime, transaction_id=transaction_id, owner_id=run_owner_id))
            except Exception as exc:
                reports.append({"transaction_id": transaction_id, "status": "recovery_error", "error": type(exc).__name__})
            continue
        try:
            recovered = recover_transaction(manager, transaction_id, external_state=external)
            recovered_state = str(recovered.get("current_state") or "")
            if recovered_state in FORWARD_EFFECT_STATES and effect_execution_authorized(recovered):
                from eimemory.governance.code_evolution_effects import execute_code_evolution_effects

                reports.append(execute_code_evolution_effects(runtime, transaction_id=transaction_id, owner_id=run_owner_id))
            else:
                reports.append(recovered)
        except Exception as exc:
            reports.append({"transaction_id": transaction_id, "status": "recovery_error", "error": type(exc).__name__})
    return {"ok": all(item.get("status") not in {"recovery_error"} for item in reports), "reports": reports}


def _parse_observation_time(value: str) -> datetime | None:
    return _shared_parse_observation_time(value)


def _observation_phase(start: datetime | None, observed: datetime | None) -> int:
    return _shared_observation_phase(start, observed)


def record_promotion_observation(
    runtime: Any,
    *,
    pattern_id: str,
    scope: dict[str, Any] | ScopeRef | None = None,
    event_id: str = "",
    hit: bool,
    improved: bool | None = None,
    outcome: str = "uncertain",
    reason: str = "",
    regressed: bool = False,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pattern = _load_pattern(runtime, pattern_id=pattern_id, scope=scope)
    if not pattern:
        return {"ok": False, "status": "not_found", "pattern_id": str(pattern_id)}

    watch = _watch_state(pattern, pattern_id=str(pattern_id))
    if watch.get("status") in {"active", "quarantined", "rolled_back"}:
        return {"ok": True, "status": str(watch["status"]), "pattern_id": str(pattern_id), "watch": watch}

    outcome_status = str(outcome or "uncertain").strip().lower()
    improved_value = _coerce_improved(improved, outcome=outcome_status)
    regressed_value = bool(regressed or outcome_status == "bad")
    observation = {
        "event_id": str(event_id or next_rollout_id(kind="promotion-observation", scope=_scope(scope), payload={"pattern_id": str(pattern_id), "count": int(watch.get("observed_count") or 0)})),
        "hit": bool(hit),
        "improved": bool(improved_value),
        "regressed": bool(regressed_value),
        "outcome": outcome_status,
        "reason": str(reason or ""),
        "details": dict(details or {}),
        "observed_at": now_utc(),
    }
    existing_ids = {str(item.get("event_id") or "") for item in watch.get("observations") or [] if isinstance(item, dict)}
    if observation["event_id"] not in existing_ids:
        observations = [item for item in watch.get("observations") or [] if isinstance(item, dict)]
        observations.append(observation)
        watch["observations"] = observations[-REQUIRED_OBSERVATIONS:]
        watch["observed_count"] = int(watch.get("observed_count") or 0) + 1
        if observation["hit"]:
            watch["hit_count"] = int(watch.get("hit_count") or 0) + 1
        if observation["improved"]:
            watch["improvement_count"] = int(watch.get("improvement_count") or 0) + 1
        if observation["regressed"]:
            watch["regression_count"] = int(watch.get("regression_count") or 0) + 1
        if observation["regressed"] or outcome_status == "bad":
            watch["failure_count"] = int(watch.get("failure_count") or 0) + 1
        if outcome_status == "bad":
            watch["bad_outcome_count"] = int(watch.get("bad_outcome_count") or 0) + 1
    watch["failure_rate"] = _failure_rate(watch)
    watch["updated_at"] = now_utc()
    pattern["post_promotion_watch"] = watch

    if int(watch.get("observed_count") or 0) >= int(watch.get("required_observations") or REQUIRED_OBSERVATIONS):
        failure_rate = _failure_rate(watch)
        watch["failure_rate"] = failure_rate
        if failure_rate >= 0.2:
            return _rollback_shadow_pattern(runtime, pattern=pattern, scope=scope, watch=watch, reason=reason or "canary failure rate exceeded threshold")
        if failure_rate <= 0.05 and _watch_can_activate(watch):
            return _activate_shadow_pattern(runtime, pattern=pattern, scope=scope, watch=watch)
        return _quarantine_shadow_pattern(runtime, pattern=pattern, scope=scope, watch=watch)

    pattern["status"] = "shadow"
    watch["status"] = WATCH_STATUS
    _write_pattern(runtime, pattern, scope=scope)
    _record_watch_ledger(runtime, pattern=pattern, scope=scope, watch=watch, decision=WATCH_STATUS)
    return {"ok": True, "status": WATCH_STATUS, "pattern_id": str(pattern_id), "watch": watch}


def _initial_watch(*, candidate_id: str, promotion_request_id: str, pattern_id: str) -> dict[str, Any]:
    now = now_utc()
    return {
        "status": WATCH_STATUS,
        "candidate_id": str(candidate_id),
        "promotion_request_id": str(promotion_request_id),
        "pattern_id": str(pattern_id),
        "required_observations": REQUIRED_OBSERVATIONS,
        "observed_count": 0,
        "hit_count": 0,
        "improvement_count": 0,
        "regression_count": 0,
        "bad_outcome_count": 0,
        "failure_count": 0,
        "failure_rate": 0.0,
        "observations": [],
        "started_at": now,
        "updated_at": now,
    }


def _watch_state(pattern: dict[str, Any], *, pattern_id: str) -> dict[str, Any]:
    watch = dict(pattern.get("post_promotion_watch") or {})
    if not watch:
        watch = _initial_watch(candidate_id=str((pattern.get("source_opportunity") or {}).get("opportunity_id") or ""), promotion_request_id="", pattern_id=pattern_id)
    watch.setdefault("status", WATCH_STATUS)
    watch.setdefault("pattern_id", pattern_id)
    watch.setdefault("required_observations", REQUIRED_OBSERVATIONS)
    watch.setdefault("observed_count", 0)
    watch.setdefault("hit_count", 0)
    watch.setdefault("improvement_count", 0)
    watch.setdefault("regression_count", 0)
    watch.setdefault("bad_outcome_count", 0)
    watch.setdefault("failure_count", max(int(watch.get("regression_count") or 0), int(watch.get("bad_outcome_count") or 0)))
    watch.setdefault("failure_rate", _failure_rate(watch))
    watch.setdefault("observations", [])
    return watch


def _watch_can_activate(watch: dict[str, Any]) -> bool:
    return (
        int(watch.get("hit_count") or 0) > 0
        and int(watch.get("improvement_count") or 0) > 0
        and _failure_rate(watch) <= 0.05
    )


def _failure_rate(watch: dict[str, Any]) -> float:
    observed = int(watch.get("observed_count") or 0)
    if observed <= 0:
        return 0.0
    if "failure_count" in watch:
        failures = int(watch.get("failure_count") or 0)
    else:
        failures = max(int(watch.get("regression_count") or 0), int(watch.get("bad_outcome_count") or 0))
    return round(min(1.0, max(0.0, failures / observed)), 6)


def _activate_shadow_pattern(runtime: Any, *, pattern: dict[str, Any], scope: dict[str, Any] | ScopeRef | None, watch: dict[str, Any]) -> dict[str, Any]:
    watch["status"] = "active"
    watch["decision"] = "active"
    watch["decided_at"] = now_utc()
    pattern["status"] = "active"
    pattern["post_promotion_watch"] = watch
    _write_pattern(runtime, pattern, scope=scope)
    _update_candidate_status(runtime, watch, scope=scope, status="promoted")
    _update_promotion_request_status(runtime, watch, scope=scope, status="active")
    _record_watch_ledger(runtime, pattern=pattern, scope=scope, watch=watch, decision="active")
    return {"ok": True, "status": "active", "activated": True, "pattern_id": str(pattern.get("id") or ""), "watch": watch}


def _quarantine_shadow_pattern(runtime: Any, *, pattern: dict[str, Any], scope: dict[str, Any] | ScopeRef | None, watch: dict[str, Any]) -> dict[str, Any]:
    previous_status = str(pattern.get("status") or "shadow")
    watch["status"] = "quarantined"
    watch["decision"] = "quarantined"
    watch["decided_at"] = now_utc()
    pattern["status"] = "quarantined"
    pattern["post_promotion_watch"] = watch
    _write_pattern(runtime, pattern, scope=scope)
    _update_candidate_status(runtime, watch, scope=scope, status="quarantined")
    _update_promotion_request_status(runtime, watch, scope=scope, status="quarantined")
    _record_watch_ledger(
        runtime,
        pattern=pattern,
        scope=scope,
        watch=watch,
        decision="quarantined",
        rollback_execution={
            "ok": True,
            "skipped": False,
            "execution_type": "intent_pattern_status_transition",
            "pattern_id": str(pattern.get("id") or ""),
            "candidate_id": str(watch.get("candidate_id") or ""),
            "status_transition": {
                "from": previous_status,
                "to": "quarantined",
                "pattern_id": str(pattern.get("id") or ""),
            },
        },
    )
    return {"ok": True, "status": "quarantined", "quarantined": True, "pattern_id": str(pattern.get("id") or ""), "watch": watch}


def _rollback_shadow_pattern(
    runtime: Any,
    *,
    pattern: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
    watch: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    previous_status = str(pattern.get("status") or "shadow")
    watch["status"] = "rolled_back"
    watch["decision"] = "rolled_back"
    watch["decided_at"] = now_utc()
    pattern["post_promotion_watch"] = watch
    _write_pattern(runtime, pattern, scope=scope)
    rollback = runtime.rollback_intent_pattern(str(pattern.get("id") or ""), scope=_scope_dict(scope), reason=str(reason or "bad outcome during shadow observe"), auto=True)
    _update_candidate_status(runtime, watch, scope=scope, status="rolled_back")
    _update_promotion_request_status(runtime, watch, scope=scope, status="rolled_back")
    _record_watch_ledger(
        runtime,
        pattern=pattern,
        scope=scope,
        watch=watch,
        decision="rolled_back",
        rollback_execution={
            "ok": rollback.get("ok") is True,
            "skipped": False,
            "execution_type": "intent_pattern_status_transition",
            "pattern_id": str(pattern.get("id") or ""),
            "candidate_id": str(watch.get("candidate_id") or ""),
            "status_transition": {
                "from": str(rollback.get("previous_status") or previous_status),
                "to": str(rollback.get("status") or "rolled_back"),
                "pattern_id": str(pattern.get("id") or ""),
            },
            "ledger_id": str(rollback.get("ledger_id") or ""),
        },
    )
    return {"ok": bool(rollback.get("ok")), "status": "rolled_back", "rolled_back": bool(rollback.get("ok")), "pattern_id": str(pattern.get("id") or ""), "rollback": rollback, "watch": watch}


def _load_pattern(runtime: Any, *, pattern_id: str, scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    row = runtime.store.sqlite._pattern_row_for_scope(str(pattern_id), _scope(scope))
    if row is None:
        return {}
    try:
        payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _write_pattern(runtime: Any, pattern: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None) -> None:
    runtime.store.sqlite.conn.execute(
        """
        UPDATE intent_patterns
        SET status = ?, payload_json = ?, last_rollback_reason = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            str(pattern.get("status") or "shadow"),
            json.dumps(pattern, ensure_ascii=False, sort_keys=True),
            str(pattern.get("last_rollback_reason") or ""),
            now_utc(),
            str(pattern.get("id") or ""),
        ),
    )
    runtime.store.sqlite.conn.commit()
    runtime.store.flush_exports()


def _record_watch_ledger(
    runtime: Any,
    *,
    pattern: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
    watch: dict[str, Any],
    decision: str,
    rollback_execution: dict[str, Any] | None = None,
) -> None:
    scope_ref = _scope(scope)
    pattern_id = str(pattern.get("id") or watch.get("pattern_id") or "")
    last_observation = {}
    observations = [item for item in list(watch.get("observations") or []) if isinstance(item, dict)]
    if observations:
        last_observation = dict(observations[-1])
    evidence = dict(last_observation.get("details") or {})
    action_type = _watch_action_for_decision(decision)
    details = {
        "decision": str(decision),
        "pattern_id": pattern_id,
        "candidate_id": str(watch.get("candidate_id") or ""),
        "audit_record_id": str(evidence.get("audit_record_id") or ""),
        "outcome_trace_id": str(evidence.get("outcome_trace_id") or ""),
        "outcome_event_id": str(evidence.get("outcome_event_id") or last_observation.get("event_id") or ""),
        "selected_records": list(evidence.get("selected_records") or []),
        "observed_count": int(watch.get("observed_count") or 0),
        "hit_count": int(watch.get("hit_count") or 0),
        "improvement_count": int(watch.get("improvement_count") or 0),
        "regression_count": int(watch.get("regression_count") or 0),
        "bad_outcome_count": int(watch.get("bad_outcome_count") or 0),
        "failure_count": int(watch.get("failure_count") or 0),
        "failure_rate": _failure_rate(watch),
    }
    if rollback_execution:
        details["rollback"] = dict(rollback_execution)
    record_lifecycle_event(
        runtime,
        scope=scope_ref,
        action_type=action_type,
        candidate_id=str(watch.get("candidate_id") or ""),
        promotion_id=str(watch.get("promotion_request_id") or ""),
        patch_id=pattern_id,
        observed_count=int(watch.get("observed_count") or 0),
        failure_rate=_failure_rate(watch),
        source_opportunity={"candidate_id": str(watch.get("candidate_id") or ""), "pattern_id": pattern_id},
        replay_report={"post_promotion_watch": watch},
        reason=str(watch.get("decision_reason") or ""),
        details=details,
        applied_artifact_id=pattern_id if decision in {"active", "rolled_back", "quarantined"} else "",
        budget_decision="ok" if decision in {"active", WATCH_STATUS} else "blocked",
    )
    runtime.store.sqlite._record_policy_rollout_ledger(
        action_type="shadow_observe",
        scope=scope_ref,
        promotion_id=str(watch.get("promotion_request_id") or next_rollout_id(kind="promotion-watch", scope=scope_ref, payload={"pattern_id": pattern_id})),
        source_opportunity_id=str(watch.get("candidate_id") or ""),
        source_opportunity={"candidate_id": str(watch.get("candidate_id") or ""), "pattern_id": pattern_id},
        trust_report={},
        replay_report={"post_promotion_watch": watch},
        is_auto=True,
        applied_pattern_id=pattern_id if decision == "active" else "",
        budget_decision="ok",
        reason=str(watch.get("decision_reason") or ""),
        details=details,
    )
    runtime.store.sqlite.conn.commit()


def _watch_action_for_decision(decision: str) -> str:
    if decision == "active":
        return "promoted_active"
    if decision == "quarantined":
        return "quarantined"
    if decision == "rolled_back":
        return "rolled_back"
    return "shadow_observed"


def _update_candidate_status(runtime: Any, watch: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None, status: str) -> None:
    candidate_id = str(watch.get("candidate_id") or "")
    if not candidate_id:
        return
    candidate = runtime.store.get_by_id(candidate_id, scope=scope)
    if candidate is None:
        return
    candidate.status = str(status)
    candidate.meta["post_promotion_watch"] = {
        "status": str(watch.get("status") or status),
        "pattern_id": str(watch.get("pattern_id") or ""),
        "observed_count": int(watch.get("observed_count") or 0),
        "hit_count": int(watch.get("hit_count") or 0),
        "improvement_count": int(watch.get("improvement_count") or 0),
        "regression_count": int(watch.get("regression_count") or 0),
        "bad_outcome_count": int(watch.get("bad_outcome_count") or 0),
        "failure_count": int(watch.get("failure_count") or 0),
    }
    runtime.store.rewrite(candidate)


def _update_promotion_request_status(runtime: Any, watch: dict[str, Any], *, scope: dict[str, Any] | ScopeRef | None, status: str) -> None:
    promotion_request_id = str(watch.get("promotion_request_id") or "")
    if not promotion_request_id:
        return
    record = runtime.store.get_by_id(promotion_request_id, scope=scope)
    if record is None:
        return
    summary = _watch_summary(watch, status=status)
    record.status = str(status)
    record.content = {
        **dict(record.content or {}),
        "post_promotion_status": str(status),
        "post_promotion_watch": summary,
    }
    record.meta = {
        **dict(record.meta or {}),
        "post_promotion_status": str(status),
        "post_promotion_watch": summary,
    }
    runtime.store.rewrite(record)


def _watch_summary(watch: dict[str, Any], *, status: str) -> dict[str, Any]:
    return {
        "status": str(watch.get("status") or status),
        "pattern_id": str(watch.get("pattern_id") or ""),
        "observed_count": int(watch.get("observed_count") or 0),
        "hit_count": int(watch.get("hit_count") or 0),
        "improvement_count": int(watch.get("improvement_count") or 0),
        "regression_count": int(watch.get("regression_count") or 0),
        "bad_outcome_count": int(watch.get("bad_outcome_count") or 0),
        "failure_count": int(watch.get("failure_count") or 0),
        "failure_rate": _failure_rate(watch),
    }


def _coerce_improved(value: bool | None, *, outcome: str) -> bool:
    if value is not None:
        return bool(value)
    return _improved_from_outcome({"outcome": outcome})


def _improved_from_outcome(payload: dict[str, Any]) -> bool:
    if "improved" in payload:
        return bool(payload.get("improved"))
    if "improvement" in payload:
        return bool(payload.get("improvement"))
    return str(payload.get("outcome") or "").strip().lower() in {"good", "success", "improved", "better"}


def _outcome_policy_attribution(
    runtime: Any,
    *,
    event_id: str,
    outcome_payload: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> dict[str, Any]:
    direct_ids = extract_pattern_ids_from_outcome(outcome_payload)
    if direct_ids:
        return {"pattern_ids": direct_ids, "audit_record_id": "", "selected_records": []}
    session_id = _session_id_from_outcome(runtime, event_id=event_id, outcome_payload=outcome_payload, scope=scope)
    if not session_id:
        return {"pattern_ids": [], "audit_record_id": "", "selected_records": []}
    audit = _latest_recall_audit_for_session(runtime, session_id=session_id, scope=scope)
    if not audit:
        return {"pattern_ids": [], "audit_record_id": "", "selected_records": []}
    content = audit.content if isinstance(audit.content, dict) else {}
    meta = audit.meta if isinstance(audit.meta, dict) else {}
    policy_ids = _coerce_string_list(content.get("policy_suggestion_ids") or meta.get("policy_suggestion_ids"))
    selected_records = [
        dict(item)
        for item in list(content.get("selected_records") or [])
        if isinstance(item, dict)
    ]
    return {
        "pattern_ids": policy_ids,
        "audit_record_id": audit.record_id,
        "selected_records": selected_records,
    }


def _session_id_from_outcome(
    runtime: Any,
    *,
    event_id: str,
    outcome_payload: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> str:
    for value in (
        outcome_payload.get("session_id"),
        (outcome_payload.get("policy_attribution") or {}).get("session_id")
        if isinstance(outcome_payload.get("policy_attribution"), dict)
        else "",
    ):
        text = str(value or "").strip()
        if text:
            return text
    scope_ref = _scope(scope)
    try:
        row = runtime.store.sqlite.conn.execute(
            """
            SELECT payload_json FROM events
            WHERE id = ?
              AND tenant_id = ?
              AND agent_id = ?
              AND workspace_id = ?
              AND user_id = ?
            LIMIT 1
            """,
            (str(event_id), scope_ref.tenant_id, scope_ref.agent_id, scope_ref.workspace_id, scope_ref.user_id),
        ).fetchone()
    except Exception:
        row = None
    if row is None:
        return ""
    try:
        event_payload = json.loads(str(row["payload_json"]))
    except json.JSONDecodeError:
        return ""
    return str(event_payload.get("session_id") or "").strip()


def _latest_recall_audit_for_session(
    runtime: Any,
    *,
    session_id: str,
    scope: dict[str, Any] | ScopeRef | None,
) -> RecordEnvelope | None:
    scope_ref = _scope(scope)
    lookup = getattr(runtime.store, "list_recall_audits_compact_by_session", None)
    if not callable(lookup):
        return None
    try:
        records = lookup(
            scope=scope_ref,
            session_id=session_id,
            limit=10,
        )
    except Exception:
        return None
    if not isinstance(records, list):
        return None
    for record in records:
        if record.scope != scope_ref:
            continue
        if str(record.source or "") != "openclaw.before_prompt_build":
            continue
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        if str(content.get("session_id") or meta.get("session_id") or "").strip() != session_id:
            continue
        if _coerce_string_list(content.get("policy_suggestion_ids") or meta.get("policy_suggestion_ids")):
            return record
    return None


def _coerce_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _scope(scope: dict[str, Any] | ScopeRef | None) -> ScopeRef:
    return normalize_scope(scope)


def _scope_dict(scope: dict[str, Any] | ScopeRef | None) -> dict[str, Any]:
    scope_ref = _scope(scope)
    return asdict(scope_ref)
