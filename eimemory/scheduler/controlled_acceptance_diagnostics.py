"""Bounded presentation of the nightly's independently verified receipt.

This module performs no authorization. The producer must verify the native
receipt before setting runtime_authority_verified. Historical business origin
does not change its controlled validation method or create online samples.
"""
from __future__ import annotations

import re


_BUSINESS_PURPOSE = "historical_business_qualification"


def _historical_business_agents(agents: object) -> dict:
    """Present only verified public counts, never source text or exact scopes."""
    if not isinstance(agents, list) or not 1 <= len(agents) <= 100:
        return {}
    results = {}
    scopes = set()
    for entry in agents:
        if not isinstance(entry, dict):
            return {}
        agent = entry.get("agent_id")
        scope = entry.get("scope_digest")
        sources = entry.get("source_ids")
        count = entry.get("sample_count")
        if (not isinstance(agent, str) or not 1 <= len(agent) <= 64
                or agent.strip() != agent or not agent.isprintable()
                or agent in results or not isinstance(scope, str)
                or re.fullmatch(r"[0-9a-f]{64}", scope) is None or scope in scopes
                or not isinstance(sources, list) or not 1 <= len(sources) <= 64
                or any(not isinstance(source, str) or not 1 <= len(source) <= 128
                       or re.fullmatch(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?", source) is None
                       for source in sources)
                or len(set(sources)) != len(sources)
                or type(count) is not int or not 3 <= count <= 1_000_000
                or type(entry.get("pass_count")) is not int or entry["pass_count"] != count
                or type(entry.get("fail_count")) is not int or entry["fail_count"] != 0
                or entry.get("accepted") is not True or entry.get("complete") is not True
                or entry.get("blocked_reason") != ""):
            return {}
        scopes.add(scope)
        results[agent] = {"sample_count": count}
    return results


def controlled_acceptance_diagnostics(value: object) -> dict:
    if not isinstance(value, dict):
        return {"status": "not_run", "accepted": False}
    if value.get("status") == "not_configured" and value.get("ok") is True:
        return {"status": "not_configured", "accepted": False}
    verification = value.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    policy_digest = value.get("policy_digest")
    identity = value.get("release_identity")
    agents = value.get("agents")
    accepted = bool(
        value.get("ok") is True and value.get("accepted") is True
        and value.get("runtime_authority_verified") is True
        and value.get("schema") == "controlled_production_acceptance.v1"
        and value.get("mode") == "controlled_existing_memory"
        and value.get("evidence_class") == "operator_controlled_existing_memory"
        and value.get("certifies_natural_queries") is False
        and type(value.get("natural_samples_created")) is int
        and value["natural_samples_created"] == 0
        and isinstance(policy_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", policy_digest)
        and isinstance(identity, dict) and identity
        and isinstance(agents, list) and agents
        and isinstance(value.get("record_id"), str) and value["record_id"]
        and verification.get("ok") is True and verification.get("accepted") is True
        and verification.get("record_id") == value["record_id"]
        and verification.get("policy_digest") == policy_digest
        and verification.get("release_identity") == identity
        and verification.get("schema") == value["schema"]
        and verification.get("mode") == value["mode"]
        and verification.get("certifies_natural_queries") is False
    )
    historical_agents = _historical_business_agents(agents)
    business = value.get("business_qualification")
    business = business if isinstance(business, dict) else {}
    sample_count = sum(item["sample_count"] for item in historical_agents.values())
    historical_accepted = bool(
        accepted and historical_agents and value.get("persisted") is True
        and verification.get("persisted") is True
        and value.get("status") == "accepted" and verification.get("status") == "accepted"
        and verification.get("evidence_class") == "operator_controlled_existing_memory"
        and type(verification.get("natural_samples_created")) is int
        and verification["natural_samples_created"] == 0
        and not any(part.get("error") or part.get("errors") or part.get("blocked_reason")
                    for part in (value, verification))
        and verification.get("agents") == agents
        and set(identity) == {"release_commit", "release_version", "deployment_receipt_id", "release_session_id"}
        and re.fullmatch(r"[0-9a-f]{40}", str(identity.get("release_commit", ""))) is not None
        and all(isinstance(item, str) and 1 <= len(item) <= 256 and item.isprintable()
                for item in identity.values())
        and len(value["record_id"]) <= 128 and value["record_id"].isprintable()
        and value.get("acceptance_purpose") == _BUSINESS_PURPOSE
        and verification.get("acceptance_purpose") == _BUSINESS_PURPOSE
        and verification.get("business_qualification") == business
        and set(business) == {"schema", "purpose", "accepted", "business_origin", "validation_method",
                             "sample_count", "pass_count", "fail_count", "agents", "policy_digest",
                             "record_id", "release_identity"}
        and business.get("schema") == "historical_business_qualification.v1"
        and business.get("purpose") == _BUSINESS_PURPOSE
        and business.get("accepted") is True
        and business.get("business_origin") == "historical_business"
        and business.get("validation_method") == "controlled_recall"
        and business.get("agents") == agents
        and business.get("policy_digest") == policy_digest
        and business.get("record_id") == value["record_id"]
        and business.get("release_identity") == identity
        and type(business.get("sample_count")) is int and business["sample_count"] == sample_count
        and type(business.get("pass_count")) is int and business["pass_count"] == sample_count
        and type(business.get("fail_count")) is int and business["fail_count"] == 0
        and all(value.get(key) is False and verification.get(key) is False
                for key in ("certifies_no_answer", "host_delivery_certified", "memory_contribution_certified"))
    )
    return {
        "status": "accepted" if accepted else "not_accepted",
        "accepted": accepted,
        "mode": "controlled_existing_memory",
        "evidence_class": "operator_controlled_existing_memory",
        "acceptance_purpose": value.get("acceptance_purpose", "operational")
            if value.get("acceptance_purpose", "operational") in ("operational", _BUSINESS_PURPOSE)
            else "unknown",
        "certifies_natural_queries": False,
        "natural_samples_created": 0,
        "agent_count": len(agents) if isinstance(agents, list) else 0,
        "record_id": str(value.get("record_id") or "")[:128],
        "policy_digest": policy_digest if isinstance(policy_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", policy_digest) else "",
        "business_qualification": {
            "schema": "historical_business_qualification.v1",
            "purpose": _BUSINESS_PURPOSE,
            "accepted": historical_accepted,
            **({"business_origin": "historical_business", "validation_method": "controlled_recall",
                "sample_count": sample_count, "pass_count": sample_count, "fail_count": 0,
                "policy_digest": policy_digest, "record_id": value["record_id"],
                "release_identity": dict(identity),
                "agents": [{key: entry[key] for key in (
                    "agent_id", "scope_digest", "source_ids", "sample_count", "pass_count", "fail_count",
                    "accepted", "complete", "blocked_reason")} for entry in agents]}
               if historical_accepted else {}),
        },
    }
