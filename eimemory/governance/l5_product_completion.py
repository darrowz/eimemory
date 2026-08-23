"""Evidence-derived full-product L5 completion semantics.

The v3 assessment remains the raw control-plane result.  This module adds the
separate product envelope and deliberately treats bootstrap/manual evidence as
non-qualifying, even when the lower-level components are healthy.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


QUALIFYING_OUTCOMES = frozenset({"succeeded_sedimented", "rolled_back_healthy"})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bool(value: Any) -> bool:
    return value is True


def _explicit_false(value: Any) -> bool:
    """Require a recorded negative provenance fact, not a missing field."""

    return value is False


def _sha256_digest(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def build_product_completion(
    assessment: Mapping[str, Any] | None,
    *,
    provider: Mapping[str, Any] | None = None,
    transaction: Mapping[str, Any] | None = None,
    current_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a truthful v4 envelope without manufacturing missing evidence."""

    raw = _mapping(assessment)
    provider_data = _mapping(provider)
    transaction_data = _mapping(transaction)
    lineage = _mapping(current_lineage)
    control_plane_ok = _bool(raw.get("ok")) and str(raw.get("status") or "") not in {"blocked", "degraded"}
    control_plane_status = "ready" if control_plane_ok else str(raw.get("status") or "blocked")
    if control_plane_status not in {"ready", "degraded", "blocked", "incomplete"}:
        control_plane_status = "blocked"

    axes = {
        "loop_maturity": str(raw.get("loop_maturity") or "unknown"),
        "capability_ready": _bool(raw.get("ok")),
        "adapter_ready": _adapter_ready(raw),
        "deployment_assurance": _deployment_status(raw),
    }
    gaps: list[str] = []
    if not control_plane_ok:
        gaps.append("raw_control_plane_not_ready")

    provider_ready = _bool(provider_data.get("ready")) or _bool(provider_data.get("provider_ready"))
    catalog_ready = _bool(provider_data.get("catalog_ready"))
    advertisement_fresh = _bool(provider_data.get("advertisement_fresh"))
    if not provider_ready:
        gaps.append("provider_not_ready")
    if not catalog_ready:
        gaps.append("catalog_not_ready")
    if not advertisement_fresh:
        gaps.append("advertisement_not_fresh")

    terminal_receipt_bound = bool(
        str(transaction_data.get("transaction_id") or "").strip()
        and _sha256_digest(transaction_data.get("terminal_receipt_digest"))
    )
    if not terminal_receipt_bound:
        gaps.append("terminal_receipt_unbound")
    transaction_evidence_verified = _bool(transaction_data.get("evidence_verified"))
    if not transaction_evidence_verified:
        gaps.append("transaction_evidence_unverified")

    outcome = str(
        transaction_data.get("qualifying_terminal_outcome")
        or transaction_data.get("outcome")
        or ""
    ) or None
    manual_bootstrap = _bool(transaction_data.get("manual_bootstrap"))
    known_before_detection = _bool(transaction_data.get("known_before_detection"))
    prior_user_reported = _bool(transaction_data.get("prior_user_reported"))
    origin = str(transaction_data.get("origin") or "")
    quarantined = _bool(transaction_data.get("quarantined")) or outcome == "recovery_quarantined"
    observation_valid = _bool(transaction_data.get("observation_valid"))
    if outcome not in QUALIFYING_OUTCOMES:
        gaps.append("no_qualifying_terminal_receipt")
    if manual_bootstrap:
        gaps.append("manual_bootstrap_nonqualifying")
    if known_before_detection:
        gaps.append("incident_known_before_system_detection")
    if prior_user_reported or origin != "system_detector":
        gaps.append("incident_not_system_originated")
    if not _explicit_false(transaction_data.get("known_before_detection")):
        gaps.append("incident_prior_knowledge_unproven")
    if not _explicit_false(transaction_data.get("prior_user_reported")):
        gaps.append("incident_not_user_reported_unproven")
    if quarantined:
        gaps.append("transaction_quarantined")
    if _bool(transaction_data.get("nonterminal")):
        gaps.append("nonterminal_transaction_exists")
    if not observation_valid:
        gaps.append("observation_not_valid")
    if outcome == "rolled_back_healthy":
        if not _bool(transaction_data.get("candidate_pushed_and_deployed")):
            gaps.append("rollback_candidate_not_deployed")
        if not _bool(transaction_data.get("rollback_executed")):
            gaps.append("rollback_not_executed")

    lineage_ok = _bool(lineage.get("ok")) and _bool(lineage.get("compatible"))
    if not lineage_ok:
        gaps.append("current_lineage_incompatible")

    # Preserve order for stable reports while suppressing duplicate diagnostics
    # when a caller supplies overlapping evidence failures.
    gaps = list(dict.fromkeys(gaps))
    qualifying_transaction = (
        outcome in QUALIFYING_OUTCOMES
        and terminal_receipt_bound
        and transaction_evidence_verified
        and not manual_bootstrap
        and _explicit_false(transaction_data.get("known_before_detection"))
        and _explicit_false(transaction_data.get("prior_user_reported"))
        and origin == "system_detector"
        and not quarantined
        and not _bool(transaction_data.get("nonterminal"))
        and observation_valid
        and (outcome != "rolled_back_healthy" or (
            _bool(transaction_data.get("candidate_pushed_and_deployed"))
            and _bool(transaction_data.get("rollback_executed"))
        ))
    )
    product_complete = (
        control_plane_ok
        and provider_ready
        and catalog_ready
        and advertisement_fresh
        and qualifying_transaction
        and lineage_ok
    )
    return {
        "schema_version": "l5_readiness.v4",
        "control_plane_ok": control_plane_ok,
        "control_plane_status": control_plane_status,
        "axes": axes,
        "code_evolution": {
            "provider_ready": provider_ready,
            "catalog_ready": catalog_ready,
            "advertisement_fresh": advertisement_fresh,
            "transaction_verified": qualifying_transaction,
            "qualifying_terminal_outcome": outcome,
            "label": outcome if outcome in QUALIFYING_OUTCOMES else None,
            "current_lineage_compatible": lineage_ok,
            "observation_valid": observation_valid,
            "gaps": list(gaps),
        },
        "product_l5_complete": product_complete,
        "completion_status": "complete" if product_complete else "incomplete",
        "gaps": list(gaps),
        "ok": product_complete,
        "status": "ready" if product_complete else "incomplete",
    }


def _adapter_ready(assessment: Mapping[str, Any]) -> bool:
    value = assessment.get("adapter_readiness")
    if isinstance(value, Mapping):
        if not value:
            return False
        return all(str(item).lower() in {"ready", "ok", "true"} for item in value.values())
    return _bool(value)


def _deployment_status(assessment: Mapping[str, Any]) -> str:
    value = assessment.get("deployment_assurance")
    if isinstance(value, Mapping):
        if value.get("ok") is True:
            return "ready"
        if value.get("blocking") is True:
            return "blocked"
        if value.get("required") is False or value.get("ok") is None:
            return "neutral"
    if isinstance(value, str) and value:
        return value
    return "unknown"


__all__ = ["QUALIFYING_OUTCOMES", "build_product_completion"]
