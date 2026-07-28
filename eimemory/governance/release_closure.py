from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from eimemory.governance.l5_readiness import readiness_gate_status
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.governance.closure_rehearsal import verify_bootstrap_pending_readiness_contract
from eimemory.governance.live_task_acceptance import LIVE_ACCEPTANCE_CASE_IDS
from eimemory.models.records import ScopeRef


_BOOTSTRAP_PENDING_RECALL_REASONS = frozenset(
    {
        "eligible_dataset_missing",
        "production_dataset_not_ready",
        "production_recall_dataset_empty",
        "production_recall_dataset_unconfigured",
        "query_features_low_signal",
    }
)
_BOOTSTRAP_DIAGNOSTIC_LATENCY_MULTIPLIER = 1.20
_BOOTSTRAP_DIAGNOSTIC_MAX_LATENCY_SAMPLES = 10


def run_release_closure(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    repo_root: str,
    current_link: str,
    health_url: str,
    prior_commit: str,
    pending_path: str | Path | None = None,
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
        "channel_acceptance": dict(not_run),
        "release_lineage": dict(not_run),
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

    record_channel_acceptance = getattr(
        runtime, "record_openclaw_channel_acceptance", None
    )
    if not callable(record_channel_acceptance):
        return _blocked(
            report,
            "channel_acceptance",
            "openclaw_channel_acceptance_recorder_unavailable",
        )
    channel_acceptance = record_channel_acceptance(
        scope=scope_payload,
        current_release=receipt_identity,
    )
    report["channel_acceptance"] = channel_acceptance
    report["record_ids"]["channel_acceptance"] = str(
        channel_acceptance.get("record_id") or ""
    )
    if channel_acceptance.get("ok") is not True:
        blocked = _blocked(
            report,
            "channel_acceptance",
            _failure_reason(
                channel_acceptance,
                "current_release_channel_acceptance_missing",
            ),
        )
        if blocked["blocked_reason"] == "current_release_channel_receipt_not_found":
            from eimemory.governance.release_closure_pending import (
                build_release_closure_pending,
                write_release_closure_pending,
            )

            checkpoint = build_release_closure_pending(
                scope=scope_payload,
                repo_root=repo_root,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
                current_release=receipt_identity,
                release_path=str(receipt.get("release_path") or ""),
                record_ids=report["record_ids"],
                replay_bootstrap=replay_bootstrap,
                live_acceptance=live_acceptance,
                bootstrap_pending=bootstrap_pending,
            )
            blocked["pending_checkpoint"] = write_release_closure_pending(
                checkpoint,
                path=pending_path,
            )
            if blocked["pending_checkpoint"].get("ok") is True:
                from eimemory.governance.release_closure_pending import (
                    reconcile_release_closure_pending,
                )

                reconciled = reconcile_release_closure_pending(
                    runtime,
                    pending_path=pending_path,
                )
                if reconciled.get("report_type") == "l5_release_closure":
                    return reconciled
                blocked["post_write_reconcile"] = reconciled
        return blocked

    return _continue_release_closure(
        runtime,
        report=report,
        scope_payload=scope_payload,
        repo_root=repo_root,
        current_release=receipt_identity,
        replay_bootstrap=replay_bootstrap,
        live_acceptance=live_acceptance,
        bootstrap_pending=bootstrap_pending,
    )


def _continue_release_closure(
    runtime: Any,
    *,
    report: dict[str, Any],
    scope_payload: dict[str, Any],
    repo_root: str,
    current_release: ReleaseIdentity,
    replay_bootstrap: dict[str, Any],
    live_acceptance: dict[str, Any],
    bootstrap_pending: dict[str, Any] | None,
) -> dict[str, Any]:
    rehearsal_kwargs: dict[str, Any] = {
        "scope": scope_payload,
        "persist": True,
        "replay_bootstrap": replay_bootstrap,
        "repo_root": repo_root,
    }
    lineage_finalizer_enabled = bool(
        callable(getattr(runtime, "record_release_lineage", None))
        and callable(getattr(runtime, "current_release_lineage", None))
    )
    if lineage_finalizer_enabled:
        rehearsal_kwargs["release_lineage_finalizer"] = (
            lambda core_replay: _finalize_release_lineage(
                runtime,
                scope=scope_payload,
                repo_root=repo_root,
                current_release=current_release,
                receipt_record_id=current_release.receipt_id,
                recall_gate_record_id=str(report["record_ids"].get("production_recall_gate") or ""),
                strict_state_record_id=str(
                    report["record_ids"].get("production_recall_strict_state") or ""
                ),
                bootstrap_pending_record_id=str(
                    report["record_ids"].get("production_recall_bootstrap") or ""
                ),
                channel_acceptance_record_id=str(
                    report["record_ids"].get("channel_acceptance") or ""
                ),
                weak_replay=replay_bootstrap,
                core_replay=core_replay,
                live_acceptance=live_acceptance,
            )
        )
    if bootstrap_pending is not None:
        rehearsal_kwargs.update(
            {
                "bootstrap_pending": bootstrap_pending,
                "release_identity": current_release,
            }
        )
    rehearsal = runtime.run_l5_closure_rehearsal(**rehearsal_kwargs)
    report["closure_rehearsal"] = rehearsal
    rehearsal_lineage = (
        rehearsal.get("release_lineage")
        if isinstance(rehearsal.get("release_lineage"), dict)
        else {}
    )
    if rehearsal_lineage:
        report["release_lineage"] = rehearsal_lineage
        report["record_ids"]["release_lineage"] = str(
            rehearsal_lineage.get("record_id") or ""
        )
    if not _rehearsal_gate_ok(rehearsal):
        return _blocked(report, "closure_rehearsal", _failure_reason(rehearsal, "closure_rehearsal_failed"))
    if bootstrap_pending is not None and not (
        rehearsal.get("ok") is True
        and rehearsal.get("closure_complete") is False
        and rehearsal.get("data_accumulating") is True
    ):
        return _blocked(report, "closure_rehearsal", "bootstrap_pending_rehearsal_state_invalid")

    readiness = (
        rehearsal.get("l5_readiness")
        if isinstance(rehearsal.get("l5_readiness"), dict)
        and rehearsal["l5_readiness"].get("schema_version") == "l5_readiness.v2"
        else runtime.build_l5_readiness_report(
            scope=scope_payload,
            persist=True,
            limit=1000,
            loop_id="release_closure",
            **({"repo_root": repo_root} if lineage_finalizer_enabled else {}),
        )
    )
    report["readiness"] = readiness
    report["record_ids"]["readiness"] = str(readiness.get("persisted_record_id") or "")
    readiness_status = readiness_gate_status(
        readiness,
        runtime=runtime,
        scope=scope_payload,
        repo_root=repo_root,
    )
    if bootstrap_pending is not None:
        pending_verification = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=scope_payload,
            bootstrap_pending=bootstrap_pending,
            release=current_release,
            readiness=readiness,
            repo_root=repo_root,
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


def resume_release_closure(
    runtime: Any,
    *,
    checkpoint: dict[str, Any],
    current_release: ReleaseIdentity,
    channel_acceptance: dict[str, Any],
) -> dict[str, Any]:
    scope_payload = dict(checkpoint.get("scope") or {})
    inputs = dict(checkpoint.get("inputs") or {})
    record_ids = dict(checkpoint.get("passed_gate_record_ids") or {})
    reports = dict(checkpoint.get("passed_gate_reports") or {})
    replay_bootstrap = dict(reports.get("replay_bootstrap") or {})
    live_acceptance = dict(reports.get("live_acceptance") or {})
    bootstrap_pending_raw = dict(reports.get("bootstrap_pending") or {})
    bootstrap_pending = bootstrap_pending_raw or None
    not_run = {"ok": False, "status": "not_run", "reason": "upstream_gate_not_run"}
    report: dict[str, Any] = {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "report_type": "l5_release_closure",
        "scope": scope_payload,
        "blocked_stage": "",
        "blocked_reason": "",
        "deployment": {
            "commit": current_release.commit,
            "version": current_release.version,
            "release_path": str(checkpoint.get("release_path") or ""),
            "promotion_request_id": current_release.receipt_id,
        },
        "record_ids": record_ids,
        "deployment_receipt": {
            "ok": True,
            "status": "checkpointed",
            "promotion_request_id": current_release.receipt_id,
        },
        "production_recall_gate": {"ok": True, "status": "checkpointed"},
        "production_recall_strict_state": {"ok": True, "status": "checkpointed"},
        "storage_migrations": {"ok": True, "status": "checkpointed"},
        "replay_bootstrap": replay_bootstrap,
        "live_acceptance": live_acceptance,
        "channel_acceptance": dict(channel_acceptance),
        "release_lineage": dict(not_run),
        "closure_rehearsal": dict(not_run),
        "readiness": dict(not_run),
        "bootstrap_pending_verification": dict(not_run),
    }
    report["record_ids"]["channel_acceptance"] = str(
        channel_acceptance.get("record_id") or ""
    )
    return _continue_release_closure(
        runtime,
        report=report,
        scope_payload=scope_payload,
        repo_root=str(inputs.get("repo_root") or ""),
        current_release=current_release,
        replay_bootstrap=replay_bootstrap,
        live_acceptance=live_acceptance,
        bootstrap_pending=bootstrap_pending,
    )


def _finalize_release_lineage(
    runtime: Any,
    *,
    scope: dict[str, Any],
    repo_root: str,
    current_release: ReleaseIdentity,
    receipt_record_id: str,
    recall_gate_record_id: str,
    strict_state_record_id: str,
    bootstrap_pending_record_id: str,
    channel_acceptance_record_id: str,
    weak_replay: dict[str, Any],
    core_replay: dict[str, Any],
    live_acceptance: dict[str, Any],
) -> dict[str, Any]:
    weak = (
        weak_replay.get("weak_capability_replay")
        if isinstance(weak_replay.get("weak_capability_replay"), dict)
        else {}
    )
    weak_manifest = str(weak.get("manifest_record_id") or "").strip()
    core_manifest = str(core_replay.get("manifest_record_id") or "").strip()
    live_record_ids = _canonical_live_case_record_ids(live_acceptance)
    if (
        not receipt_record_id
        or receipt_record_id != current_release.receipt_id
        or not weak_manifest
        or not core_manifest
        or not channel_acceptance_record_id
        or live_record_ids is None
        or bool(recall_gate_record_id) != bool(strict_state_record_id)
        or bool(bootstrap_pending_record_id)
        == bool(recall_gate_record_id and strict_state_record_id)
    ):
        return {
            "ok": False,
            "validated": False,
            "compatible": False,
            "error": "release_lineage_gate_references_incomplete",
        }
    recall_references = (
        [bootstrap_pending_record_id, core_manifest]
        if bootstrap_pending_record_id
        else [recall_gate_record_id, strict_state_record_id]
    )
    gate_evidence = {
        "memory.recall": recall_references,
        "memory.governance": [weak_manifest, core_manifest],
        "channel.openclaw": [channel_acceptance_record_id],
        "storage.integrity": live_record_ids,
        "deployment.runtime": [receipt_record_id],
    }
    recorded = runtime.record_release_lineage(
        scope=scope,
        repo_root=repo_root,
        current_release=current_release,
        gate_evidence=gate_evidence,
    )
    if not isinstance(recorded, dict) or recorded.get("ok") is not True:
        return (
            recorded
            if isinstance(recorded, dict)
            else {
                "ok": False,
                "validated": False,
                "compatible": False,
                "error": "release_lineage_record_failed",
            }
        )
    resolved = runtime.current_release_lineage(
        scope=scope,
        repo_root=repo_root,
        current_release=current_release,
    )
    if (
        not isinstance(resolved, dict)
        or resolved.get("ok") is not True
        or resolved.get("validated") is not True
        or str(resolved.get("record_id") or "") != str(recorded.get("record_id") or "")
    ):
        return {
            "ok": False,
            "validated": False,
            "compatible": False,
            "error": "release_lineage_revalidation_failed",
        }
    return resolved


def _canonical_live_case_record_ids(
    live_acceptance: dict[str, Any],
) -> list[str] | None:
    cases = live_acceptance.get("cases")
    if not isinstance(cases, list):
        return None
    by_case: dict[str, str] = {}
    for item in cases:
        if not isinstance(item, dict):
            return None
        case_id = str(item.get("case_id") or "").strip()
        record_id = str(item.get("record_id") or "").strip()
        if not case_id or not record_id or case_id in by_case:
            return None
        by_case[case_id] = record_id
    if set(by_case) != set(LIVE_ACCEPTANCE_CASE_IDS):
        return None
    ordered = [by_case[case_id] for case_id in LIVE_ACCEPTANCE_CASE_IDS]
    return ordered if len(set(ordered)) == len(ordered) else None


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
    expected = _deployment_identity(receipt)
    return bool(
        report.get("ok") is True
        and int(report.get("case_count") or 0) == 10
        and int(report.get("pass_count") or 0) == 10
        and int(report.get("fail_count") or 0) == 0
        and int(report.get("distinct_task_types") or 0) == 10
        and str(deployment.get("commit") or "") == expected["commit"]
        and str(deployment.get("release_path") or "") == expected["release_path"]
        and str(deployment.get("promotion_request_id") or "")
        == expected["promotion_request_id"]
    )


def _rehearsal_gate_ok(rehearsal: dict[str, Any]) -> bool:
    complete = rehearsal.get("closure_complete") is True
    accumulating = rehearsal.get("data_accumulating") is True
    return bool(rehearsal.get("ok") is True and complete != accumulating)


def _recall_result_allows_bootstrap_pending(report: dict[str, Any]) -> bool:
    return bool(
        _missing_dataset_recall_result(report)
        or _passing_diagnostic_recall_result(report)
        or _bounded_latency_only_diagnostic_recall_result(report)
    )


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


def _bounded_latency_only_diagnostic_recall_result(report: dict[str, Any]) -> bool:
    quality = report.get("quality_gate") if isinstance(report.get("quality_gate"), dict) else {}
    blocking = (
        quality.get("blocking_metrics")
        if isinstance(quality.get("blocking_metrics"), dict)
        else {}
    )
    latency = blocking.get("latency_ms_p95") if len(blocking) == 1 else None
    latency = latency if isinstance(latency, dict) else {}
    thresholds = quality.get("thresholds") if isinstance(quality.get("thresholds"), dict) else {}
    actual = latency.get("actual")
    threshold = latency.get("threshold")
    sample_count = report.get("sample_count")
    numeric = (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
    )
    bootstrap_smoke_sample = (
        type(sample_count) is int
        and 0 < sample_count <= _BOOTSTRAP_DIAGNOSTIC_MAX_LATENCY_SAMPLES
    )
    bounded_latency = bool(
        numeric
        and bootstrap_smoke_sample
        and float(threshold) > 0.0
        and float(actual) > float(threshold)
        and float(actual)
        <= float(threshold) * _BOOTSTRAP_DIAGNOSTIC_LATENCY_MULTIPLIER
        and latency.get("operator") == "<="
        and report.get("latency_ms_p95") == actual
        and thresholds.get("latency_ms_p95") == threshold
    )
    return bool(
        report.get("ok") is False
        and report.get("accepted") is False
        and report.get("gate_status") == "diagnostic"
        and report.get("dataset_kind") == "diagnostic"
        and report.get("gate_ok") is False
        and report.get("passed_threshold") is False
        and report.get("blocked_reason") == "recall_quality_gate_failed"
        and quality.get("ok") is False
        and quality.get("blocked_reason") == "recall_quality_gate_failed"
        and bounded_latency
        and report.get("errors") == []
        and type(report.get("seed_error_count")) is int
        and report.get("seed_error_count") == 0
        and bootstrap_smoke_sample
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
