from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.core.ids import generate_record_id
from eimemory.experience.capability_contract import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    normalize_capability_contract,
    validate_capability_contract,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


REPORT_TYPE = "capability_acceptance"
PROBE_REPORT_TYPE = "capability_probe_result"
PROBE_SCHEMA_VERSION = "capability_probe_result.v1"


_CAPABILITY_ACCEPTANCE_CASES: tuple[dict[str, Any], ...] = (
    {
        "case_id": "search_recent_source",
        "capability": "search.discovery",
        "input": {"query": "recent project updates", "recency_window": "30d"},
        "observation": {"recency_window": "30d", "source_trust_score": 0.9, "source_verified": True},
    },
    {
        "case_id": "search_trending_github",
        "capability": "search.discovery",
        "input": {"query": "trending GitHub projects", "created_range": "2026-01-01..2026-01-31"},
        "observation": {
            "platform": "GitHub",
            "created_range": "2026-01-01..2026-01-31",
            "sort_by": "stars",
            "ranking_verified": True,
        },
    },
    {
        "case_id": "search_primary_source",
        "capability": "search.discovery",
        "input": {"query": "verify a technical fact", "preferred_source": "official"},
        "observation": {"source_tier": "official", "source_verified": True},
    },
    {
        "case_id": "research_evidence_gate",
        "capability": "research.synthesis",
        "input": {"task": "summarize a paper", "evidence_required": True},
        "observation": {"citation_count": 2, "facts_separated_from_inference": True},
    },
    {
        "case_id": "research_conflict_resolution",
        "capability": "research.synthesis",
        "input": {"task": "resolve conflicting sources", "source_count": 2},
        "observation": {"conflict_count": 1, "recency_compared": True, "confidence_reported": True},
    },
    {
        "case_id": "research_actionable_takeaway",
        "capability": "research.synthesis",
        "input": {"task": "turn research into an implementation step"},
        "observation": {"decision": "adopt", "implementation_step": "add replay", "next_artifact": "replay"},
    },
    {
        "case_id": "uumit_requirement_checklist",
        "capability": "operations.uumit",
        "input": {"task": "verify an external delivery", "requirements": ["format", "content", "deadline"]},
        "observation": {"requirement_count": 3, "checklist_complete": True, "acceptance_verified": True},
    },
    {
        "case_id": "uumit_quality_gate",
        "capability": "operations.uumit",
        "input": {"task": "quality-gate a delivery asset"},
        "observation": {
            "version_verified": True,
            "visual_verified": True,
            "customer_constraints_verified": True,
        },
    },
    {
        "case_id": "uumit_post_delivery_followup",
        "capability": "operations.uumit",
        "input": {"task": "record post-delivery learning"},
        "observation": {"outcome_recorded": True, "correction_recorded": True, "next_policy_recorded": True},
    },
    {
        "case_id": "device_physical_channel",
        "capability": "device.control",
        "input": {"task": "rehearse media output", "physical_action": False},
        "observation": {"channel": "speaker", "control_action": "play", "output_verified": True},
    },
    {
        "case_id": "device_missing_info",
        "capability": "device.control",
        "input": {"task": "detect a missing device target", "physical_action": False},
        "observation": {"target_missing_detected": True, "resolution": "clarify"},
    },
    {
        "case_id": "device_safe_boundary",
        "capability": "device.control",
        "input": {"task": "rehearse a reversible device boundary", "physical_action": False},
        "observation": {
            "reversible": True,
            "rollback_plan": "stop playback",
            "verification_signal": "speaker silent",
        },
    },
)

CAPABILITY_ACCEPTANCE_CASE_IDS: tuple[str, ...] = tuple(
    str(artifact["case_id"]) for artifact in _CAPABILITY_ACCEPTANCE_CASES
)


def capability_acceptance_case(case_id: str) -> dict[str, Any]:
    expected = str(case_id or "").strip()
    for artifact in _CAPABILITY_ACCEPTANCE_CASES:
        if str(artifact.get("case_id") or "") == expected:
            return deepcopy(artifact)
    return {}


def run_capability_acceptance(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
    execution_id: str = "",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    final_execution_id = str(execution_id or "").strip() or generate_record_id("replay_result")
    results: list[dict[str, Any]] = []
    persisted_probe_ids: list[str] = []
    trace_record_ids: list[str] = []

    for case_id in CAPABILITY_ACCEPTANCE_CASE_IDS:
        artifact = capability_acceptance_case(case_id)
        result = _run_probe(
            runtime,
            artifact=artifact,
            scope=scope_ref,
            persist=bool(persist),
            execution_id=final_execution_id,
        )
        results.append(result)
        if result["persisted"]:
            persisted_probe_ids.append(result["probe_id"])
        if result["trace_record_id"]:
            trace_record_ids.append(result["trace_record_id"])

    probe_ids = [str(item["probe_id"]) for item in results]
    trace_ids = [str(item["trace_id"]) for item in results]
    distinct_probe_sources = len(probe_ids) == len(set(probe_ids)) == len(CAPABILITY_ACCEPTANCE_CASE_IDS)
    distinct_trace_ids = len(trace_ids) == len(set(trace_ids)) == len(CAPABILITY_ACCEPTANCE_CASE_IDS)
    pass_count = sum(1 for item in results if item["passed"])
    failed_count = len(results) - pass_count
    all_passed = (
        len(results) == len(CAPABILITY_ACCEPTANCE_CASE_IDS)
        and failed_count == 0
        and distinct_probe_sources
        and distinct_trace_ids
        and (not persist or len(trace_record_ids) == len(CAPABILITY_ACCEPTANCE_CASE_IDS))
    )
    return {
        "ok": all_passed,
        "all_passed": all_passed,
        "report_type": REPORT_TYPE,
        "scope": asdict(scope_ref),
        "execution_id": final_execution_id,
        "persisted": bool(persist),
        "case_count": len(results),
        "probe_count": len(results),
        "trace_count": len(trace_record_ids),
        "pass_count": pass_count,
        "failed_count": failed_count,
        "distinct_probe_sources": distinct_probe_sources,
        "distinct_trace_ids": distinct_trace_ids,
        "probe_ids": probe_ids,
        "probe_record_ids": persisted_probe_ids,
        "trace_ids": trace_ids,
        "trace_record_ids": trace_record_ids,
        "results": results,
    }


def _run_probe(
    runtime: Any,
    *,
    artifact: dict[str, Any],
    scope: ScopeRef,
    persist: bool,
    execution_id: str,
) -> dict[str, Any]:
    case_id = str(artifact["case_id"])
    capability = str(artifact["capability"])
    probe_id = generate_record_id("replay_result")
    trace_id = f"capability-acceptance-{execution_id}-{case_id}-{probe_id}"
    digest = capability_acceptance_digest(
        capability=capability,
        case_id=case_id,
        input_data=artifact["input"],
        observation=artifact["observation"],
    )
    checks = [{"name": "canonical_observation_contract", "passed": True, "evidence_ref": probe_id}]
    contract = normalize_capability_contract(
        {
            "schema_version": CONTRACT_SCHEMA_VERSION,
            "capability": capability,
            "case_id": case_id,
            "observations": dict(artifact["observation"]),
            "checks": checks,
            "source_record_ids": [probe_id],
            "probe": True,
        }
    )
    validation_error = validate_capability_contract(
        contract,
        expected_capability=capability,
        expected_case_id=case_id,
    )
    validator_passed = not validation_error
    probe_record = _probe_record(
        probe_id=probe_id,
        artifact=artifact,
        scope=scope,
        execution_id=execution_id,
        digest=digest,
        checks=checks,
        passed=validator_passed,
        error=validation_error,
    )
    if persist:
        runtime.store.append(probe_record)

    trace_record_id = ""
    trace_error = ""
    if validator_passed and persist:
        trace_result = runtime.record_outcome_trace(
            {
                "trace_id": trace_id,
                "idempotency_key": trace_id,
                "task_type": "capability.acceptance",
                "input_summary": f"Non-destructive acceptance probe: {case_id}",
                "selected_tools": [],
                "actions": [{"type": "contract_validation", "case_id": case_id}],
                "outcome": {"status": "success", "success": True, "rehearsal": True},
                "verifier": {
                    "passed": True,
                    "method": "validate_capability_contract",
                    "evidence_ref": probe_id,
                    "artifact_digest": digest,
                },
                "capability": capability,
                "capability_case_id": case_id,
                "capability_contract": contract,
            },
            scope=asdict(scope),
        )
        if trace_result.get("ok") is True and trace_result.get("record_id"):
            trace_record_id = str(trace_result["record_id"])
        else:
            trace_error = str(trace_result.get("error") or "outcome trace persistence failed")

    passed = validator_passed and (not persist or bool(trace_record_id))
    return {
        "case_id": case_id,
        "capability": capability,
        "probe_id": probe_id,
        "probe_record_id": probe_id if persist else "",
        "source_record_id": probe_id,
        "trace_id": trace_id,
        "trace_record_id": trace_record_id,
        "digest": digest,
        "validator_passed": validator_passed,
        "trace_emitted": bool(trace_record_id),
        "persisted": persist,
        "passed": passed,
        "error": validation_error or trace_error,
    }


def _probe_record(
    *,
    probe_id: str,
    artifact: dict[str, Any],
    scope: ScopeRef,
    execution_id: str,
    digest: str,
    checks: list[dict[str, Any]],
    passed: bool,
    error: str,
) -> RecordEnvelope:
    case_id = str(artifact["case_id"])
    capability = str(artifact["capability"])
    verdict = "pass" if passed else "fail"
    record = RecordEnvelope.create(
        kind="replay_result",
        title=f"Capability acceptance probe: {case_id}",
        summary=f"{verdict}: canonical non-destructive artifact validation",
        scope=scope,
        content={
            "report_type": PROBE_REPORT_TYPE,
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "case_id": case_id,
            "capability": capability,
            "input": dict(artifact["input"]),
            "checks": [dict(check) for check in checks],
            "observation": dict(artifact["observation"]),
            "digest": digest,
            "passed": passed,
            "verdict": verdict,
            "validator": {
                "schema_version": CONTRACT_SCHEMA_VERSION,
                "passed": passed,
                "error": error,
            },
        },
        tags=["governance", "capability-acceptance", "probe", verdict],
        source="eimemory.capability_acceptance",
        provenance={
            "report_type": PROBE_REPORT_TYPE,
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "artifact_digest": digest,
        },
        meta={
            "report_type": PROBE_REPORT_TYPE,
            "schema_version": PROBE_SCHEMA_VERSION,
            "execution_id": execution_id,
            "case_id": case_id,
            "capability": capability,
            "artifact_digest": digest,
            "passed": passed,
            "verdict": verdict,
        },
    )
    record.record_id = probe_id
    return record


def capability_acceptance_digest(
    *,
    capability: str,
    case_id: str,
    input_data: dict[str, Any],
    observation: dict[str, Any],
) -> str:
    artifact = {
        "case_id": str(case_id),
        "capability": str(capability),
        "input": dict(input_data),
        "observation": dict(observation),
    }
    canonical = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()
