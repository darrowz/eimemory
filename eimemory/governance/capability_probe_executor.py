from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
from typing import Any, Callable


EXECUTOR_VERSION = "capability_probe_executor.v1"
ProbeExecutor = Callable[[dict[str, Any], dict[str, Any], Any], dict[str, Any]]


def _search_recent(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    window_days = int(str(input_data["recency_window"]).removesuffix("d"))
    selected = [item for item in fixture["sources"] if int(item["age_days"]) <= window_days]
    selected.sort(key=lambda item: (-float(item["trust"]), int(item["age_days"]), str(item["id"])))
    return {
        "selected_sources": [item["id"] for item in selected],
        "recency_window": input_data["recency_window"],
        "source_trust_score": float(selected[0]["trust"]) if selected else 0.0,
        "source_verified": bool(selected) and all(item.get("verified") is True for item in selected),
    }


def _search_trending(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    start, end = str(input_data["created_range"]).split("..", 1)
    ranked = [repo for repo in fixture["repositories"] if start <= str(repo["created_at"]) <= end]
    ranked.sort(key=lambda repo: (-int(repo["stars"]), str(repo["name"])))
    return {
        "platform": "GitHub",
        "created_range": input_data["created_range"],
        "sort_by": "stars",
        "ranked_repositories": [repo["name"] for repo in ranked],
        "ranking_verified": [repo["stars"] for repo in ranked] == sorted((repo["stars"] for repo in ranked), reverse=True),
    }


def _search_primary(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    preferred = str(input_data["preferred_source"])
    tiers = {"official": 0, "paper": 1, "vendor": 2, "community": 3}
    sources = sorted(fixture["sources"], key=lambda item: (tiers.get(str(item["tier"]), 99), str(item["id"])))
    selected = next((item for item in sources if item["tier"] == preferred), sources[0] if sources else {})
    return {
        "selected_source": selected.get("id", ""),
        "source_tier": selected.get("tier", ""),
        "source_verified": selected.get("verified") is True,
    }


def _research_evidence(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    statements = list(fixture["statements"])
    citations = sorted({str(item["citation"]) for item in statements if item.get("citation")})
    kinds = {str(item.get("kind") or "") for item in statements}
    return {
        "citations": citations,
        "citation_count": len(citations),
        "facts_separated_from_inference": {"fact", "inference"}.issubset(kinds),
    }


def _research_conflict(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    sources = sorted(fixture["sources"], key=lambda item: str(item["published_at"]), reverse=True)
    claims = {str(item["claim"]) for item in sources}
    return {
        "conflict_count": max(0, len(claims) - 1),
        "recency_compared": len({item["published_at"] for item in sources}) == len(sources),
        "confidence_reported": all(isinstance(item.get("confidence"), (int, float)) for item in sources),
        "preferred_claim": sources[0]["claim"] if sources else "",
    }


def _research_actionable(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    finding = max(fixture["findings"], key=lambda item: (float(item["confidence"]), str(item["finding"])))
    return {
        "finding": finding["finding"],
        "decision": finding["decision"],
        "implementation_step": finding["implementation_step"],
        "next_artifact": finding["next_artifact"],
    }


def _uumit_requirements(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    delivered = dict(fixture["delivered"])
    checklist = [{"requirement": item, "passed": delivered.get(item) is True} for item in input_data["requirements"]]
    return {
        "checklist": checklist,
        "requirement_count": len(checklist),
        "checklist_complete": bool(checklist) and all(item["passed"] for item in checklist),
        "acceptance_verified": bool(fixture.get("acceptance_signature")),
    }


def _uumit_quality(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    expected = dict(fixture["expected"])
    observed = dict(fixture["observed"])
    return {
        "version_verified": observed.get("version") == expected.get("version"),
        "visual_verified": observed.get("visual_hash") == expected.get("visual_hash"),
        "customer_constraints_verified": observed.get("constraints") == expected.get("constraints"),
    }


def _uumit_post_delivery(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef

    with TemporaryDirectory(prefix="eimemory-probe-") as root:
        sandbox = Runtime.create(root=root)
        try:
            scope = ScopeRef.from_dict({"agent_id": "probe", "workspace_id": "delivery", "user_id": "sandbox"})
            for report_type in ("delivery_outcome", "delivery_correction", "delivery_next_policy"):
                sandbox.store.append(
                    RecordEnvelope.create(
                        kind="reflection",
                        title=report_type,
                        summary=str(fixture[report_type]),
                        scope=scope,
                        content={"report_type": report_type, "value": fixture[report_type]},
                    )
                )
            rows = sandbox.store.list_records(kinds=["reflection"], scope=scope, limit=10)
            report_types = {str(row.content.get("report_type") or "") for row in rows}
        finally:
            sandbox.close()
    return {
        "transaction_record_count": len(report_types),
        "outcome_recorded": "delivery_outcome" in report_types,
        "correction_recorded": "delivery_correction" in report_types,
        "next_policy_recorded": "delivery_next_policy" in report_types,
    }


def _device_route(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    route = dict(fixture["routes"]).get(str(input_data["media_type"]), {})
    return {
        "channel": route.get("channel", ""),
        "control_action": route.get("action", ""),
        "output_verified": bool(route) and input_data.get("physical_action") is False,
        "physical_side_effect": False,
    }


def _device_missing(input_data: dict[str, Any], _fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    missing = not str(input_data.get("target") or "").strip()
    return {
        "target_missing_detected": missing,
        "resolution": "clarify" if missing else "route",
        "clarification": "Which device target should receive the action?" if missing else "",
    }


def _device_safety(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    action = str(input_data["requested_action"])
    rollback = dict(fixture["rollback_by_action"]).get(action, "")
    return {
        "reversible": bool(rollback),
        "rollback_plan": rollback,
        "verification_signal": fixture["verification_signal"],
        "physical_side_effect": False,
    }


PROBE_EXECUTORS: dict[str, ProbeExecutor] = {
    "search_recent_source": _search_recent,
    "search_trending_github": _search_trending,
    "search_primary_source": _search_primary,
    "research_evidence_gate": _research_evidence,
    "research_conflict_resolution": _research_conflict,
    "research_actionable_takeaway": _research_actionable,
    "uumit_requirement_checklist": _uumit_requirements,
    "uumit_quality_gate": _uumit_quality,
    "uumit_post_delivery_followup": _uumit_post_delivery,
    "device_physical_channel": _device_route,
    "device_missing_info": _device_missing,
    "device_safe_boundary": _device_safety,
}


def execute_probe(artifact: dict[str, Any], *, runtime: Any, evidence_ref: str) -> dict[str, Any]:
    case_id = str(artifact.get("case_id") or "")
    input_data = deepcopy(artifact.get("input") or {})
    fixture = deepcopy(artifact.get("fixture") or {})
    executor = PROBE_EXECUTORS.get(case_id)
    executor_id = f"eimemory.capability_probe.{case_id}"
    error = ""
    if executor is None:
        output: dict[str, Any] = {}
        checks = [{"name": "executor_available", "passed": False, "evidence_ref": evidence_ref}]
        error = f"executor unavailable: {case_id}"
    else:
        try:
            raw_output = executor(deepcopy(input_data), deepcopy(fixture), runtime)
            output = deepcopy(raw_output) if isinstance(raw_output, dict) else {}
            checks = _evaluate_invariants(output, artifact.get("expected_invariants"), evidence_ref=evidence_ref)
            if not isinstance(raw_output, dict):
                error = "executor output must be an object"
        except Exception as exc:
            output = {}
            checks = [{"name": "executor_completed", "passed": False, "evidence_ref": evidence_ref}]
            error = f"executor exception: {type(exc).__name__}"
    observation = _observation_from_output(output, artifact.get("expected_invariants"))
    execution_digest = execution_evidence_digest(
        executor_id=executor_id,
        executor_version=EXECUTOR_VERSION,
        input_data=input_data,
        output=output,
        observation=observation,
        checks=checks,
    )
    passed = bool(checks) and all(check.get("passed") is True for check in checks) and not error
    return {
        "executor_id": executor_id,
        "executor_version": EXECUTOR_VERSION,
        "input": input_data,
        "output": output,
        "observation": observation,
        "checks": checks,
        "execution_digest": execution_digest,
        "passed": passed,
        "error": error or ("" if passed else "executor invariant check failed"),
    }


def validate_execution_evidence(
    artifact: dict[str, Any], *, runtime: Any, evidence_ref: str, evidence: dict[str, Any]
) -> str:
    expected = execute_probe(artifact, runtime=runtime, evidence_ref=evidence_ref)
    if expected.get("passed") is not True:
        return "canonical_probe_executor_failed"
    fields = ("executor_id", "executor_version", "input", "output", "observation", "checks", "execution_digest")
    if any(evidence.get(field) != expected.get(field) for field in fields):
        return "probe_execution_evidence_mismatch"
    if evidence.get("passed") is not True:
        return "probe_execution_not_passed"
    return ""


def execution_evidence_digest(
    *,
    executor_id: str,
    executor_version: str,
    input_data: dict[str, Any],
    output: dict[str, Any],
    observation: dict[str, Any],
    checks: list[dict[str, Any]],
) -> str:
    payload = {
        "executor_id": str(executor_id),
        "executor_version": str(executor_version),
        "input": deepcopy(input_data),
        "output": deepcopy(output),
        "observation": deepcopy(observation),
        "checks": deepcopy(checks),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _evaluate_invariants(output: dict[str, Any], raw_invariants: Any, *, evidence_ref: str) -> list[dict[str, Any]]:
    invariants = list(raw_invariants or [])
    checks: list[dict[str, Any]] = []
    for invariant in invariants:
        if not isinstance(invariant, dict):
            checks.append({"name": "invalid_invariant", "passed": False, "evidence_ref": evidence_ref})
            continue
        field = str(invariant.get("field") or "")
        operation = str(invariant.get("op") or "eq")
        observed = output.get(field)
        expected = invariant.get("value")
        if operation == "eq":
            passed = observed == expected
        elif operation == "min":
            passed = isinstance(observed, (int, float)) and not isinstance(observed, bool) and observed >= expected
        elif operation == "nonempty":
            passed = bool(observed)
        else:
            passed = False
        checks.append({
            "name": f"{field}_{operation}",
            "field": field,
            "operation": operation,
            "expected": deepcopy(expected),
            "observed": deepcopy(observed),
            "passed": passed,
            "evidence_ref": evidence_ref,
        })
    return checks


def _observation_from_output(output: dict[str, Any], raw_invariants: Any) -> dict[str, Any]:
    fields = [str(item.get("field") or "") for item in list(raw_invariants or []) if isinstance(item, dict)]
    return {field: deepcopy(output.get(field)) for field in fields if field}
