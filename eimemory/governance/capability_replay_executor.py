from __future__ import annotations

from copy import deepcopy
from typing import Any

from eimemory.experience.capability_contract import (
    SCHEMA_VERSION as CONTRACT_SCHEMA_VERSION,
    contract_source_ids,
    normalize_capability_contract,
    validate_capability_contract,
)
from eimemory.governance.capability_acceptance import PROBE_REPORT_TYPE, PROBE_SCHEMA_VERSION
from eimemory.governance.capability_attribution import collect_capability_evidence
from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef


SUCCESS_STATUSES = {"success", "good", "passed", "pass", "completed"}


def execute_capability_replay_case(runtime: Any, case: dict[str, Any]) -> dict[str, Any]:
    """Replay one case only from a verified outcome-trace and probe contract chain."""

    scope = ScopeRef.from_dict(case.get("scope") or {})
    capability = str(case.get("target_capability") or "").strip()
    case_id = str(case.get("case_id") or "").strip()
    evidence_by_capability = collect_capability_evidence(runtime, scope=scope, limit=500)
    candidates = sorted(
        (
            item
            for item in evidence_by_capability.get(capability, [])
            if item.get("contract_verified") is True
            and str(item.get("case_id") or "") == case_id
            and str(item.get("source_kind") or "") == "outcome_trace"
            and str(item.get("source_id") or "")
        ),
        key=lambda item: str(item.get("source_id") or ""),
    )
    if not candidates:
        return _failure("not_run", "contract_backed_outcome_evidence_missing")

    probe_uses: dict[str, int] = {}
    for item in candidates:
        for source_id in item.get("source_record_ids") or []:
            probe_id = str(source_id or "").strip()
            if probe_id:
                probe_uses[probe_id] = probe_uses.get(probe_id, 0) + 1
    reused_probe_ids = sorted(probe_id for probe_id, count in probe_uses.items() if count > 1)
    if reused_probe_ids:
        return _failure(
            "fail",
            "reused_probe_source",
            observed=f"probe_source_id={reused_probe_ids[0]}",
            probe_source_id=reused_probe_ids[0],
        )

    failures: list[dict[str, Any]] = []
    for evidence in candidates:
        result = _validate_contract_chain(
            runtime,
            evidence=evidence,
            scope=scope,
            capability=capability,
            case_id=case_id,
        )
        if result.get("verdict") == "pass":
            return result
        failures.append(result)
    return failures[0] if failures else _failure("not_run", "contract_backed_outcome_evidence_missing")


def _validate_contract_chain(
    runtime: Any,
    *,
    evidence: dict[str, Any],
    scope: ScopeRef,
    capability: str,
    case_id: str,
) -> dict[str, Any]:
    trace_record_id = str(evidence.get("source_id") or "").strip()
    trace_record = runtime.store.get_by_id(trace_record_id, scope=scope)
    if trace_record is None:
        return _failure("not_run", "outcome_trace_not_retrievable", trace_record_id=trace_record_id)
    if trace_record.kind != "reflection" or trace_record.source != "eimemory.experience.outcome_trace":
        return _failure("fail", "untrusted_outcome_trace_source", trace_record_id=trace_record_id)

    trace_meta = business_metadata(trace_record.meta)
    if str(trace_meta.get("report_type") or "") != "outcome_trace":
        return _failure("fail", "invalid_outcome_trace_report_type", trace_record_id=trace_record_id)
    content = trace_record.content if isinstance(trace_record.content, dict) else {}
    payload = content.get("payload") if isinstance(content.get("payload"), dict) else {}
    trace_id = str(payload.get("trace_id") or "").strip()
    if not trace_id or str(trace_meta.get("trace_id") or "").strip() != trace_id:
        return _failure("fail", "outcome_trace_id_mismatch", trace_record_id=trace_record_id)

    contract = normalize_capability_contract(payload.get("capability_contract"))
    contract_error = validate_capability_contract(
        contract,
        expected_capability=capability,
        expected_case_id=case_id,
    )
    if contract_error:
        return _failure("fail", "invalid_capability_contract", trace_id=trace_id, trace_record_id=trace_record_id)
    if (
        trace_meta.get("contract_verified") is not True
        or str(trace_meta.get("capability") or "") != capability
        or str(trace_meta.get("capability_case_id") or "") != case_id
        or str(payload.get("capability") or "") != capability
        or str(payload.get("capability_case_id") or "") != case_id
    ):
        return _failure("fail", "trace_contract_attribution_mismatch", trace_id=trace_id, trace_record_id=trace_record_id)

    source_ids = contract_source_ids(contract)
    if len(source_ids) != 1:
        return _failure("fail", "single_probe_source_required", trace_id=trace_id, trace_record_id=trace_record_id)
    probe_source_id = source_ids[0]
    if list(evidence.get("source_record_ids") or []) != source_ids:
        return _failure(
            "fail",
            "attribution_source_mismatch",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )

    verifier = payload.get("verifier") if isinstance(payload.get("verifier"), dict) else {}
    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
    outcome_status = str(outcome.get("status") or trace_meta.get("outcome_status") or "").strip().lower()
    if outcome_status not in SUCCESS_STATUSES or outcome.get("rehearsal") is not True:
        return _failure(
            "fail",
            "verified_rehearsal_outcome_required",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )
    if verifier.get("passed") is not True or str(verifier.get("evidence_ref") or "") != probe_source_id:
        return _failure(
            "fail",
            "outcome_verifier_probe_mismatch",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )
    if any(str(check.get("evidence_ref") or "") != probe_source_id for check in contract.get("checks") or []):
        return _failure(
            "fail",
            "contract_check_probe_mismatch",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )

    probe_record = runtime.store.get_by_id(probe_source_id, scope=scope)
    if probe_record is None:
        return _failure(
            "not_run",
            "probe_source_unavailable_in_scope",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )
    probe_meta = business_metadata(probe_record.meta)
    probe_content = probe_record.content if isinstance(probe_record.content, dict) else {}
    validator = probe_content.get("validator") if isinstance(probe_content.get("validator"), dict) else {}
    if (
        probe_record.kind != "replay_result"
        or probe_record.source != "eimemory.capability_acceptance"
        or str(probe_meta.get("report_type") or "") != PROBE_REPORT_TYPE
        or str(probe_meta.get("schema_version") or "") != PROBE_SCHEMA_VERSION
        or str(probe_meta.get("capability") or "") != capability
        or str(probe_meta.get("case_id") or "") != case_id
        or probe_meta.get("passed") is not True
        or str(probe_meta.get("verdict") or "") != "pass"
        or str(probe_content.get("report_type") or "") != PROBE_REPORT_TYPE
        or str(probe_content.get("schema_version") or "") != PROBE_SCHEMA_VERSION
        or str(probe_content.get("capability") or "") != capability
        or str(probe_content.get("case_id") or "") != case_id
        or probe_content.get("passed") is not True
        or str(probe_content.get("verdict") or "") != "pass"
        or validator.get("passed") is not True
        or str(validator.get("schema_version") or "") != CONTRACT_SCHEMA_VERSION
    ):
        return _failure(
            "fail",
            "invalid_capability_probe",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )
    probe_checks = probe_content.get("checks") if isinstance(probe_content.get("checks"), list) else []
    if not probe_checks or any(
        not isinstance(check, dict)
        or check.get("passed") is not True
        or str(check.get("evidence_ref") or "") != probe_source_id
        for check in probe_checks
    ):
        return _failure(
            "fail",
            "invalid_probe_checks",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )

    observation = probe_content.get("observation")
    if not isinstance(observation, dict) or observation != contract.get("observations"):
        return _failure(
            "fail",
            "probe_observation_contract_mismatch",
            trace_id=trace_id,
            trace_record_id=trace_record_id,
            probe_source_id=probe_source_id,
        )
    immutable_observation = deepcopy(observation)
    return {
        "verdict": "pass",
        "hit": True,
        "evidence_source_id": trace_record_id,
        "trace_id": trace_id,
        "trace_record_id": trace_record_id,
        "probe_source_id": probe_source_id,
        "contract_schema": str(contract.get("schema_version") or ""),
        "observation": immutable_observation,
        "observed": (
            f"trace_id={trace_id};trace_record_id={trace_record_id};"
            f"probe_source_id={probe_source_id};contract_schema={contract.get('schema_version', '')}"
        ),
    }


def _failure(
    verdict: str,
    reason: str,
    *,
    observed: str = "",
    trace_id: str = "",
    trace_record_id: str = "",
    probe_source_id: str = "",
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "hit": False if verdict == "fail" else None,
        "observed": observed,
        "reason": reason,
        "trace_id": trace_id,
        "trace_record_id": trace_record_id,
        "probe_source_id": probe_source_id,
        "contract_schema": "",
        "observation": {},
    }
