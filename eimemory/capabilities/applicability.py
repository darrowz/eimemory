"""Deterministic applicability inputs for capability-state projection.

This module is deliberately a *qualifier*, not a scoring engine.  It turns
explicit binding declarations, observation environments, knowledge-link state,
and a profile's freshness rule into a small immutable-friendly DTO.  A host
name, package version, model name, or machine identifier is never interpreted
as a capability identity or as an implicit readiness signal.

The projector owns maturity.  Keeping applicability here prevents every
consumer from inventing its own treatment for stale evidence, an unavailable
binding, or a contradicted knowledge source.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from eimemory.core.clock import now_iso


APPLICABILITY_SCHEMA = "capability.applicability.v1"


class CapabilityApplicabilityError(ValueError):
    """Raised only for a caller contract error, never for evidence content."""


_STATUS_PRIORITY = {
    "applicable": 0,
    "qualified": 1,
    "stale": 2,
    "blocked": 3,
    "quarantined": 4,
}
_BLOCKING_SOURCE_STATES = frozenset({"conflicted", "rejected", "blocked"})
_STALE_SOURCE_STATES = frozenset({"needs_refresh", "stale", "deprecated"})
_BLOCKING_REVIEW_STATES = frozenset({"rejected"})
_QUALIFYING_REVIEW_STATES = frozenset({"unreviewed"})
_BLOCKING_BINDING_STATES = frozenset({"disabled", "deprecated", "retired", "stale"})
_QUARANTINED_BINDING_STATES = frozenset({"quarantined"})


def evaluate_applicability(
    *,
    capability_scope: str,
    binding_descriptor: Mapping[str, Any],
    binding_status: str,
    observations: Sequence[Mapping[str, Any]],
    knowledge_links: Sequence[Mapping[str, Any]],
    requirement: Mapping[str, Any],
    at_time: str = "",
) -> dict[str, Any]:
    """Return a bounded applicability decision with exact input references.

    Inputs are already scoped by the caller.  This function consequently does
    not perform any fallback lookup and cannot accidentally mix tenants,
    agents, workspaces, users, or logical capability scopes.
    """

    if not isinstance(capability_scope, str) or not capability_scope.strip():
        raise CapabilityApplicabilityError("capability_scope is required")
    if not isinstance(binding_descriptor, Mapping):
        raise CapabilityApplicabilityError("binding_descriptor must be a mapping")
    if not isinstance(requirement, Mapping):
        raise CapabilityApplicabilityError("requirement must be a mapping")
    if isinstance(observations, (str, bytes)) or not isinstance(observations, Sequence):
        raise CapabilityApplicabilityError("observations must be a sequence")
    if isinstance(knowledge_links, (str, bytes)) or not isinstance(knowledge_links, Sequence):
        raise CapabilityApplicabilityError("knowledge_links must be a sequence")

    reference_time = _reference_time(at_time, observations=observations, knowledge_links=knowledge_links)
    environment = _environment_applicability(observations)
    binding = _binding_applicability(
        capability_scope=capability_scope,
        descriptor=binding_descriptor,
        effective_status=binding_status,
        environment=environment,
    )
    knowledge = _knowledge_applicability(
        capability_scope=capability_scope,
        rows=knowledge_links,
        environment=environment,
        reference_time=reference_time,
    )
    freshness = _freshness_applicability(
        observations=observations,
        requirement=requirement,
        reference_time=reference_time,
    )

    components = (binding, knowledge, freshness)
    status = max((str(item["status"]) for item in components), key=lambda item: _STATUS_PRIORITY[item])
    reason_codes = sorted(
        {
            str(reason)
            for item in components
            for reason in item.get("reason_codes", ())
            if str(reason or "")
        }
    )
    evidence_refs = sorted(
        {
            str(ref)
            for item in components
            for ref in item.get("evidence_refs", ())
            if str(ref or "")
        }
    )
    decision = {
        "schema": APPLICABILITY_SCHEMA,
        "status": status,
        "blocking": status in {"stale", "blocked", "quarantined"},
        "maturity_ceiling": _maturity_ceiling(status),
        "reference_time": reference_time,
        "binding": binding,
        "knowledge": knowledge,
        "environment": environment,
        "freshness": freshness,
        "reason_codes": reason_codes or ["applicability_explicitly_checked"],
        "evidence_refs": evidence_refs,
    }
    decision["input_digest"] = _digest(
        {
            "capability_scope": capability_scope,
            "binding": binding,
            "knowledge": knowledge,
            "environment": environment,
            "freshness": freshness,
            "reference_time": reference_time,
        }
    )
    return decision


def _binding_applicability(
    *,
    capability_scope: str,
    descriptor: Mapping[str, Any],
    effective_status: str,
    environment: Mapping[str, Any],
) -> dict[str, Any]:
    """Assess only declared binding restrictions and lifecycle state."""

    status = str(effective_status or descriptor.get("status") or "").strip().lower()
    raw_applicability = descriptor.get("applicability")
    applicability = dict(raw_applicability) if isinstance(raw_applicability, Mapping) else {}
    reasons: list[str] = []
    result_status = "applicable"
    if status in _QUARANTINED_BINDING_STATES:
        result_status = "quarantined"
        reasons.append("provider_binding_quarantined")
    elif status in _BLOCKING_BINDING_STATES:
        result_status = "blocked"
        reasons.append(f"provider_binding_{status}")
    elif status != "active":
        result_status = "blocked"
        reasons.append("provider_binding_not_active")

    declared_state = str(applicability.get("status") or applicability.get("state") or "").strip().lower()
    if applicability.get("enabled") is False:
        result_status = _more_restrictive(result_status, "blocked")
        reasons.append("binding_applicability_disabled")
    if declared_state in _QUARANTINED_BINDING_STATES:
        result_status = _more_restrictive(result_status, "quarantined")
        reasons.append("binding_applicability_quarantined")
    elif declared_state in _BLOCKING_BINDING_STATES | {"blocked", "unsupported", "unavailable"}:
        result_status = _more_restrictive(result_status, "blocked")
        reasons.append("binding_applicability_not_active")

    declared_scope = str(applicability.get("capability_scope") or applicability.get("scope") or "").strip()
    if declared_scope and declared_scope not in {"global", capability_scope}:
        result_status = _more_restrictive(result_status, "blocked")
        reasons.append("binding_capability_scope_mismatch")
    allowed_scopes = _string_set(applicability.get("allowed_scopes"))
    if allowed_scopes and capability_scope not in allowed_scopes:
        result_status = _more_restrictive(result_status, "blocked")
        reasons.append("binding_scope_not_allowed")

    environment_gate = _environment_constraint_gate(
        applicability,
        capability_scope=capability_scope,
        environment_digests=set(str(item) for item in environment.get("fingerprint_digests", ())),
    )
    result_status = _more_restrictive(result_status, str(environment_gate["status"]))
    reasons.extend(environment_gate["reason_codes"])
    evidence_refs = _refs(descriptor.get("advertisement_evidence_refs"))
    return {
        "status": result_status,
        "binding_id": str(descriptor.get("binding_id") or ""),
        "binding_digest": str(descriptor.get("binding_digest") or ""),
        "effective_status": status,
        "declared_applicability": applicability,
        "reason_codes": sorted(set(reasons)),
        "evidence_refs": evidence_refs,
        "input_digest": _digest(
            {
                "binding_id": descriptor.get("binding_id"),
                "binding_digest": descriptor.get("binding_digest"),
                "effective_status": status,
                "applicability": applicability,
                "environment_gate": environment_gate,
            }
        ),
    }


def _knowledge_applicability(
    *,
    capability_scope: str,
    rows: Sequence[Mapping[str, Any]],
    environment: Mapping[str, Any],
    reference_time: str,
) -> dict[str, Any]:
    """Make stale/contradicted knowledge visible without self-promoting state."""

    payloads = [dict(row.get("payload") or {}) for row in rows if isinstance(row, Mapping) and isinstance(row.get("payload"), Mapping)]
    if not payloads:
        return {
            "status": "applicable",
            "link_count": 0,
            "applicable_link_count": 0,
            "blocked_link_count": 0,
            "affects_maturity": False,
            "reason_codes": ["knowledge_not_linked"],
            "evidence_refs": [],
            "input_digest": _digest({"links": []}),
        }

    result_status = "applicable"
    reasons: list[str] = []
    evidence_refs: set[str] = set()
    applicable_count = 0
    blocked_count = 0
    link_digests: list[str] = []
    for payload in payloads:
        relation_type = str(payload.get("relation_type") or "").strip()
        source_status = str(payload.get("source_status") or "").strip()
        review_state = str(payload.get("review_state") or "").strip()
        contradiction_state = str(payload.get("contradiction_state") or "").strip()
        link_applicability = str(payload.get("applicability") or "").strip()
        link_id = str(payload.get("link_id") or "")
        link_digest = str(payload.get("link_digest") or "")
        if link_id:
            evidence_refs.add(link_id)
        if link_digest:
            link_digests.append(link_digest)
        evidence_refs.update(_refs(payload.get("evidence_refs")))
        evidence_refs.update(_refs(payload.get("applicability_evidence_refs")))

        is_contradiction = contradiction_state == "contradicted" or relation_type == "refutes"
        is_limiting = relation_type == "limits_applicability"
        source_blocked = source_status in _BLOCKING_SOURCE_STATES
        source_stale = source_status in _STALE_SOURCE_STATES
        review_blocked = review_state in _BLOCKING_REVIEW_STATES
        review_unverified = review_state in _QUALIFYING_REVIEW_STATES
        created_at = str(payload.get("created_at") or "")
        try:
            _parse_time(created_at)
            record_timestamp_invalid = False
        except ValueError:
            record_timestamp_invalid = True
        temporal_gate = _temporal_validity_gate(payload.get("temporal_validity"), reference_time)
        environment_gate = _environment_constraint_gate(
            payload.get("environment_constraints") if isinstance(payload.get("environment_constraints"), Mapping) else {},
            capability_scope=capability_scope,
            environment_digests=set(str(item) for item in environment.get("fingerprint_digests", ())),
        )
        link_is_current = (
            link_applicability == "applicable"
            and not source_blocked
            and not source_stale
            and not review_blocked
            and not review_unverified
            and not record_timestamp_invalid
            and not is_contradiction
            and temporal_gate["status"] == "applicable"
            and environment_gate["status"] == "applicable"
        )
        if link_is_current:
            applicable_count += 1
        else:
            blocked_count += 1

        # A refutation/contradiction is an explicit negative input.  A
        # limitation with invalid or unavailable constraints also fails closed:
        # we cannot safely assume the current environment is in range.
        if is_contradiction:
            result_status = _more_restrictive(result_status, "blocked")
            reasons.append("knowledge_contradiction")
        elif record_timestamp_invalid:
            result_status = _more_restrictive(result_status, "blocked")
            reasons.append("knowledge_link_timestamp_invalid")
        elif is_limiting and (
            source_blocked
            or source_stale
            or review_blocked
            or review_unverified
            or link_applicability != "applicable"
            or temporal_gate["status"] != "applicable"
            or environment_gate["status"] != "applicable"
        ):
            result_status = _more_restrictive(result_status, "blocked")
            reasons.append("knowledge_limit_not_applicable")
        elif source_blocked or review_blocked or link_applicability == "blocked":
            result_status = _more_restrictive(result_status, "blocked")
            reasons.append("knowledge_context_blocked")
        elif source_stale:
            result_status = _more_restrictive(result_status, "stale")
            reasons.append("knowledge_source_stale")
        elif review_unverified or link_applicability != "applicable":
            result_status = _more_restrictive(result_status, "qualified")
            reasons.append("knowledge_context_not_current")
        elif temporal_gate["status"] != "applicable":
            result_status = _more_restrictive(result_status, "stale")
            reasons.extend(temporal_gate["reason_codes"])
        elif environment_gate["status"] != "applicable":
            result_status = _more_restrictive(result_status, "qualified")
            reasons.extend(environment_gate["reason_codes"])

    return {
        "status": result_status,
        "link_count": len(payloads),
        "applicable_link_count": applicable_count,
        "blocked_link_count": blocked_count,
        "affects_maturity": False,
        "link_digests": sorted(set(link_digests)),
        "reason_codes": sorted(set(reasons)) or ["knowledge_context_checked"],
        "evidence_refs": sorted(evidence_refs),
        "input_digest": _digest(
            {
                "reference_time": reference_time,
                "links": [
                    {
                        "link_id": payload.get("link_id"),
                        "link_digest": payload.get("link_digest"),
                        "applicability": payload.get("applicability"),
                        "source_status": payload.get("source_status"),
                        "review_state": payload.get("review_state"),
                        "contradiction_state": payload.get("contradiction_state"),
                        "temporal_validity": payload.get("temporal_validity"),
                        "environment_constraints": payload.get("environment_constraints"),
                    }
                    for payload in payloads
                ],
            }
        ),
    }


def _environment_applicability(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    fingerprints: list[dict[str, Any]] = []
    observation_refs: list[str] = []
    for row in observations:
        if not isinstance(row, Mapping):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        fingerprint = payload.get("environment_fingerprint") if isinstance(payload.get("environment_fingerprint"), Mapping) else {}
        if fingerprint:
            fingerprints.append(dict(fingerprint))
        observation_id = str(row.get("observation_id") or "")
        if observation_id:
            observation_refs.append(observation_id)
    digests = sorted({_digest(value) for value in fingerprints})
    return {
        "status": "applicable" if fingerprints else "qualified",
        "observation_environment_count": len(digests),
        "fingerprint_digests": digests,
        "host_identity_not_used": True,
        "reason_codes": [] if fingerprints else ["observation_environment_missing"],
        "evidence_refs": sorted(set(observation_refs)),
        "input_digest": _digest({"fingerprint_digests": digests, "observation_refs": sorted(set(observation_refs))}),
    }


def _freshness_applicability(
    *,
    observations: Sequence[Mapping[str, Any]],
    requirement: Mapping[str, Any],
    reference_time: str,
) -> dict[str, Any]:
    max_age = requirement.get("max_evidence_age_seconds", 0)
    if isinstance(max_age, bool) or not isinstance(max_age, int) or max_age < 0:
        return {
            "status": "blocked",
            "max_evidence_age_seconds": None,
            "latest_observed_at": "",
            "reason_codes": ["profile_freshness_requirement_invalid"],
            "evidence_refs": [],
            "input_digest": _digest({"invalid_max_age": repr(max_age)}),
        }
    timestamps: list[tuple[str, str]] = []
    for row in observations:
        if not isinstance(row, Mapping):
            continue
        observed_at = str(row.get("observed_at") or "")
        observation_id = str(row.get("observation_id") or "")
        if observed_at:
            timestamps.append((observed_at, observation_id))
    latest_observed_at, latest_ref = max(timestamps, default=("", ""))
    if max_age == 0:
        return {
            "status": "applicable",
            "max_evidence_age_seconds": 0,
            "latest_observed_at": latest_observed_at,
            "reason_codes": [],
            "evidence_refs": [latest_ref] if latest_ref else [],
            "input_digest": _digest({"max_age": 0, "latest": latest_observed_at}),
        }
    if not latest_observed_at:
        return {
            "status": "stale",
            "max_evidence_age_seconds": max_age,
            "latest_observed_at": "",
            "reason_codes": ["evidence_freshness_unknown"],
            "evidence_refs": [],
            "input_digest": _digest({"max_age": max_age, "latest": "", "reference_time": reference_time}),
        }
    try:
        age_seconds = max(0.0, (_parse_time(reference_time) - _parse_time(latest_observed_at)).total_seconds())
    except ValueError:
        return {
            "status": "blocked",
            "max_evidence_age_seconds": max_age,
            "latest_observed_at": latest_observed_at,
            "reason_codes": ["evidence_freshness_timestamp_invalid"],
            "evidence_refs": [latest_ref] if latest_ref else [],
            "input_digest": _digest({"max_age": max_age, "latest": latest_observed_at, "reference_time": reference_time}),
        }
    stale = age_seconds > max_age
    return {
        "status": "stale" if stale else "applicable",
        "max_evidence_age_seconds": max_age,
        "latest_observed_at": latest_observed_at,
        "age_seconds": round(age_seconds, 6),
        "reason_codes": ["evidence_freshness_expired"] if stale else [],
        "evidence_refs": [latest_ref] if latest_ref else [],
        "input_digest": _digest(
            {"max_age": max_age, "latest": latest_observed_at, "reference_time": reference_time}
        ),
    }


def _temporal_validity_gate(value: object, reference_time: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "blocked", "reason_codes": ["knowledge_temporal_validity_missing"]}
    validity = dict(value)
    start = str(validity.get("not_before") or validity.get("valid_from") or "")
    end = str(validity.get("not_after") or validity.get("valid_until") or validity.get("expires_at") or "")
    try:
        reference = _parse_time(reference_time)
        if start and reference < _parse_time(start):
            return {"status": "stale", "reason_codes": ["knowledge_not_yet_temporally_valid"]}
        if end and reference > _parse_time(end):
            return {"status": "stale", "reason_codes": ["knowledge_temporal_validity_expired"]}
    except ValueError:
        return {"status": "blocked", "reason_codes": ["knowledge_temporal_validity_invalid"]}
    return {"status": "applicable", "reason_codes": []}


def _environment_constraint_gate(
    value: Mapping[str, Any],
    *,
    capability_scope: str,
    environment_digests: set[str],
) -> dict[str, Any]:
    constraints = dict(value) if isinstance(value, Mapping) else {}
    declared_scope = str(constraints.get("capability_scope") or constraints.get("scope") or "").strip()
    if declared_scope and declared_scope not in {"global", capability_scope}:
        return {"status": "blocked", "reason_codes": ["declared_capability_scope_mismatch"]}
    allowed_scopes = _string_set(constraints.get("allowed_scopes"))
    if allowed_scopes and capability_scope not in allowed_scopes:
        return {"status": "blocked", "reason_codes": ["declared_capability_scope_not_allowed"]}
    expected = str(constraints.get("environment_digest") or constraints.get("fingerprint_digest") or "").strip()
    allowed_digests = _string_set(constraints.get("allowed_environment_digests"))
    if expected and expected not in environment_digests:
        return {"status": "blocked", "reason_codes": ["environment_constraint_not_satisfied"]}
    if allowed_digests and not environment_digests.intersection(allowed_digests):
        return {"status": "blocked", "reason_codes": ["environment_not_in_declared_applicability"]}
    return {"status": "applicable", "reason_codes": []}


def _reference_time(
    value: str,
    *,
    observations: Sequence[Mapping[str, Any]],
    knowledge_links: Sequence[Mapping[str, Any]],
) -> str:
    if value:
        _parse_time(value)
        return value
    material_times: list[str] = []
    for row in observations:
        if isinstance(row, Mapping) and str(row.get("observed_at") or ""):
            material_times.append(str(row["observed_at"]))
    for row in knowledge_links:
        payload = row.get("payload") if isinstance(row, Mapping) and isinstance(row.get("payload"), Mapping) else {}
        if str(payload.get("created_at") or ""):
            material_times.append(str(payload["created_at"]))
    if material_times:
        # A live projection without an explicit as-of time is reproducible from
        # its immutable inputs.  Schedulers that require wall-clock freshness
        # pass ``at_time`` explicitly; a readiness read must not create a new
        # snapshot merely because it was repeated a second later.
        return max(material_times)
    return now_iso()


def _parse_time(value: str) -> datetime:
    normalized = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _maturity_ceiling(status: str) -> str:
    if status == "quarantined":
        return "quarantined"
    if status in {"blocked", "stale"}:
        return "observed"
    if status == "qualified":
        return "evaluated"
    return "reliable"


def _more_restrictive(left: str, right: str) -> str:
    return left if _STATUS_PRIORITY.get(left, -1) >= _STATUS_PRIORITY.get(right, -1) else right


def _refs(value: object) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    return sorted({str(item) for item in value if str(item or "")})


def _string_set(value: object) -> set[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "APPLICABILITY_SCHEMA",
    "CapabilityApplicabilityError",
    "evaluate_applicability",
]
