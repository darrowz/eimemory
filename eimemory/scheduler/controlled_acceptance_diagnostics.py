"""Bounded presentation of the nightly's independently verified receipt.

This module performs no authorization. The producer must verify the native
receipt before setting runtime_authority_verified; natural quality is separate.
"""
from __future__ import annotations

import re


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
    return {
        "status": "accepted" if accepted else "not_accepted",
        "accepted": accepted,
        "mode": "controlled_existing_memory",
        "evidence_class": "operator_controlled_existing_memory",
        "certifies_natural_queries": False,
        "natural_samples_created": 0,
        "agent_count": len(agents) if isinstance(agents, list) else 0,
        "record_id": str(value.get("record_id") or "")[:128],
        "policy_digest": policy_digest if isinstance(policy_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", policy_digest) else "",
    }
