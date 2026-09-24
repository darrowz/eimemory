"""Conservative loop-stage evidence joins for L5; no promotion side effects.

An immutable knowledge link is a historical assertion, not present authority.
A current-record digest and live eligibility check are required for loop credit.
Historical horizons filter evaluations but do not reconstruct historical source
files: unavailable present authority never receives positive credit.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

_POSITIVE_LINKS = frozenset({"supports", "informs_eval", "informs_change", "explains_outcome"})
_POSITIVE_MATURITY = frozenset({"observed", "evaluated", "reliable"})


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _payload(row: Any) -> Mapping:
    payload = row.get("payload") if isinstance(row, Mapping) else None
    return payload if isinstance(payload, Mapping) else {}


def _declared_applicable(payload: Mapping) -> bool:
    return (payload.get("source_status") == "active"
            and payload.get("applicability") == "applicable"
            and payload.get("source_trust") in {"medium", "high"}
            and payload.get("review_state") in {"reviewed", "approved"}
            and payload.get("contradiction_state") in {"none", "resolved"})


def _current_link(store, scope, row: Mapping, payload: Mapping) -> bool:
    """Re-use the existing bridge's current canonical-artifact verifier."""
    from eimemory.knowledge.capabilities import assess_knowledge_capability_eligibility
    from eimemory.storage.jsonl import payload_digest

    record_id = payload.get("knowledge_record_id")
    expected = row.get("knowledge_record_digest")
    if not isinstance(record_id, str) or not record_id or not isinstance(expected, str) or not expected:
        return False
    record = store.get_by_id(record_id, scope=scope)
    if record is None or record.scope != scope or record.status != "active":
        return False
    if payload_digest(record.to_dict()) != expected:
        return False
    assessment = assess_knowledge_capability_eligibility(
        store, knowledge_record=record, runtime_scope=scope,
        source_trust=payload.get("source_trust"), review_state=payload.get("review_state"),
        temporal_validity=payload.get("temporal_validity"),
        environment_constraints=payload.get("environment_constraints"),
    )
    provenance = payload.get("provenance")
    artifact = provenance.get("artifact_digest") if isinstance(provenance, Mapping) else None
    return (isinstance(artifact, str) and bool(artifact) and artifact == assessment.artifact_digest
            and assessment.applicability == "applicable" and assessment.source_status == "active"
            and assessment.canonical_artifact_verified is True
            and assessment.contradiction_state in {"none", "resolved"})


def loop_maturity(store, scope, capability_scope: str, projection: Mapping, *, at_time: str = "") -> str:
    if projection.get("blocked"):
        return "diagnosing"
    snapshots = [item for item in projection.get("snapshots") or () if isinstance(item, Mapping)]
    if not snapshots:
        return "observing"
    horizon = _time(at_time) if at_time else datetime.now(timezone.utc)
    profile_id = projection.get("profile_id")
    if horizon is None or not isinstance(profile_id, str) or not profile_id:
        return "diagnosing"
    try:
        runs = store.read_capabilities(lambda repository: repository.list_evaluation_runs(
            scope=scope, capability_scope=capability_scope, profile_id=profile_id, limit=500))
        if not runs:
            return "observing"
        latest = {}
        for row in runs:
            payload = _payload(row)
            if not payload:
                continue
            finished = _time(row.get("finished_at"))
            target = (payload.get("capability_revision_id"), payload.get("provider_binding_id"))
            if finished is None or finished > horizon or not all(isinstance(key, str) and key for key in target):
                continue
            # Some historical DTOs omit profile_id in their payload; the typed
            # storage query above is authoritative. Explicit contradictions fail.
            if payload.get("profile_id", profile_id) != profile_id:
                continue
            passed = payload.get("verdict") == "pass"
            old = latest.get(target)
            if old is None or finished > old[0]:
                latest[target] = (finished, passed)
            elif finished == old[0]:
                latest[target] = (finished, old[1] and passed)
        passing = {target for target, (_, passed) in latest.items() if passed}
        if not passing:
            return "experimenting"
        links = store.read_capabilities(lambda repository: repository.list_knowledge_links(
            scope=scope, capability_scope=capability_scope, limit=500))
        # A restrictive assertion about the SAME record version cannot be
        # bypassed by choosing an older positive link from the bounded set.
        vetoes = {(p.get("capability_revision_id"), p.get("knowledge_record_id"), row.get("knowledge_record_digest"))
                  for row in links if isinstance(row, Mapping)
                  for p in [_payload(row)] if p and not _declared_applicable(p)}
        linked = set()
        for row in links:
            payload = _payload(row)
            if not payload or not _declared_applicable(payload) or payload.get("relation_type") not in _POSITIVE_LINKS:
                continue
            revision = payload.get("capability_revision_id")
            key = (revision, payload.get("knowledge_record_id"), row.get("knowledge_record_digest"))
            created = _time(payload.get("created_at"))
            if (key in vetoes or created is None or created > horizon
                    or not any(target[0] == revision for target in passing)):
                continue
            if _current_link(store, scope, row, payload):
                linked.add(revision)
        capabilities = set()
        for item in snapshots:
            target = (item.get("capability_revision_id"), item.get("provider_binding_id"))
            maturity = item.get("maturity_state", item.get("maturity"))
            computed = _time(item.get("computed_at"))
            capability = item.get("capability_id")
            if (target in passing and target[0] in linked and maturity in _POSITIVE_MATURITY
                    and computed is not None and computed <= horizon
                    and isinstance(capability, str) and capability):
                capabilities.add(capability)
        if not capabilities:
            return "experimenting"
        return "compounding" if len(capabilities) >= 2 else "evolving"
    except Exception:
        # Optional/obsolete storage or source authority is diagnostic work,
        # never a reason to reuse the previous successful maturity stage.
        return "diagnosing"
