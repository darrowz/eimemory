from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.governance.l5_readiness import readiness_gate_status
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.governance.closure_rehearsal import verify_bootstrap_pending_readiness_contract
from eimemory.models.records import ScopeRef


_BOOTSTRAP_PENDING_RECALL_REASONS = frozenset(
    {
        "eligible_dataset_missing",
        "production_dataset_not_ready",
        "production_recall_dataset_empty",
        "production_recall_dataset_unconfigured",
    }
)


def run_release_closure(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    repo_root: str,
    current_link: str,
    health_url: str,
    prior_commit: str,
) -> dict[str, Any]:
    scope_payload = asdict(scope) if isinstance(scope, ScopeRef) else dict(scope or {})
    not_run = {"ok": False, "status": "not_run", "reason": "upstream_gate_not_run"}
    report: dict[str, Any] = {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "report_type": "l5_release_closure",
        "scope": scope_payload,
        "blocked_stage": "",
        "blocked_reason": "",
        "deployment": {},
        "record_ids": {},
        "deployment_receipt": dict(not_run),
        "production_recall_gate": dict(not_run),
        "production_recall_strict_state": dict(not_run),
        "storage_migrations": dict(not_run),
        "replay_bootstrap": dict(not_run),
        "live_acceptance": dict(not_run),
        "closure_rehearsal": dict(not_run),
        "readiness": dict(not_run),
        "bootstrap_pending_verification": dict(not_run),
    }
    identity_kwargs = {
        "scope": scope_payload,
        "repo_root": str(repo_root),
        "current_link": str(current_link),
        "health_url": str(health_url),
        "prior_commit": str(prior_commit),
    }

    receipt = runtime.verify_and_record_deployment(**identity_kwargs)
    report["deployment_receipt"] = receipt
    if receipt.get("ok") is not True:
        return _blocked(report, "deployment_receipt", _failure_reason(receipt, "deployment_receipt_failed"))
    report["deployment"] = _deployment_identity(receipt)
    report["record_ids"]["deployment_receipt"] = str(receipt.get("promotion_request_id") or "")
    from eimemory.governance.l5_readiness import _storage_migration_status

    migration_status = _storage_migration_status(runtime)
    report["storage_migrations"] = migration_status
    if migration_status.get("ok") is not True:
        return _blocked(report, "storage_migrations", "storage_migrations_pending")
    receipt_identity = ReleaseIdentity(
        commit=str(receipt.get("commit") or ""),
        version=str(receipt.get("version") or ""),
        receipt_id=str(receipt.get("promotion_request_id") or ""),
        session_id=str(receipt.get("release_session_id") or receipt.get("promotion_request_id") or ""),
    )

    run_recall = getattr(runtime, "run_configured_production_recall_gate", None)
    if not callable(run_recall):
        return _blocked(report, "production_recall_gate", "production_recall_gate_runner_unavailable")
    executed_recall_gate = run_recall(scope=scope_payload)
    report["production_recall_gate"] = executed_recall_gate
    bootstrap_pending: dict[str, Any] | None = None
    if executed_recall_gate.get("accepted") is not True:
        if not _recall_result_allows_bootstrap_pending(executed_recall_gate):
            return _blocked(
                report,
                "production_recall_gate",
                _failure_reason(executed_recall_gate, "production_recall_gate_failed"),
            )
        from eimemory.evaluation.real_query_gate import verify_current_bootstrap_data_pending

        bootstrap_pending = verify_current_bootstrap_data_pending(
            runtime,
            scope=scope_payload,
            release=receipt_identity,
        )
        if bootstrap_pending.get("ok") is not True:
            return _blocked(
                report,
                "production_recall_gate",
                _failure_reason(executed_recall_gate, "production_recall_gate_failed"),
            )
        report["production_recall_gate"] = {
            **executed_recall_gate,
            "bootstrap": bootstrap_pending,
            "status": "data_accumulating",
        }
        report["record_ids"]["production_recall_bootstrap"] = str(bootstrap_pending.get("record_id") or "")

    if bootstrap_pending is None:
        verify_recall = getattr(runtime, "verify_production_recall_gate", None)
        if not callable(verify_recall):
            return _blocked(report, "production_recall_gate", "production_recall_gate_verifier_unavailable")
        recall_gate = verify_recall(
            scope=scope_payload,
            release_identity=receipt_identity,
            limit=500,
        )
        report["production_recall_gate"] = recall_gate
        report["record_ids"]["production_recall_gate"] = str(recall_gate.get("record_id") or "")
        if recall_gate.get("ok") is not True:
            return _blocked(
                report,
                "production_recall_gate",
                _failure_reason(recall_gate, "production_recall_gate_failed"),
            )
        activate_strict = getattr(runtime, "activate_production_recall_strict_state", None)
        if not callable(activate_strict):
            return _blocked(
                report,
                "production_recall_strict_state",
                "production_recall_strict_activator_unavailable",
            )
        strict_state = activate_strict(
            scope=scope_payload,
            release_identity=receipt_identity,
            gate_record_id=str(recall_gate.get("record_id") or ""),
        )
        report["production_recall_strict_state"] = strict_state
        report["record_ids"]["production_recall_strict_state"] = str(strict_state.get("record_id") or "")
        if strict_state.get("ok") is not True or strict_state.get("status") != "strict_activated":
            return _blocked(
                report,
                "production_recall_strict_state",
                _failure_reason(strict_state, "production_recall_strict_activation_failed"),
            )

    replay_bootstrap = runtime.run_weak_capability_replay_gate(
        scope=scope_payload,
        persist=True,
        loop_id="release_closure_bootstrap",
    )
    report["replay_bootstrap"] = replay_bootstrap
    if replay_bootstrap.get("ok") is not True:
        return _blocked(
            report,
            "replay_bootstrap",
            _failure_reason(replay_bootstrap, "weak_capability_replay_failed"),
        )

    live_acceptance = runtime.run_live_task_acceptance(**identity_kwargs)
    report["live_acceptance"] = live_acceptance
    if not _live_acceptance_ok(live_acceptance, receipt=receipt):
        return _blocked(report, "live_acceptance", _failure_reason(live_acceptance, "live_acceptance_failed"))

    rehearsal_kwargs: dict[str, Any] = {
        "scope": scope_payload,
        "persist": True,
        "replay_bootstrap": replay_bootstrap,
    }
    if bootstrap_pending is not None:
        rehearsal_kwargs.update(
            {
                "bootstrap_pending": bootstrap_pending,
                "release_identity": receipt_identity,
            }
        )
    rehearsal = runtime.run_l5_closure_rehearsal(**rehearsal_kwargs)
    report["closure_rehearsal"] = rehearsal
    if not _rehearsal_gate_ok(rehearsal):
        return _blocked(report, "closure_rehearsal", _failure_reason(rehearsal, "closure_rehearsal_failed"))
    if bootstrap_pending is not None and not (
        rehearsal.get("ok") is True
        and rehearsal.get("closure_complete") is False
        and rehearsal.get("data_accumulating") is True
    ):
        return _blocked(report, "closure_rehearsal", "bootstrap_pending_rehearsal_state_invalid")

    readiness = runtime.build_l5_readiness_report(
        scope=scope_payload,
        persist=True,
        limit=1000,
        loop_id="release_closure",
    )
    report["readiness"] = readiness
    report["record_ids"]["readiness"] = str(readiness.get("persisted_record_id") or "")
    readiness_status = readiness_gate_status(readiness)
    if bootstrap_pending is not None:
        pending_verification = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=scope_payload,
            bootstrap_pending=bootstrap_pending,
            release=receipt_identity,
            readiness=readiness,
        )
        report["bootstrap_pending_verification"] = pending_verification
        if pending_verification.get("ok") is not True:
            return _blocked(
                report,
                "readiness",
                str(pending_verification.get("reason") or "bootstrap_data_pending_readiness_invalid"),
            )
        report["ok"] = True
        report["closure_complete"] = False
        report["data_accumulating"] = True
        return report
    if readiness_status != "L5":
        return _blocked(report, "readiness", "readiness_not_l5")

    report["ok"] = True
    report["closure_complete"] = True
    return report


def _blocked(report: dict[str, Any], stage: str, reason: str) -> dict[str, Any]:
    report["ok"] = False
    report["closure_complete"] = False
    report["blocked_stage"] = str(stage)
    report["blocked_reason"] = str(reason)
    return report


def _failure_reason(stage_report: dict[str, Any], fallback: str) -> str:
    error = str(
        stage_report.get("error")
        or stage_report.get("blocked_reason")
        or stage_report.get("reason")
        or ""
    ).strip()
    if error:
        return error
    blocked = [str(item).strip() for item in stage_report.get("blocked_reasons") or [] if str(item).strip()]
    return blocked[0] if blocked else fallback


def _deployment_identity(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "commit": str(receipt.get("commit") or ""),
        "version": str(receipt.get("version") or ""),
        "release_path": str(receipt.get("release_path") or ""),
        "promotion_request_id": str(receipt.get("promotion_request_id") or ""),
    }


def _live_acceptance_ok(report: dict[str, Any], *, receipt: dict[str, Any]) -> bool:
    deployment = report.get("deployment") if isinstance(report.get("deployment"), dict) else {}
    return bool(
        report.get("ok") is True
        and int(report.get("case_count") or 0) == 10
        and int(report.get("pass_count") or 0) == 10
        and int(report.get("fail_count") or 0) == 0
        and int(report.get("distinct_task_types") or 0) == 10
        and deployment == _deployment_identity(receipt)
    )


def _rehearsal_gate_ok(rehearsal: dict[str, Any]) -> bool:
    complete = rehearsal.get("closure_complete") is True
    accumulating = rehearsal.get("data_accumulating") is True
    return bool(rehearsal.get("ok") is True and complete != accumulating)


def _recall_result_allows_bootstrap_pending(report: dict[str, Any]) -> bool:
    return _missing_dataset_recall_result(report) or _passing_diagnostic_recall_result(report)


def _missing_dataset_recall_result(report: dict[str, Any]) -> bool:
    threshold = report.get("threshold_gate") if isinstance(report.get("threshold_gate"), dict) else {}
    blocking_metrics = threshold.get("blocking_metrics")
    cross_channel_leakage = report.get("cross_channel_leakage_count")
    source_filter_leakage = report.get("source_filter_leakage_count")
    return bool(
        report.get("ok") is False
        and report.get("accepted") is False
        and report.get("gate_status") == "not_run"
        and str(report.get("blocked_reason") or "") in _BOOTSTRAP_PENDING_RECALL_REASONS
        and _zero_or_missing(cross_channel_leakage)
        and _zero_or_missing(source_filter_leakage)
        and (blocking_metrics is None or blocking_metrics == {})
    )


def _passing_diagnostic_recall_result(report: dict[str, Any]) -> bool:
    quality = report.get("quality_gate") if isinstance(report.get("quality_gate"), dict) else {}
    return bool(
        report.get("ok") is True
        and report.get("accepted") is False
        and report.get("gate_status") == "diagnostic"
        and report.get("dataset_kind") == "diagnostic"
        and report.get("gate_ok") is True
        and report.get("passed_threshold") is True
        and report.get("blocked_reason") == ""
        and quality.get("ok") is True
        and quality.get("blocked_reason") == ""
        and quality.get("blocking_metrics") == {}
        and report.get("errors") == []
        and type(report.get("seed_error_count")) is int
        and report.get("seed_error_count") == 0
        and type(report.get("sample_count")) is int
        and int(report.get("sample_count")) > 0
        and _exact_zero_number(report.get("false_recall_rate"))
        and _exact_zero_number(report.get("forbidden_hit_rate"))
        and _exact_zero_int(report.get("cross_channel_leakage_count"))
        and _exact_zero_int(report.get("source_filter_leakage_count"))
    )


def _exact_zero_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == 0.0


def _exact_zero_int(value: Any) -> bool:
    return type(value) is int and value == 0


def _zero_or_missing(value: Any) -> bool:
    return value is None or type(value) is int and value == 0
