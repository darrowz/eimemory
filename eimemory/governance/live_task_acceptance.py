from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from typing import Any, Callable

from eimemory.governance.deployment_receipt import (
    DEFAULT_DEPLOYMENT_CURRENT_LINK,
    DEFAULT_DEPLOYMENT_HEALTH_URL,
    DEFAULT_DEPLOYMENT_REPO_ROOT,
)
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef
from eimemory.runtime_identity import package_import_root, runtime_package_tree_digest
from eimemory.version import __version__


REPORT_TYPE = "live_task_acceptance"
CASE_REPORT_TYPE = "live_task_acceptance_case"
SCHEMA_VERSION = "live_task_acceptance.v1"
VERIFIER_METHOD = "eimemory.live_task_acceptance"
EVIDENCE_CLASS = "operational_probe"
REQUIRED_CASE_COUNT = 10
LIVE_ACCEPTANCE_CASE_IDS = (
    "store.sqlite_query",
    "store.scoped_record_read",
    "memory.store_search_read",
    "sources.registry_read",
    "governance.policy_ledger_read",
    "governance.skill_registry_read",
    "governance.dashboard_read",
    "governance.readiness_pure_read",
    "governance.replay_integrity",
    "deployment.identity",
)


def live_acceptance_task_type(case_id: str) -> str:
    return f"live.acceptance.{case_id}" if case_id in LIVE_ACCEPTANCE_CASE_IDS else ""


def validate_live_acceptance_case(
    runtime: Any,
    *,
    scope: ScopeRef,
    evidence: Any,
    case_id: str,
    task_type: str,
    trace_id: str,
    deployment_commit: str,
    passed: bool,
    identity: dict[str, Any] | None = None,
) -> bool:
    payload = _record_payload(evidence)
    digest = str(payload.get("observation_digest") or "")
    deployment_version = str(payload.get("deployment_version") or "")
    release_path = str(payload.get("release_path") or "")
    promotion_request_id = str(payload.get("promotion_request_id") or "")
    release_session_id = str(payload.get("release_session_id") or "")
    receipt = runtime.store.get_by_id(promotion_request_id, scope=scope) if promotion_request_id else None
    if identity is not None and (
        deployment_commit != str(identity.get("commit") or "")
        or deployment_version != str(identity.get("version") or "")
        or not _same_path(release_path, identity.get("release_path"))
        or promotion_request_id != str(identity.get("promotion_request_id") or "")
        or release_session_id
        != str(identity.get("release_session_id") or identity.get("promotion_request_id") or "")
    ):
        return False
    return bool(
        case_id in LIVE_ACCEPTANCE_CASE_IDS
        and task_type == live_acceptance_task_type(case_id)
        and evidence.kind == "learning_eval"
        and str(evidence.source or "") == "eimemory.live_task_acceptance"
        and str(payload.get("report_type") or "") == CASE_REPORT_TYPE
        and str(payload.get("schema_version") or "") == SCHEMA_VERSION
        and str(payload.get("evidence_class") or "") == EVIDENCE_CLASS
        and str(payload.get("case_id") or "") == case_id
        and str(payload.get("task_type") or "") == task_type
        and str(payload.get("deployment_commit") or "") == deployment_commit
        and bool(release_session_id)
        and payload.get("passed") is passed
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and str(payload.get("trace_id") or "") == trace_id
        and trace_id == f"live-acceptance:{deployment_commit}:{case_id}:{digest[:12]}"
        and _valid_deployment_receipt(
            receipt,
            commit=deployment_commit,
            version=deployment_version,
            release_path=release_path,
        )
    )


def _valid_deployment_receipt(receipt: Any, *, commit: str, version: str, release_path: str) -> bool:
    if receipt is None:
        return False
    content = receipt.content if isinstance(getattr(receipt, "content", None), dict) else {}
    meta = receipt.meta if isinstance(getattr(receipt, "meta", None), dict) else {}
    gate = content.get("gate") if isinstance(content.get("gate"), dict) else {}
    side_effect = content.get("side_effect") if isinstance(content.get("side_effect"), dict) else {}
    verification = side_effect.get("verification") if isinstance(side_effect.get("verification"), dict) else {}
    deployment = side_effect.get("deployment") if isinstance(side_effect.get("deployment"), dict) else {}
    health = side_effect.get("post_deploy_health") if isinstance(side_effect.get("post_deploy_health"), dict) else {}
    health_checks = health.get("checks") if isinstance(health.get("checks"), dict) else {}
    commit_payload = side_effect.get("commit") if isinstance(side_effect.get("commit"), dict) else {}
    release = side_effect.get("release") if isinstance(side_effect.get("release"), dict) else {}
    rollback = side_effect.get("rollback_evidence") if isinstance(side_effect.get("rollback_evidence"), dict) else {}
    expected_import_root = str(Path(release_path) / "eimemory")
    return bool(
        receipt.kind == "promotion_request"
        and str(receipt.source or "") == "eimemory.deployment_receipt"
        and str(content.get("report_type") or meta.get("report_type") or "") == "deployment_receipt"
        and str(receipt.status or "") == "deployed"
        and content.get("promotion_target") == "code_patch"
        and content.get("action") == "code_patch"
        and gate.get("ok") is True
        and gate.get("receipt_verified") is True
        and side_effect.get("ok") is True
        and side_effect.get("production_applied") is True
        and side_effect.get("deployment_executed") is True
        and verification.get("ok") is True
        and verification.get("skipped") is not True
        and deployment.get("ok") is True
        and deployment.get("skipped") is not True
        and health.get("ok") is True
        and health.get("skipped") is not True
        and health_checks.get("ready") is True
        and str(commit_payload.get("commit_sha") or "") == commit
        and str(release.get("version") or "") == version
        and _same_path(release.get("release_path"), release_path)
        and _same_path(deployment.get("release_path"), release_path)
        and str(health.get("commit") or "") == commit
        and str(health.get("version") or "") == version
        and _same_path(health.get("release_path"), release_path)
        and _same_path(health.get("import_root"), expected_import_root)
        and str(health.get("package_tree_digest") or "") == runtime_package_tree_digest()
        and str(rollback.get("prior_commit_sha") or "")
        and str(rollback.get("rollback_command") or "")
    )


def run_live_task_acceptance(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    repo_root: str = DEFAULT_DEPLOYMENT_REPO_ROOT,
    current_link: str = DEFAULT_DEPLOYMENT_CURRENT_LINK,
    health_url: str = DEFAULT_DEPLOYMENT_HEALTH_URL,
    prior_commit: str = "",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    identity = _verified_deployment_identity(
        runtime,
        scope=scope_ref,
        repo_root=repo_root,
        current_link=current_link,
        health_url=health_url,
        prior_commit=prior_commit,
    )
    if identity.get("ok") is not True:
        return {
            "ok": False,
            "report_type": REPORT_TYPE,
            "error": str(identity.get("error") or "deployment_identity_unverified"),
            "case_count": 0,
            "pass_count": 0,
            "cases": [],
        }

    definitions = _case_definitions(runtime, scope=scope_ref, identity=identity)
    case_ids = {str(item.get("case_id") or "") for item in definitions}
    task_types = {str(item.get("task_type") or "") for item in definitions}
    if (
        len(definitions) != REQUIRED_CASE_COUNT
        or case_ids != set(LIVE_ACCEPTANCE_CASE_IDS)
        or task_types != {live_acceptance_task_type(case_id) for case_id in LIVE_ACCEPTANCE_CASE_IDS}
    ):
        return {
            "ok": False,
            "report_type": REPORT_TYPE,
            "error": "invalid_live_acceptance_case_set",
            "case_count": 0,
            "pass_count": 0,
            "cases": [],
        }

    cases: list[dict[str, Any]] = []
    reused_count = 0
    for definition in definitions:
        existing = _existing_passed_case(
            runtime,
            scope=scope_ref,
            identity=identity,
            definition=definition,
        )
        if existing is not None:
            trace_result = _record_case_outcome(runtime, scope=scope_ref, case_record=existing)
            case = _case_response(existing, reused=True, trace_ok=trace_result.get("ok") is True)
            cases.append(case)
            reused_count += 1
            continue
        cases.append(
            _execute_and_record_case(
                runtime,
                scope=scope_ref,
                identity=identity,
                definition=definition,
            )
        )

    pass_count = sum(1 for case in cases if case.get("passed") is True)
    task_types = {str(case.get("task_type") or "") for case in cases if str(case.get("task_type") or "")}
    return {
        "ok": pass_count == REQUIRED_CASE_COUNT and len(task_types) == REQUIRED_CASE_COUNT,
        "report_type": REPORT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "scope": asdict(scope_ref),
        "deployment": {
            "commit": str(identity.get("commit") or ""),
            "version": str(identity.get("version") or ""),
            "release_path": str(identity.get("release_path") or ""),
            "promotion_request_id": str(identity.get("promotion_request_id") or ""),
        },
        "case_count": len(cases),
        "pass_count": pass_count,
        "fail_count": len(cases) - pass_count,
        "distinct_task_types": len(task_types),
        "reused_count": reused_count,
        "cases": cases,
    }


def _verified_deployment_identity(
    runtime: Any,
    *,
    scope: ScopeRef,
    repo_root: str,
    current_link: str,
    health_url: str,
    prior_commit: str,
) -> dict[str, Any]:
    identity = runtime.verify_and_record_deployment(
        scope=asdict(scope),
        repo_root=repo_root,
        current_link=current_link,
        health_url=health_url,
        prior_commit=prior_commit,
    )
    if identity.get("ok") is not True:
        return identity
    if not _runtime_matches_identity(identity):
        return {"ok": False, "error": "acceptance_runtime_not_current_immutable_release"}
    receipt_id = str(identity.get("promotion_request_id") or "")
    receipt = runtime.store.get_by_id(receipt_id, scope=scope) if receipt_id else None
    if not _valid_deployment_receipt(
        receipt,
        commit=str(identity.get("commit") or ""),
        version=str(identity.get("version") or ""),
        release_path=str(identity.get("release_path") or ""),
    ):
        return {"ok": False, "error": "acceptance_deployment_receipt_invalid"}
    return identity


def _runtime_matches_identity(identity: dict[str, Any]) -> bool:
    commit = str(identity.get("commit") or "").strip().lower()
    version = str(identity.get("version") or "").strip()
    configured_commit = str(os.environ.get("EIMEMORY_RUNTIME_COMMIT") or "").strip().lower()
    try:
        release_path = Path(str(identity.get("release_path") or "")).resolve(strict=True)
        import_root = package_import_root().resolve(strict=True)
    except OSError:
        return False
    return bool(
        re.fullmatch(r"[0-9a-f]{40}", commit)
        and release_path.name.lower() == commit
        and import_root.is_relative_to(release_path)
        and version == __version__
        and (not configured_commit or configured_commit == commit)
    )


def _case_definitions(runtime: Any, *, scope: ScopeRef, identity: dict[str, Any]) -> list[dict[str, Any]]:
    scope_payload = asdict(scope)

    def store_sqlite() -> dict[str, Any]:
        row = runtime.store.sqlite.conn.execute("SELECT 1").fetchone()
        return {"passed": bool(row and int(row[0]) == 1), "sqlite_ready": bool(row)}

    def scoped_records() -> dict[str, Any]:
        records = runtime.store.list_records(scope=scope, limit=10)
        return {"passed": isinstance(records, list), "sample_count": len(records)}

    def store_search_read() -> dict[str, Any]:
        items = runtime.store.search(
            query="eimemory production health and deployment evidence",
            scope=scope,
            limit=1,
        )
        return {"passed": isinstance(items, list), "sample_count": len(items)}

    def source_registry() -> dict[str, Any]:
        sources = runtime.sources.list_sources()
        return {"passed": isinstance(sources, list), "source_count": len(sources)}

    def policy_ledger() -> dict[str, Any]:
        records = runtime.get_policy_rollout_ledger(scope=scope_payload, limit=10)
        return {"passed": isinstance(records, list), "sample_count": len(records)}

    def skill_registry() -> dict[str, Any]:
        report = runtime.list_eiskills(scope=scope_payload, limit=10)
        return {"passed": report.get("ok") is True, "skill_count": int(report.get("skill_count") or 0)}

    def dashboard_read() -> dict[str, Any]:
        report = runtime.build_capability_dashboard_metrics(scope=scope_payload, persist=False, limit=500)
        return {"passed": report.get("ok") is True, "metric_count": len(report.get("metrics") or {})}

    def readiness_pure_read() -> dict[str, Any]:
        before = int(runtime.store.sqlite.conn.total_changes)
        report = runtime.build_l5_readiness_report(scope=scope_payload, persist=False, limit=500)
        after = int(runtime.store.sqlite.conn.total_changes)
        return {
            "passed": report.get("ok") is True and before == after,
            "connection_changes_before": before,
            "connection_changes_after": after,
            "stage": str(report.get("current_stage") or ""),
        }

    def replay_integrity() -> dict[str, Any]:
        report = runtime.build_l5_readiness_report(scope=scope_payload, persist=False, limit=500)
        replay = report.get("verified_replay") if isinstance(report.get("verified_replay"), dict) else {}
        passed = (
            int(replay.get("executed_count") or 0) >= 10
            and not replay.get("weak_capabilities_missing")
            and not replay.get("manifest_rejection_reasons")
        )
        return {
            "passed": passed,
            "executed_count": int(replay.get("executed_count") or 0),
            "weak_missing_count": len(replay.get("weak_capabilities_missing") or []),
            "manifest_rejection_count": len(replay.get("manifest_rejection_reasons") or {}),
        }

    def deployment_identity() -> dict[str, Any]:
        commit = str(identity.get("commit") or "")
        version = str(identity.get("version") or "")
        release_path = str(identity.get("release_path") or "")
        return {
            "passed": len(commit) == 40 and bool(version) and release_path.rstrip("/").endswith("/" + commit),
            "commit_present": len(commit) == 40,
            "version_present": bool(version),
            "release_bound": release_path.rstrip("/").endswith("/" + commit),
        }

    return [
        _definition("store.sqlite_query", store_sqlite),
        _definition("store.scoped_record_read", scoped_records),
        _definition("memory.store_search_read", store_search_read),
        _definition("sources.registry_read", source_registry),
        _definition("governance.policy_ledger_read", policy_ledger),
        _definition("governance.skill_registry_read", skill_registry),
        _definition("governance.dashboard_read", dashboard_read),
        _definition("governance.readiness_pure_read", readiness_pure_read),
        _definition("governance.replay_integrity", replay_integrity),
        _definition("deployment.identity", deployment_identity),
    ]


def _definition(case_id: str, check: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    return {"case_id": case_id, "task_type": live_acceptance_task_type(case_id), "check": check}


def _execute_and_record_case(
    runtime: Any,
    *,
    scope: ScopeRef,
    identity: dict[str, Any],
    definition: dict[str, Any],
) -> dict[str, Any]:
    try:
        observation = definition["check"]()
        observation = observation if isinstance(observation, dict) else {"passed": False, "invalid_result": True}
    except Exception as exc:
        observation = {"passed": False, "error_type": type(exc).__name__}
    passed = observation.get("passed") is True
    case_id = str(definition.get("case_id") or "")
    task_type = str(definition.get("task_type") or "")
    digest = _observation_digest(observation)
    commit = str(identity.get("commit") or "")
    trace_id = f"live-acceptance:{commit}:{case_id}:{digest[:12]}"
    payload = {
        "report_type": CASE_REPORT_TYPE,
        "schema_version": SCHEMA_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "case_id": case_id,
        "task_type": task_type,
        "trace_id": trace_id,
        "passed": passed,
        "observation_digest": digest,
        "deployment_commit": commit,
        "deployment_version": str(identity.get("version") or ""),
        "release_path": str(identity.get("release_path") or ""),
        "promotion_request_id": str(identity.get("promotion_request_id") or ""),
        "release_session_id": str(
            identity.get("release_session_id")
            or identity.get("promotion_request_id")
            or ""
        ),
    }
    record = append_learning_record_once(
        runtime,
        kind="learning_eval",
        title=f"Live task acceptance {case_id}",
        summary=f"{case_id}: {'passed' if passed else 'failed'}",
        scope=scope,
        loop_id=f"live_acceptance_{commit[:12]}",
        step_name=case_id,
        semantic_key=stable_semantic_key("live_task_acceptance", commit, case_id, passed, digest),
        status="active",
        content=payload,
        meta=payload,
        evidence=[str(identity.get("promotion_request_id") or "")],
        source="eimemory.live_task_acceptance",
    )
    trace_result = _record_case_outcome(runtime, scope=scope, case_record=record)
    return _case_response(record, reused=False, trace_ok=trace_result.get("ok") is True)


def _record_case_outcome(runtime: Any, *, scope: ScopeRef, case_record: Any) -> dict[str, Any]:
    payload = _record_payload(case_record)
    passed = payload.get("passed") is True
    return runtime.record_outcome_trace(
        {
            "source": "eimemory.live_task_acceptance",
            "trace_id": str(payload.get("trace_id") or ""),
            "idempotency_key": f"live-acceptance-outcome:{case_record.record_id}",
            "task_type": str(payload.get("task_type") or ""),
            "input_summary": f"Production live acceptance {payload.get('case_id')}",
            "outcome": {"status": "success" if passed else "failed", "success": passed, "rehearsal": False},
            "verifier": {
                "passed": passed,
                "method": VERIFIER_METHOD,
                "evidence_refs": [case_record.record_id],
            },
            "deployment_commit": str(payload.get("deployment_commit") or ""),
            "deployment_version": str(payload.get("deployment_version") or ""),
            "release_path": str(payload.get("release_path") or ""),
            "promotion_request_id": str(payload.get("promotion_request_id") or ""),
            "release_session_id": str(payload.get("release_session_id") or ""),
            "evidence_class": EVIDENCE_CLASS,
            "acceptance_case_id": str(payload.get("case_id") or ""),
        },
        scope=asdict(scope),
    )


def _existing_passed_case(
    runtime: Any,
    *,
    scope: ScopeRef,
    identity: dict[str, Any],
    definition: dict[str, Any],
) -> Any | None:
    deployment_commit = str(identity.get("commit") or "")
    case_id = str(definition.get("case_id") or "")
    task_type = str(definition.get("task_type") or "")
    offset = 0
    while True:
        records = runtime.store.list_records(kinds=["learning_eval"], scope=scope, limit=500, offset=offset)
        for record in records:
            payload = _record_payload(record)
            if (
                validate_live_acceptance_case(
                    runtime,
                    scope=scope,
                    evidence=record,
                    case_id=case_id,
                    task_type=task_type,
                    trace_id=str(payload.get("trace_id") or ""),
                    deployment_commit=deployment_commit,
                    passed=True,
                    identity=identity,
                )
            ):
                return record
        if len(records) < 500:
            return None
        offset += len(records)


def _case_response(record: Any, *, reused: bool, trace_ok: bool) -> dict[str, Any]:
    payload = _record_payload(record)
    return {
        "case_id": str(payload.get("case_id") or ""),
        "task_type": str(payload.get("task_type") or ""),
        "passed": payload.get("passed") is True and trace_ok,
        "trace_persisted": trace_ok,
        "record_id": str(record.record_id or ""),
        "trace_id": str(payload.get("trace_id") or ""),
        "observation_digest": str(payload.get("observation_digest") or ""),
        "reused": reused,
    }


def _record_payload(record: Any) -> dict[str, Any]:
    content = record.content if isinstance(getattr(record, "content", None), dict) else {}
    meta = record.meta if isinstance(getattr(record, "meta", None), dict) else {}
    return {**meta, **content}


def _observation_digest(observation: dict[str, Any]) -> str:
    encoded = json.dumps(observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _same_path(left: Any, right: Any) -> bool:
    return str(left or "").replace("\\", "/").rstrip("/").casefold() == str(right or "").replace("\\", "/").rstrip("/").casefold()
