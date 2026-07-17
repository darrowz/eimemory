from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.live_task_acceptance import validate_live_acceptance_case
from eimemory.governance.rollout_lifecycle import is_executed_rollback_ledger_record
from eimemory.models.records import ScopeRef
from eimemory.runtime_identity import package_import_root


SUCCESS_LABELS = {
    "1",
    "true",
    "yes",
    "ok",
    "good",
    "success",
    "succeeded",
    "pass",
    "passed",
    "complete",
    "completed",
    "delivered",
    "done",
    "verified",
    "health_ok",
    "all_ok",
    "ready",
    "readyz_ok",
}
FAILURE_LABELS = {
    "0",
    "false",
    "no",
    "bad",
    "fail",
    "failed",
    "failure",
    "error",
    "errored",
    "timeout",
    "timed_out",
    "rollback",
    "rolled_back",
    "quarantined",
    "verification_missing",
    "missing_verification",
}


def build_capability_dashboard_metrics(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    limit: int = 500,
    loop_id: str = "capability_dashboard_1_6_9",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    recall_replays = _records(runtime, scope_ref, ["replay_result"], limit)
    recall_items = [record for record in recall_replays if _capability(record) == "memory.recall" or _has_key(record, "hit")]
    recall_hits = sum(1 for record in recall_items if _truthy(_field(record, "hit")) or _verdict(record) == "pass")
    recall_total = len(recall_items)

    corrections = [
        record
        for record in _records(runtime, scope_ref, ["feedback", "incident", "reflection"], limit)
        if str(_field(record, "report_type") or "").lower() in {"user_correction", "operator_correction"}
        or "correction" in (record.title + " " + record.summary).lower()
    ]

    evals = _records(runtime, scope_ref, ["learning_eval"], limit)
    task_evals = [record for record in evals if _field(record, "task_success") is not None]
    task_outcomes = [
        record
        for record in task_evals + _outcome_trace_records(runtime, scope_ref, limit) + _event_outcome_records(runtime, scope_ref, limit)
        if not _truthy(_field(record, "rehearsal"))
    ]
    task_success = sum(1 for record in task_outcomes if _outcome_success(record))
    verified_live_tasks = _verified_live_task_outcomes(
        runtime,
        scope=scope_ref,
        records=_outcome_trace_records(runtime, scope_ref, limit),
    )
    verified_live_success = sum(1 for item in verified_live_tasks if item["success"] is True)
    verified_live_task_types = {str(item.get("task_type") or "") for item in verified_live_tasks}
    current_deployment_commit = _latest_verified_deployment_commit(runtime, scope=scope_ref, limit=limit)
    current_deployment_tasks = [
        item
        for item in verified_live_tasks
        if item.get("evidence_class") == "live_acceptance"
        and str(item.get("deployment_commit") or "") == current_deployment_commit
    ]
    current_deployment_success = sum(1 for item in current_deployment_tasks if item["success"] is True)
    current_deployment_task_types = {str(item.get("task_type") or "") for item in current_deployment_tasks}

    promotions = _records(runtime, scope_ref, ["promotion_request"], limit)
    patch_promotions = [
        record
        for record in promotions
        if str(_field(record, "promotion_target") or "").lower() == "code_patch"
        or str(_field(record, "action") or "").lower() == "code_patch"
    ]
    latest_patch_candidates = _latest_patch_candidate_records(patch_promotions)
    valid_patch_candidates = sum(1 for record in latest_patch_candidates if _valid_code_patch_candidate(record))
    executed_patch_deployments = [record for record in latest_patch_candidates if _executed_code_patch_deployment(record)]
    patch_success = sum(1 for record in executed_patch_deployments if _verified_code_patch_promotion(record))
    policy_rollbacks = _policy_rollback_records(runtime, scope_ref, limit)
    rollback_count = len(policy_rollbacks)
    skill_invocations = sum(1 for record in evals if str(_field(record, "report_type") or "") == "eiskill_invocation")
    skill_registry_reuse = sum(
        max(0, _int(_field(record, "reuse_count")))
        for record in _records(runtime, scope_ref, ["learning_playbook"], limit)
        if str(_field(record, "report_type") or "") == "eiskill_registry_entry"
    )
    skill_reuse_count = max(skill_invocations, skill_registry_reuse)

    patch_candidate_validity_rate = _rate(valid_patch_candidates, len(latest_patch_candidates))
    patch_deployment_success_rate = _rate(patch_success, len(executed_patch_deployments))
    patch_metric_quality = _quality(len(executed_patch_deployments), minimum=1)
    metrics = {
        "recall_hit_rate": _rate(recall_hits, recall_total),
        "user_correction_rate": _rate(len(corrections), recall_total),
        "task_success_rate": _rate(task_success, len(task_outcomes)),
        "verified_live_task_success_rate": _rate(verified_live_success, len(verified_live_tasks)),
        "current_deployment_live_task_success_rate": _rate(current_deployment_success, len(current_deployment_tasks)),
        "patch_candidate_validity_rate": patch_candidate_validity_rate,
        "patch_deployment_success_rate": patch_deployment_success_rate,
        "patch_promotion_success_rate": patch_deployment_success_rate,
        "auto_patch_success_rate": patch_deployment_success_rate,
        "rollback_count": rollback_count,
        "skill_reuse_count": skill_reuse_count,
    }
    metric_quality = {
        "recall_hit_rate": _quality(recall_total),
        "user_correction_rate": _quality(recall_total),
        "task_success_rate": _quality(len(task_outcomes)),
        "verified_live_task_success_rate": _quality(len(verified_live_tasks)),
        "current_deployment_live_task_success_rate": _quality(len(current_deployment_tasks)),
        "patch_candidate_validity_rate": _quality(len(latest_patch_candidates), minimum=1),
        "patch_deployment_success_rate": patch_metric_quality,
        "patch_promotion_success_rate": patch_metric_quality,
        "auto_patch_success_rate": patch_metric_quality,
        "rollback_count": _quality(len(policy_rollbacks), minimum=1),
        "skill_reuse_count": _quality(skill_reuse_count, minimum=1),
    }
    record_id = ""
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="reflection",
            title="Capability dashboard hard metrics",
            summary=(
                f"recall_hit_rate={metrics['recall_hit_rate']}; "
                f"task_success_rate={metrics['task_success_rate']}; "
                f"patch_success_rate={metrics['patch_promotion_success_rate']}"
            ),
            scope=scope_ref,
            loop_id=loop_id,
            step_name="capability_dashboard_metrics",
            semantic_key=stable_semantic_key("capability_dashboard_metrics", scope_ref, metrics),
            authority_tier="L0",
            status="active",
            content={"report_type": "capability_dashboard_metrics", "metrics": metrics, "metric_quality": metric_quality},
            meta={"report_type": "capability_dashboard_metrics", **metrics, "metric_quality": metric_quality},
            source="eimemory.capability_dashboard",
        )
        record_id = record.record_id
    return {
        "ok": True,
        "report_type": "capability_dashboard_metrics",
        "scope": asdict(scope_ref),
        "metrics": metrics,
        "metric_quality": metric_quality,
        "persisted_record_id": record_id,
        "sample_counts": {
            "recall": recall_total,
            "corrections": len(corrections),
            "task_evals": len(task_evals),
            "task_outcomes": len(task_outcomes),
            "verified_live_tasks": len(verified_live_tasks),
            "verified_live_task_types": len(verified_live_task_types),
            "current_deployment_acceptance": len(current_deployment_tasks),
            "current_deployment_live_task_types": len(current_deployment_task_types),
            "patch_candidates": len(latest_patch_candidates),
            "patch_deployments": len(executed_patch_deployments),
            "patch_promotions": len(executed_patch_deployments),
            "policy_rollbacks": len(policy_rollbacks),
        },
    }


def _records(runtime: Any, scope: ScopeRef, kinds: list[str], limit: int) -> list[Any]:
    return runtime.store.list_records(kinds=kinds, scope=scope, limit=max(0, int(limit)))


def _outcome_trace_records(runtime: Any, scope: ScopeRef, limit: int) -> list[Any]:
    records = _records(runtime, scope, ["reflection"], limit)
    return [
        record
        for record in records
        if str(_field(record, "report_type") or "") == "outcome_trace" and _has_outcome_signal(record)
    ]


def _event_outcome_records(runtime: Any, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    sqlite = getattr(getattr(runtime, "store", None), "sqlite", None)
    conn = getattr(sqlite, "conn", None)
    if conn is None:
        return []
    rows = conn.execute(
        """
        SELECT payload_json
        FROM event_outcomes
        WHERE tenant_id = ?
          AND agent_id = ?
          AND workspace_id = ?
          AND user_id = ?
        ORDER BY recorded_at DESC
        LIMIT ?
        """,
        (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, max(0, int(limit))),
    ).fetchall()
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            continue
        payload.setdefault("report_type", "event_outcome")
        outcomes.append(payload)
    return outcomes


def _verified_live_task_outcomes(
    runtime: Any,
    *,
    scope: ScopeRef,
    records: list[Any],
) -> list[dict[str, Any]]:
    outcomes: list[dict[str, Any]] = []
    seen_acceptance_cases: set[tuple[str, str]] = set()
    for record in records:
        if str(getattr(record, "source", "") or "") != "eimemory.experience.outcome_trace":
            continue
        if str(_field(record, "report_type") or "") != "outcome_trace":
            continue
        if str(_field(record, "schema_version") or "") != "outcome_trace.v1":
            continue
        task_type = str(_field(record, "task_type") or "").strip()
        outcome = _field(record, "outcome")
        verifier = _field(record, "verifier")
        if (
            not task_type
            or not isinstance(outcome, dict)
            or outcome.get("rehearsal") is not False
            or not isinstance(outcome.get("success"), bool)
            or not isinstance(verifier, dict)
            or not isinstance(verifier.get("passed"), bool)
        ):
            continue
        evidence_refs = verifier.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs or not all(str(item or "").strip() for item in evidence_refs):
            continue
        method = str(verifier.get("method") or "").strip()
        if method == "eimemory.live_task_acceptance":
            case_id = str(_field(record, "acceptance_case_id") or "").strip()
            deployment_commit = str(_field(record, "deployment_commit") or "").strip()
            key = (deployment_commit, case_id)
            if not case_id or len(deployment_commit) != 40 or key in seen_acceptance_cases:
                continue
            if not _valid_live_acceptance_evidence(
                runtime,
                scope=scope,
                evidence_id=str(evidence_refs[0]),
                case_id=case_id,
                task_type=task_type,
                trace_id=str(_field(record, "trace_id") or ""),
                deployment_commit=deployment_commit,
                passed=verifier.get("passed"),
            ):
                continue
            seen_acceptance_cases.add(key)
        else:
            continue
        outcomes.append(
            {
                "record_id": _record_id(record),
                "task_type": task_type,
                "evidence_class": "live_acceptance",
                "deployment_commit": str(_field(record, "deployment_commit") or ""),
                "success": outcome.get("success") is True and verifier.get("passed") is True,
            }
        )
    return outcomes


def _valid_live_acceptance_evidence(
    runtime: Any,
    *,
    scope: ScopeRef,
    evidence_id: str,
    case_id: str,
    task_type: str,
    trace_id: str,
    deployment_commit: str,
    passed: bool,
) -> bool:
    evidence = runtime.store.get_by_id(evidence_id, scope=scope)
    if evidence is None:
        return False
    return validate_live_acceptance_case(
        runtime,
        scope=scope,
        evidence=evidence,
        case_id=case_id,
        task_type=task_type,
        trace_id=trace_id,
        deployment_commit=deployment_commit,
        passed=passed,
    )


def _latest_verified_deployment_commit(runtime: Any, *, scope: ScopeRef, limit: int) -> str:
    actual_commit, _production_runtime = _actual_runtime_commit()
    test_override = False
    if not actual_commit and os.environ.get("PYTEST_CURRENT_TEST"):
        test_commit = str(getattr(runtime, "_test_runtime_commit", "") or "").strip().lower()
        actual_commit = test_commit if re.fullmatch(r"[0-9a-f]{40}", test_commit) else ""
        test_override = bool(actual_commit)
    for record in _records(runtime, scope, ["promotion_request"], limit):
        if str(getattr(record, "source", "") or "") != "eimemory.deployment_receipt":
            continue
        if not _verified_code_patch_promotion(record):
            continue
        content = record.content if isinstance(getattr(record, "content", None), dict) else {}
        side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
        commit = side_effect.get("commit") if isinstance(side_effect.get("commit"), dict) else {}
        commit_sha = str(commit.get("commit_sha") or _field(record, "commit_sha") or "").strip()
        if len(commit_sha) == 40:
            if actual_commit:
                if commit_sha != actual_commit:
                    continue
                if not test_override and not _runtime_import_matches_receipt(record, commit_sha=commit_sha):
                    return ""
                return commit_sha
            return ""
    return ""


def _actual_runtime_commit() -> tuple[str, bool]:
    configured = str(os.environ.get("EIMEMORY_RUNTIME_COMMIT") or "").strip().lower()
    root = package_import_root()
    root_commit = ""
    for release in (root, *root.parents):
        releases_root = str(release.parent).replace("\\", "/").rstrip("/").casefold()
        if releases_root == "/opt/eimemory/releases" and re.fullmatch(r"[0-9a-f]{40}", release.name):
            root_commit = release.name.lower()
            break
    try:
        production_runtime = root.is_relative_to(Path("/opt/eimemory"))
    except (OSError, ValueError):
        production_runtime = False
    if re.fullmatch(r"[0-9a-f]{40}", configured) and root_commit and configured != root_commit:
        return "", True
    if root_commit:
        return root_commit, True
    return "", production_runtime


def _runtime_import_matches_receipt(record: Any, *, commit_sha: str) -> bool:
    content = record.content if isinstance(getattr(record, "content", None), dict) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
    release = side_effect.get("release") if isinstance(side_effect.get("release"), dict) else {}
    try:
        receipt_release = Path(str(release.get("release_path") or "")).resolve(strict=True)
        canonical_release = (Path("/opt/eimemory/releases") / commit_sha).resolve(strict=True)
        import_root = package_import_root().resolve(strict=True)
    except OSError:
        return False
    return receipt_release == canonical_release and import_root.is_relative_to(receipt_release)


def _policy_rollback_records(runtime: Any, scope: ScopeRef, limit: int) -> list[dict[str, Any]]:
    getter = getattr(runtime, "get_policy_rollout_ledger", None)
    if not callable(getter):
        return []
    try:
        records = getter(scope=scope, limit=max(0, int(limit)))
    except Exception:
        return []
    return [record for record in records if isinstance(record, dict) and is_executed_rollback_ledger_record(record)]


def _field(record: Any, key: str) -> Any:
    if isinstance(record, dict):
        meta = record.get("meta")
        content = record.get("content")
        for payload in (meta, content):
            if isinstance(payload, dict) and key in payload:
                return payload.get(key)
        nested = content.get("payload") if isinstance(content, dict) and isinstance(content.get("payload"), dict) else {}
        if key in nested:
            return nested.get(key)
        nested_outcome = nested.get("outcome") if isinstance(nested.get("outcome"), dict) else {}
        if key in nested_outcome:
            return nested_outcome.get(key)
        if key in record:
            return record.get(key)
        return None
    content = getattr(record, "content", {}) or {}
    for payload in (getattr(record, "meta", {}) or {}, content):
        if isinstance(payload, dict) and key in payload:
            return payload.get(key)
    nested = content.get("payload") if isinstance(content, dict) and isinstance(content.get("payload"), dict) else {}
    if key in nested:
        return nested.get(key)
    nested_outcome = nested.get("outcome") if isinstance(nested.get("outcome"), dict) else {}
    if key in nested_outcome:
        return nested_outcome.get(key)
    return None


def _verified_code_patch_promotion(record: Any) -> bool:
    status = str(record.get("status", "") if isinstance(record, dict) else getattr(record, "status", "") or "").lower()
    if status not in {"promoted", "active", "deployed"}:
        return False
    content = record.get("content") if isinstance(record, dict) else getattr(record, "content", {})
    meta = record.get("meta") if isinstance(record, dict) else getattr(record, "meta", {})
    content = content if isinstance(content, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    gate = content.get("gate") if isinstance(content.get("gate"), dict) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
    verification = side_effect.get("verification") if isinstance(side_effect.get("verification"), dict) else {}
    deployment = side_effect.get("deployment") if isinstance(side_effect.get("deployment"), dict) else {}
    health = side_effect.get("post_deploy_health") if isinstance(side_effect.get("post_deploy_health"), dict) else {}
    commit = side_effect.get("commit") if isinstance(side_effect.get("commit"), dict) else {}
    release = side_effect.get("release") if isinstance(side_effect.get("release"), dict) else {}
    rollback = side_effect.get("rollback_evidence") if isinstance(side_effect.get("rollback_evidence"), dict) else {}
    commit_sha = str(commit.get("commit_sha") or meta.get("commit_sha") or "").strip()
    version = str(release.get("version") or meta.get("version") or "").strip()
    release_path = str(release.get("release_path") or meta.get("release_path") or "").strip()
    return bool(
        _gate_passed(gate, meta)
        and side_effect.get("ok") is True
        and side_effect.get("production_applied") is True
        and verification.get("ok") is True
        and verification.get("skipped") is not True
        and deployment.get("ok") is True
        and deployment.get("skipped") is not True
        and health.get("ok") is True
        and health.get("skipped") is not True
        and commit_sha
        and version
        and release_path
        and str(health.get("commit") or "") == commit_sha
        and str(health.get("version") or "") == version
        and _same_path(health.get("release_path"), release_path)
        and _same_path(deployment.get("release_path"), release_path)
        and str(rollback.get("prior_commit_sha") or "").strip()
        and str(rollback.get("rollback_command") or "").strip()
    )


def _latest_patch_candidate_records(records: list[Any]) -> list[Any]:
    latest: dict[str, Any] = {}
    for record in records:
        candidate_id = str(_field(record, "candidate_id") or _record_id(record)).strip()
        if candidate_id and candidate_id not in latest:
            latest[candidate_id] = record
    return list(latest.values())


def _valid_code_patch_candidate(record: Any) -> bool:
    content = record.get("content") if isinstance(record, dict) else getattr(record, "content", {})
    meta = record.get("meta") if isinstance(record, dict) else getattr(record, "meta", {})
    content = content if isinstance(content, dict) else {}
    meta = meta if isinstance(meta, dict) else {}
    gate = content.get("gate") if isinstance(content.get("gate"), dict) else {}
    return _gate_passed(gate, meta)


def _gate_passed(gate: dict[str, Any], meta: dict[str, Any]) -> bool:
    if "ok" in gate:
        return gate.get("ok") is True
    return meta.get("gate_ok") is True


def _executed_code_patch_deployment(record: Any) -> bool:
    if not _valid_code_patch_candidate(record):
        return False
    content = record.get("content") if isinstance(record, dict) else getattr(record, "content", {})
    content = content if isinstance(content, dict) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
    deployment = side_effect.get("deployment") if isinstance(side_effect.get("deployment"), dict) else {}
    return side_effect.get("deployment_executed") is True and deployment.get("skipped") is not True


def _record_id(record: Any) -> str:
    if isinstance(record, dict):
        return str(record.get("record_id") or record.get("id") or "")
    return str(getattr(record, "record_id", "") or "")


def _same_path(left: Any, right: Any) -> bool:
    return str(left or "").replace("\\", "/").rstrip("/").casefold() == str(right or "").replace("\\", "/").rstrip("/").casefold()


def _has_key(record: Any, key: str) -> bool:
    return _field(record, key) is not None


def _capability(record: Any) -> str:
    return str(_field(record, "capability") or _field(record, "target_capability") or "")


def _verdict(record: Any) -> str:
    status = record.get("status", "") if isinstance(record, dict) else getattr(record, "status", "")
    return str(_field(record, "verdict") or status or "").lower()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "pass", "passed", "success"}


def _has_outcome_signal(record: Any) -> bool:
    return any(
        _field(record, key) is not None
        for key in (
            "task_success",
            "outcome",
            "status",
            "result",
            "ok",
            "success",
            "verified",
            "verification",
            "verdict",
        )
    )


def _outcome_success(record: Any) -> bool:
    outcome = _field(record, "outcome")
    labels: list[str] = []
    bool_values: list[Any] = []
    for key in ("task_success", "success", "verified", "ok"):
        value = _field(record, key)
        if value is not None:
            bool_values.append(value)
    verifier = _field(record, "verifier")
    if isinstance(verifier, dict) and "passed" in verifier:
        bool_values.append(verifier.get("passed"))
    verification = _field(record, "verification")
    if isinstance(verification, dict):
        for key in ("passed", "success", "verified", "ok"):
            if key in verification:
                bool_values.append(verification.get(key))
        labels.extend(str(verification.get(key) or "").strip().lower() for key in ("status", "result", "verdict"))
    elif verification is not None:
        labels.append(str(verification or "").strip().lower())
    if isinstance(outcome, dict):
        labels.extend(str(outcome.get(key) or "").strip().lower() for key in ("status", "outcome", "result"))
        for key in ("success", "verified", "ok"):
            if key in outcome:
                bool_values.append(outcome.get(key))
    elif outcome is not None:
        labels.append(str(outcome or "").strip().lower())
    labels.extend(str(_field(record, key) or "").strip().lower() for key in ("status", "result", "verdict"))
    for label in labels:
        if _is_failure_label(label):
            return False
    if any(value is False or (not isinstance(value, bool) and str(value or "").strip().lower() in FAILURE_LABELS) for value in bool_values):
        return False
    if any(_truthy(value) for value in bool_values):
        return True
    for label in labels:
        if label in SUCCESS_LABELS:
            return True
    return _verdict(record) in SUCCESS_LABELS


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 3) if denominator else 0.0


def _is_failure_label(value: Any) -> bool:
    normalized = " ".join(
        str(value or "").strip().lower().replace("_", " ").replace("-", " ").replace(":", " ").split()
    )
    prefixes = {" ".join(label.replace("_", " ").split()) for label in FAILURE_LABELS}
    prefixes.update({"not run", "not executed", "skipped", "unavailable", "unknown", "missing"})
    return any(normalized == prefix or normalized.startswith(prefix + " ") for prefix in prefixes)


def _quality(sample_count: int, *, minimum: int = 10) -> dict[str, Any]:
    count = max(0, int(sample_count or 0))
    return {
        "sample_count": count,
        "minimum": minimum,
        "sufficient": count >= minimum,
    }


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
