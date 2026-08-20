"""Fail-closed links from reviewed knowledge to capability revisions.

This module deliberately stops at evidence attribution.  A knowledge link is
not an observation, evaluation run, maturity input, or promotion signal.  It
can support a separately persisted hypothesis, but it cannot make a
capability more mature by itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Any

from eimemory.capabilities.contracts import (
    CapabilityContractError,
    normalize_capability_id,
    normalize_json_payload,
    normalize_opaque_id,
)
from eimemory.capabilities.models import (
    KNOWLEDGE_LINK_TYPES,
    KNOWLEDGE_REVIEW_STATES,
    KNOWLEDGE_SOURCE_STATUSES,
    KNOWLEDGE_TRUST_LEVELS,
    CapabilityKnowledgeLink,
)
from eimemory.capabilities.registry import CapabilityRegistry, CapabilityRegistryError, exact_runtime_scope
from eimemory.intake.papers import artifacts as paper_artifacts
from eimemory.intake.papers.artifacts import PaperArtifactError
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import payload_digest
from eimemory.storage.runtime_store import RuntimeStore


KNOWLEDGE_CAPABILITY_BRIDGE_SCHEMA = "knowledge.capability_bridge.v1"
KNOWLEDGE_CAPABILITY_CONTEXT_SCHEMA = "knowledge.capability_context.v1"
KNOWLEDGE_CAPABILITY_REFRESH_SCHEMA = "knowledge.capability_refresh.v1"
KNOWLEDGE_CAPABILITY_MARKER_KEY = "knowledge_capability_applicability"

__all__ = [
    "KNOWLEDGE_CAPABILITY_BRIDGE_SCHEMA",
    "KNOWLEDGE_CAPABILITY_CONTEXT_SCHEMA",
    "KNOWLEDGE_CAPABILITY_REFRESH_SCHEMA",
    "KNOWLEDGE_CAPABILITY_MARKER_KEY",
    "KnowledgeCapabilityAssessment",
    "KnowledgeCapabilityBridgeError",
    "KnowledgeCapabilityLinkResult",
    "assess_knowledge_capability_eligibility",
    "list_registered_knowledge_links",
    "load_registered_knowledge_link",
    "normalize_capability_context",
    "refresh_capability_applicability_marker",
    "register_knowledge_capability_link",
]

_HARD_BLOCKED_SOURCE_STATUSES = frozenset(
    {"rejected", "deprecated", "conflicted", "needs_refresh", "stale", "unverified", "blocked"}
)
_SOURCE_STATUS_PRIORITY = {
    "active": 0,
    "candidate": 1,
    "unverified": 2,
    "deprecated": 3,
    "stale": 4,
    "needs_refresh": 5,
    "conflicted": 6,
    "blocked": 7,
    "rejected": 8,
}


class KnowledgeCapabilityBridgeError(RuntimeError):
    """Raised when a knowledge-to-capability operation cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class KnowledgeCapabilityAssessment:
    """A reproducible, non-promoting applicability decision for one record."""

    knowledge_record_id: str
    paper_source_id: str
    source_status: str
    applicability: str
    source_trust: str
    review_state: str
    temporal_validity: dict[str, Any]
    environment_constraints: dict[str, Any]
    contradiction_state: str
    canonical_artifact_verified: bool
    artifact_digest: str
    reasons: tuple[str, ...]
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": KNOWLEDGE_CAPABILITY_BRIDGE_SCHEMA,
            "knowledge_record_id": self.knowledge_record_id,
            "paper_source_id": self.paper_source_id,
            "source_status": self.source_status,
            "applicability": self.applicability,
            "source_trust": self.source_trust,
            "review_state": self.review_state,
            "temporal_validity": dict(self.temporal_validity),
            "environment_constraints": dict(self.environment_constraints),
            "contradiction_state": self.contradiction_state,
            "canonical_artifact_verified": self.canonical_artifact_verified,
            "artifact_digest": self.artifact_digest,
            "reasons": list(self.reasons),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class KnowledgeCapabilityLinkResult:
    """The immutable link plus its storage receipt and applicability evidence."""

    link: CapabilityKnowledgeLink
    receipt: Any
    assessment: KnowledgeCapabilityAssessment

    def to_dict(self) -> dict[str, Any]:
        receipt = self.receipt
        if hasattr(receipt, "to_dict"):
            receipt_payload: dict[str, Any] = receipt.to_dict()
        elif isinstance(receipt, Mapping):
            receipt_payload = dict(receipt)
        else:
            receipt_payload = {
                key: getattr(receipt, key)
                for key in ("entity_type", "entity_id", "entity_digest", "operation_id", "ledger_event_id", "idempotent")
                if hasattr(receipt, key)
            }
        return {
            "link": self.link.to_dict(),
            "receipt": receipt_payload,
            "assessment": self.assessment.to_dict(),
        }


def normalize_capability_context(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Validate an explicit, non-inferential claim/relation capability context.

    Text in a claim, entity, relation, title, or paper is never interpreted as
    a capability identifier.  Callers must provide all identity fields
    deliberately, and this helper only carries them forward as provenance.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise KnowledgeCapabilityBridgeError("capability_context must be an object")
    required = {"capability_id", "capability_revision_id", "capability_scope", "relation_type"}
    allowed = {*required, "schema_version"}
    unknown = set(value).difference(allowed)
    if unknown:
        raise KnowledgeCapabilityBridgeError(
            f"capability_context has unsupported fields: {', '.join(sorted(str(item) for item in unknown))}"
        )
    if not required.issubset(value):
        raise KnowledgeCapabilityBridgeError("capability_context requires capability identity, revision, scope, and relation type")
    try:
        schema_version = str(value.get("schema_version") or "").strip()
        if schema_version and schema_version != KNOWLEDGE_CAPABILITY_CONTEXT_SCHEMA:
            raise CapabilityContractError("capability_context schema_version is unsupported")
        relation_type = str(value["relation_type"] or "").strip()
        if relation_type not in KNOWLEDGE_LINK_TYPES:
            raise CapabilityContractError("capability_context relation_type is not a knowledge-link type")
        return {
            "schema_version": KNOWLEDGE_CAPABILITY_CONTEXT_SCHEMA,
            "capability_id": normalize_capability_id(value["capability_id"]),
            "capability_revision_id": normalize_opaque_id(
                value["capability_revision_id"], field="capability_context.capability_revision_id"
            ),
            "capability_scope": normalize_opaque_id(
                value["capability_scope"], field="capability_context.capability_scope"
            ),
            "relation_type": relation_type,
        }
    except CapabilityContractError as exc:
        raise KnowledgeCapabilityBridgeError(str(exc)) from exc


def refresh_capability_applicability_marker(
    refresh_state: str,
    *,
    reason: str = "",
    resolved_contradiction_ids: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Return a passive refresh marker without recreating a direct conflict gate.

    The knowledge projector treats direct ``contradiction_ids`` as unsafe.  A
    rebuilt page therefore carries only resolved audit ids in this separate
    marker; the link bridge can keep the trace while the projector sees a safe
    page built from active claims.
    """

    state = str(refresh_state or "").strip().lower()
    mapping = {
        "blocked": ("needs_refresh", "blocked", "none"),
        "superseded": ("deprecated", "blocked", "none"),
        "recompiled": ("active", "candidate", "resolved"),
    }
    if state not in mapping:
        raise KnowledgeCapabilityBridgeError("unsupported knowledge refresh state")
    source_status, applicability, contradiction_state = mapping[state]
    audit_ids = _safe_string_list(resolved_contradiction_ids or (), field="resolved_contradiction_ids")
    return {
        "schema_version": KNOWLEDGE_CAPABILITY_REFRESH_SCHEMA,
        "refresh_state": state,
        "source_status": source_status,
        "applicability": applicability,
        "contradiction_state": contradiction_state,
        "reason": str(reason or "").strip()[:512],
        "resolved_contradiction_ids": audit_ids,
    }


def assess_knowledge_capability_eligibility(
    runtime_or_store: Any,
    *,
    knowledge_record: RecordEnvelope | str,
    runtime_scope: ScopeRef | Mapping[str, Any],
    source_trust: str = "unverified",
    review_state: str = "unreviewed",
    temporal_validity: Mapping[str, Any] | None = None,
    environment_constraints: Mapping[str, Any] | None = None,
    environment_context: Mapping[str, Any] | None = None,
) -> KnowledgeCapabilityAssessment:
    """Assess a record without making a capability write or maturity change.

    Canonical source verification is intentionally performed here, not merely
    at extraction time: an old claim cannot outlive a missing, replaced, or
    blocked canonical artifact.  Every ambiguous or unavailable dependency is
    treated as non-applicable.
    """

    store = _store_from(runtime_or_store)
    scope = _exact_scope(runtime_scope)
    record = _resolve_knowledge_record(store, knowledge_record, scope)
    normalized_trust = _allowed_or_default(source_trust, KNOWLEDGE_TRUST_LEVELS, "unverified")
    normalized_review = _allowed_or_default(review_state, KNOWLEDGE_REVIEW_STATES, "unreviewed")
    normalized_temporal, temporal_reasons = _normalize_temporal_validity(temporal_validity)
    normalized_constraints, environment_ok, environment_reasons = _environment_is_supported(
        environment_constraints,
        environment_context,
    )

    source_id, source_id_reason = _paper_source_id(record)
    source_record: RecordEnvelope | None = None
    reasons: list[str] = [*temporal_reasons, *environment_reasons]
    if source_id_reason:
        reasons.append(source_id_reason)
    if source_id:
        candidate = store.get_by_id(source_id, scope=scope)
        if candidate is None or candidate.kind != "paper_source" or not _same_scope(candidate.scope, scope):
            reasons.append("paper_source_not_found_exact_scope")
        else:
            source_record = candidate

    source_status = _effective_source_status(record, source_record)
    contradiction_state = _effective_contradiction_state(record, source_record)
    canonical_verified, artifact_digest, artifact_reason = _verify_canonical_artifact(store, source_record)
    # Expired or malformed temporal validity is evidence that the source can no
    # longer be safely applied.  Likewise, a missing/unverifiable canonical
    # artifact must not retain an ``active`` source label merely because an old
    # claim did.  Preserve a stronger immutable source status when one exists.
    if temporal_reasons:
        source_status = _more_restrictive_source_status(source_status, "stale")
    declared_artifact_status = _declared_artifact_source_status(source_record)
    if declared_artifact_status:
        source_status = _more_restrictive_source_status(source_status, declared_artifact_status)
    if not canonical_verified:
        source_status = _more_restrictive_source_status(source_status, "unverified")
    if artifact_reason:
        reasons.append(artifact_reason)
    if source_status in _HARD_BLOCKED_SOURCE_STATUSES:
        reasons.append(f"source_status_{source_status}")
    if contradiction_state == "contradicted":
        reasons.append("knowledge_contradicted")
    if normalized_review in {"unreviewed", "rejected"}:
        reasons.append(f"review_state_{normalized_review}")
    if normalized_trust in {"unverified", "low"}:
        reasons.append(f"source_trust_{normalized_trust}")
    if not canonical_verified:
        reasons.append("canonical_artifact_not_verified")
    if not environment_ok:
        reasons.append("environment_not_supported")

    # ``rejected`` remains a distinct immutable fact for audit/query use.  All
    # other unsafe evidence is blocked, and merely insufficient review/trust
    # remains a non-active candidate only.
    if source_status == "rejected" or normalized_review == "rejected":
        applicability = "rejected"
    elif (
        source_status in _HARD_BLOCKED_SOURCE_STATUSES
        or contradiction_state == "contradicted"
        or not canonical_verified
        or not environment_ok
        or bool(temporal_reasons)
    ):
        applicability = "blocked"
    elif source_status != "active" or normalized_review not in {"reviewed", "approved"} or normalized_trust not in {
        "medium",
        "high",
    }:
        applicability = "candidate"
    else:
        applicability = "applicable"

    evidence_refs = _dedupe_strings(
        [
            f"knowledge:{record.record_id}",
            *( [f"paper_source:{source_id}"] if source_id else [] ),
            *( [f"artifact:{artifact_digest}"] if artifact_digest else [] ),
        ]
    )
    return KnowledgeCapabilityAssessment(
        knowledge_record_id=record.record_id,
        paper_source_id=source_id,
        source_status=source_status,
        applicability=applicability,
        source_trust=normalized_trust,
        review_state=normalized_review,
        temporal_validity=normalized_temporal,
        environment_constraints=normalized_constraints,
        contradiction_state=contradiction_state,
        canonical_artifact_verified=canonical_verified,
        artifact_digest=artifact_digest,
        reasons=tuple(sorted(set(reasons))),
        evidence_refs=tuple(evidence_refs),
    )


def register_knowledge_capability_link(
    runtime_or_store: Any,
    *,
    knowledge_record_id: str,
    capability_id: str,
    capability_revision_id: str,
    capability_scope: str,
    runtime_scope: ScopeRef | Mapping[str, Any],
    relation_type: str = "supports",
    source_trust: str = "unverified",
    review_state: str = "unreviewed",
    temporal_validity: Mapping[str, Any] | None = None,
    environment_constraints: Mapping[str, Any] | None = None,
    environment_context: Mapping[str, Any] | None = None,
    request_key: str = "",
) -> KnowledgeCapabilityLinkResult:
    """Persist one immutable, exact-scope link after fail-closed verification."""

    store = _store_from(runtime_or_store)
    scope = _exact_scope(runtime_scope)
    try:
        normalized_capability_id = normalize_capability_id(capability_id)
        normalized_revision_id = normalize_opaque_id(capability_revision_id, field="capability_revision_id")
        normalized_capability_scope = normalize_opaque_id(capability_scope, field="capability_scope")
    except CapabilityContractError as exc:
        raise KnowledgeCapabilityBridgeError(str(exc)) from exc
    normalized_relation = str(relation_type or "").strip()
    if normalized_relation not in KNOWLEDGE_LINK_TYPES:
        raise KnowledgeCapabilityBridgeError("relation_type must be a supported knowledge link type")
    _resolve_active_revision(
        store,
        scope=scope,
        capability_id=normalized_capability_id,
        capability_revision_id=normalized_revision_id,
        capability_scope=normalized_capability_scope,
    )
    record = _resolve_knowledge_record(store, knowledge_record_id, scope)
    assessment = assess_knowledge_capability_eligibility(
        store,
        knowledge_record=record,
        runtime_scope=scope,
        source_trust=source_trust,
        review_state=review_state,
        temporal_validity=temporal_validity,
        environment_constraints=environment_constraints,
        environment_context=environment_context,
    )
    knowledge_digest = payload_digest(record.to_dict())
    created_at = _stable_record_timestamp(record)
    environment_context_digest = _environment_context_digest(environment_context)
    identity = {
        "schema_version": KNOWLEDGE_CAPABILITY_BRIDGE_SCHEMA,
        "runtime_scope": _scope_payload(scope),
        "capability_id": normalized_capability_id,
        "capability_revision_id": normalized_revision_id,
        "capability_scope": normalized_capability_scope,
        "knowledge_record_id": record.record_id,
        "knowledge_record_digest": knowledge_digest,
        "relation_type": normalized_relation,
        "assessment": assessment.to_dict(),
        "environment_context_digest": environment_context_digest,
    }
    link_id = "knowledge_link_" + sha256(_canonical_json(identity).encode("utf-8")).hexdigest()[:32]
    link = CapabilityKnowledgeLink(
        link_id=link_id,
        capability_id=normalized_capability_id,
        capability_revision_id=normalized_revision_id,
        knowledge_record_id=record.record_id,
        relation_type=normalized_relation,
        source_status=assessment.source_status,
        applicability=assessment.applicability,
        source_trust=assessment.source_trust,
        review_state=assessment.review_state,
        temporal_validity=assessment.temporal_validity,
        environment_constraints=assessment.environment_constraints,
        contradiction_state=assessment.contradiction_state,
        applicability_score=_applicability_score(assessment),
        applicability_evidence_refs=assessment.evidence_refs,
        evidence_refs=assessment.evidence_refs,
        created_at=created_at,
        scope=normalized_capability_scope,
        provenance={
            "schema_version": KNOWLEDGE_CAPABILITY_BRIDGE_SCHEMA,
            "knowledge_record_digest": knowledge_digest,
            "paper_source_id": assessment.paper_source_id,
            "artifact_digest": assessment.artifact_digest,
            "canonical_artifact_verified": assessment.canonical_artifact_verified,
            "assessment_reasons": list(assessment.reasons),
            "environment_context_digest": environment_context_digest,
        },
    )
    receipt = store.mutate_capabilities_atomically(
        lambda repository: repository.register_knowledge_link(
            link,
            scope=scope,
            knowledge_storage_key=_knowledge_storage_key(record),
            knowledge_record_digest=knowledge_digest,
            request_key=request_key or f"knowledge-link:{link.link_id}",
        )
    )
    return KnowledgeCapabilityLinkResult(link=link, receipt=receipt, assessment=assessment)


def list_registered_knowledge_links(
    runtime_or_store: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str,
    capability_id: str = "",
    capability_revision_id: str = "",
    knowledge_record_id: str = "",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Read immutable links through the Storage v2 exact-scope API only."""

    store = _store_from(runtime_or_store)
    scope = _exact_scope(runtime_scope)
    try:
        normalized_capability_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        normalized_capability_id = normalize_capability_id(capability_id) if capability_id else ""
        normalized_revision_id = (
            normalize_opaque_id(capability_revision_id, field="capability_revision_id")
            if capability_revision_id
            else ""
        )
        normalized_knowledge_record_id = (
            normalize_opaque_id(knowledge_record_id, field="knowledge_record_id") if knowledge_record_id else ""
        )
    except CapabilityContractError as exc:
        raise KnowledgeCapabilityBridgeError(str(exc)) from exc

    def reader(repository: Any) -> list[dict[str, Any]]:
        query = getattr(repository, "list_knowledge_links", None)
        if not callable(query):
            raise KnowledgeCapabilityBridgeError("capability knowledge-link query API is unavailable")
        rows = query(
            scope=scope,
            capability_scope=normalized_capability_scope,
            capability_id=normalized_capability_id,
            capability_revision_id=normalized_revision_id,
            knowledge_record_id=normalized_knowledge_record_id,
            limit=max(1, min(500, int(limit))),
        )
        return [dict(item) for item in rows if isinstance(item, Mapping)]

    return store.read_capabilities(reader)


def load_registered_knowledge_link(
    runtime_or_store: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str,
    link_id: str,
    link_digest: str = "",
) -> CapabilityKnowledgeLink:
    """Load and revalidate one immutable link, never a latest-scope fallback."""

    normalized_link_id = _normalize_id(link_id, "link_id")
    rows = list_registered_knowledge_links(
        runtime_or_store,
        runtime_scope=runtime_scope,
        capability_scope=capability_scope,
        limit=500,
    )
    for row in rows:
        if str(row.get("link_id") or "") != normalized_link_id:
            continue
        if link_digest and str(row.get("link_digest") or "") != link_digest:
            continue
        payload = row.get("payload")
        if not isinstance(payload, Mapping):
            raise KnowledgeCapabilityBridgeError("stored knowledge link payload is malformed")
        return _link_from_payload(payload)
    raise KnowledgeCapabilityBridgeError("knowledge link not found in exact scope")


def _store_from(runtime_or_store: Any) -> RuntimeStore:
    store = getattr(runtime_or_store, "store", runtime_or_store)
    required = ("get_by_id", "mutate_capabilities_atomically", "read_capabilities")
    if any(not callable(getattr(store, attribute, None)) for attribute in required):
        raise KnowledgeCapabilityBridgeError("runtime store does not expose the required bridge APIs")
    return store


def _exact_scope(value: ScopeRef | Mapping[str, Any]) -> ScopeRef:
    try:
        return exact_runtime_scope(value)
    except CapabilityRegistryError as exc:
        raise KnowledgeCapabilityBridgeError(str(exc)) from exc


def _resolve_knowledge_record(
    store: RuntimeStore,
    knowledge_record: RecordEnvelope | str,
    scope: ScopeRef,
) -> RecordEnvelope:
    if isinstance(knowledge_record, RecordEnvelope):
        if not _same_scope(knowledge_record.scope, scope):
            raise KnowledgeCapabilityBridgeError("knowledge record scope does not match the requested exact scope")
        return knowledge_record
    record_id = str(knowledge_record or "").strip()
    if not record_id:
        raise KnowledgeCapabilityBridgeError("knowledge_record_id is required")
    record = store.get_by_id(record_id, scope=scope)
    if record is None or not _same_scope(record.scope, scope):
        raise KnowledgeCapabilityBridgeError("knowledge record was not found in the requested exact scope")
    return record


def _resolve_active_revision(
    store: RuntimeStore,
    *,
    scope: ScopeRef,
    capability_id: str,
    capability_revision_id: str,
    capability_scope: str,
) -> None:
    try:
        resolution = CapabilityRegistry(store).resolve(
            capability_id,
            runtime_scope=scope,
            capability_scope=capability_scope,
            revision_id=capability_revision_id,
            limit=2,
        )
    except (CapabilityRegistryError, CapabilityContractError) as exc:
        raise KnowledgeCapabilityBridgeError(str(exc)) from exc
    matches = [
        item
        for item in resolution.revisions
        if str(item.get("entity_id") or "") == capability_revision_id and str(item.get("status") or "") == "active"
    ]
    if not resolution.ok or not matches:
        raise KnowledgeCapabilityBridgeError(
            f"capability revision is not active in exact scope: {resolution.reason or 'revision_not_found'}"
        )


def _paper_source_id(record: RecordEnvelope) -> tuple[str, str]:
    if record.kind == "paper_source":
        return record.record_id, ""
    candidates: list[str] = []
    for container in _record_mappings(record):
        values = container.get("source_ids")
        if isinstance(values, (list, tuple, set)):
            candidates.extend(str(item or "").strip() for item in values if str(item or "").strip())
        direct = container.get("paper_source_id")
        if str(direct or "").strip():
            candidates.append(str(direct).strip())
        metadata = container.get("metadata")
        if isinstance(metadata, Mapping):
            nested = metadata.get("paper_source_id")
            if str(nested or "").strip():
                candidates.append(str(nested).strip())
    unique = sorted(set(candidates))
    if not unique:
        return "", "paper_source_id_missing"
    if len(unique) != 1:
        return "", "paper_source_id_ambiguous"
    return unique[0], ""


def _effective_source_status(record: RecordEnvelope, source_record: RecordEnvelope | None) -> str:
    statuses: list[str] = []
    for candidate in (record, source_record):
        if candidate is None:
            continue
        raw = str(candidate.status or "").strip().lower()
        statuses.append(raw if raw in KNOWLEDGE_SOURCE_STATUSES else "unverified")
        marker = _refresh_marker(candidate)
        marker_status = str(marker.get("source_status") or "").strip().lower()
        if marker_status in KNOWLEDGE_SOURCE_STATUSES:
            statuses.append(marker_status)
        refresh = candidate.content.get("refresh") if isinstance(candidate.content, Mapping) else None
        refresh_state = str(refresh.get("state") or "").strip().lower() if isinstance(refresh, Mapping) else ""
        if refresh_state == "blocked":
            statuses.append("needs_refresh")
        elif refresh_state == "superseded":
            statuses.append("deprecated")
    if source_record is None:
        statuses.append("unverified")
    return max(statuses or ["unverified"], key=lambda item: _SOURCE_STATUS_PRIORITY.get(item, _SOURCE_STATUS_PRIORITY["unverified"]))


def _more_restrictive_source_status(left: str, right: str) -> str:
    """Return the status that fails closed when two source facts disagree."""

    return max(
        (left, right),
        key=lambda item: _SOURCE_STATUS_PRIORITY.get(item, _SOURCE_STATUS_PRIORITY["unverified"]),
    )


def _effective_contradiction_state(record: RecordEnvelope, source_record: RecordEnvelope | None) -> str:
    has_resolved_audit = False
    for candidate in (record, source_record):
        if candidate is None:
            continue
        for container in _record_mappings(candidate):
            for field in ("contradiction_ids", "contradiction_claim_ids"):
                values = container.get(field)
                if isinstance(values, (list, tuple, set)) and any(str(item or "").strip() for item in values):
                    return "contradicted"
        marker = _refresh_marker(candidate)
        if str(marker.get("contradiction_state") or "") == "contradicted":
            return "contradicted"
        if str(marker.get("contradiction_state") or "") == "resolved":
            has_resolved_audit = True
        refresh = candidate.content.get("refresh") if isinstance(candidate.content, Mapping) else None
        if isinstance(refresh, Mapping) and _has_ids(refresh.get("resolved_contradiction_ids")):
            has_resolved_audit = True
        for container in _record_mappings(candidate):
            if _has_ids(container.get("resolved_contradiction_ids")):
                has_resolved_audit = True
    return "resolved" if has_resolved_audit else "none"


def _verify_canonical_artifact(
    store: RuntimeStore,
    source_record: RecordEnvelope | None,
) -> tuple[bool, str, str]:
    if source_record is None:
        return False, "", "paper_source_not_found_exact_scope"
    payload = source_record.content if isinstance(source_record.content, Mapping) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    artifact = metadata.get("artifact") if isinstance(metadata.get("artifact"), Mapping) else payload.get("artifact")
    if not isinstance(artifact, Mapping):
        return False, "", "canonical_artifact_missing"
    artifact_payload = {str(key): value for key, value in artifact.items()}
    if str(artifact_payload.get("status") or "").strip().lower() != "ready":
        return False, "", "canonical_artifact_not_ready"
    pdf_blob_ref = str(payload.get("pdf_blob_ref") or "").strip()
    normalized_text_ref = str(payload.get("normalized_text_ref") or "").strip()
    if not pdf_blob_ref or not normalized_text_ref:
        return False, "", "canonical_artifact_reference_missing"
    artifact_digest = str(
        artifact_payload.get("text_sha256")
        or artifact_payload.get("manifest_sha256")
        or artifact_payload.get("pdf_sha256")
        or ""
    ).strip().lower()
    try:
        verifier = getattr(paper_artifacts, "load_verified_canonical_text", None)
        if not callable(verifier):
            return False, artifact_digest, "canonical_artifact_verifier_unavailable"
        result = verifier(
            store.root,
            pdf_blob_ref=pdf_blob_ref,
            normalized_text_ref=normalized_text_ref,
            artifact=artifact_payload,
        )
        text = str(result[0] if isinstance(result, tuple) else result or "")
        if not text:
            return False, artifact_digest, "canonical_artifact_invalid"
        return True, artifact_digest, ""
    except PaperArtifactError as exc:
        return False, artifact_digest, f"canonical_artifact_{exc.code}"
    except Exception as exc:  # defensive security boundary; do not consume bare text on verifier failures
        return False, artifact_digest, f"canonical_artifact_verification_failed_{type(exc).__name__}"


def _declared_artifact_source_status(source_record: RecordEnvelope | None) -> str:
    """Treat an explicit unsafe artifact lifecycle as a source lifecycle fact."""

    if source_record is None:
        return ""
    payload = source_record.content if isinstance(source_record.content, Mapping) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    artifact = metadata.get("artifact") if isinstance(metadata.get("artifact"), Mapping) else payload.get("artifact")
    if not isinstance(artifact, Mapping):
        return ""
    status = str(artifact.get("status") or "").strip().lower()
    if status in _HARD_BLOCKED_SOURCE_STATUSES:
        return status
    return ""


def _normalize_temporal_validity(value: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    raw = {"state": "current"} if value is None else value
    try:
        normalized = normalize_json_payload(raw, field="temporal_validity", reject_executable=True)
    except CapabilityContractError:
        return {"state": "invalid"}, ["temporal_validity_invalid"]
    reasons: list[str] = []
    now = datetime.now(timezone.utc)
    for field in ("valid_from", "valid_until", "expires_at"):
        raw_timestamp = normalized.get(field)
        if raw_timestamp in (None, ""):
            continue
        try:
            timestamp = _parse_utc_timestamp(str(raw_timestamp))
        except ValueError:
            reasons.append(f"temporal_{field}_invalid")
            continue
        if field == "valid_from" and timestamp > now:
            reasons.append("temporal_not_yet_valid")
        if field in {"valid_until", "expires_at"} and timestamp <= now:
            reasons.append("temporal_expired")
    return normalized or {"state": "current"}, reasons


def _environment_is_supported(
    constraints: Mapping[str, Any] | None,
    context: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], bool, list[str]]:
    raw_constraints = {"required": {}} if constraints is None else constraints
    try:
        normalized_constraints = normalize_json_payload(
            raw_constraints,
            field="environment_constraints",
            reject_executable=True,
        )
        normalized_context = normalize_json_payload(
            context or {},
            field="environment_context",
            reject_executable=True,
        )
    except CapabilityContractError:
        return {"required": {}}, False, ["environment_payload_invalid"]
    reasons: list[str] = []
    if normalized_context.get("supported") is not True:
        reasons.append("environment_unsupported")
    required = normalized_constraints.get("required", {})
    if not isinstance(required, Mapping):
        reasons.append("environment_constraints_invalid")
        return normalized_constraints or {"required": {}}, False, reasons
    for key, expected in required.items():
        if key not in normalized_context or normalized_context.get(key) != expected:
            reasons.append(f"environment_constraint_mismatch:{key}")
    return normalized_constraints or {"required": {}}, not reasons, reasons


def _applicability_score(assessment: KnowledgeCapabilityAssessment) -> float:
    if assessment.applicability != "applicable":
        return 0.0
    return 0.9 if assessment.source_trust == "high" and assessment.review_state == "approved" else 0.75


def _refresh_marker(record: RecordEnvelope) -> Mapping[str, Any]:
    content = record.content if isinstance(record.content, Mapping) else {}
    marker = content.get(KNOWLEDGE_CAPABILITY_MARKER_KEY)
    if not isinstance(marker, Mapping):
        marker = record.meta.get(KNOWLEDGE_CAPABILITY_MARKER_KEY) if isinstance(record.meta, Mapping) else None
    return marker if isinstance(marker, Mapping) else {}


def _record_mappings(record: RecordEnvelope) -> tuple[Mapping[str, Any], ...]:
    result: list[Mapping[str, Any]] = []
    for value in (record.content, record.meta, record.provenance):
        if isinstance(value, Mapping):
            result.append(value)
    return tuple(result)


def _stable_record_timestamp(record: RecordEnvelope) -> str:
    # Runtime record timestamps may retain their local RFC3339 offset.  Link
    # contracts require UTC, so normalize rather than rejecting otherwise valid
    # immutable records.  Prefer creation time to avoid a mutable refresh touch
    # changing the link's evidence timestamp.
    for value in (record.time.created_at, record.time.occurred_at, record.time.updated_at):
        try:
            return _parse_utc_timestamp(str(value)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        except (TypeError, ValueError):
            continue
    raise KnowledgeCapabilityBridgeError("knowledge record has no valid immutable timestamp")


def _knowledge_storage_key(record: RecordEnvelope) -> str:
    scope = record.scope
    return "\x1f".join(
        [str(scope.tenant_id), str(scope.agent_id), str(scope.workspace_id), str(scope.user_id), str(record.record_id)]
    )


def _environment_context_digest(value: Mapping[str, Any] | None) -> str:
    try:
        normalized = normalize_json_payload(value or {}, field="environment_context", reject_executable=True)
    except CapabilityContractError:
        return ""
    return payload_digest(normalized)


def _link_from_payload(value: Mapping[str, Any]) -> CapabilityKnowledgeLink:
    payload = {str(key): item for key, item in value.items()}
    payload.pop("link_digest", None)
    try:
        return CapabilityKnowledgeLink(**payload)
    except (CapabilityContractError, TypeError) as exc:
        raise KnowledgeCapabilityBridgeError("stored knowledge link violates the capability contract") from exc


def _allowed_or_default(value: object, allowed: frozenset[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def _same_scope(left: ScopeRef, right: ScopeRef) -> bool:
    return (
        left.tenant_id == right.tenant_id
        and left.agent_id == right.agent_id
        and left.workspace_id == right.workspace_id
        and left.user_id == right.user_id
    )


def _scope_payload(scope: ScopeRef) -> dict[str, str]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }


def _parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _normalize_id(value: object, field: str) -> str:
    try:
        return normalize_opaque_id(value, field=field)
    except CapabilityContractError as exc:
        raise KnowledgeCapabilityBridgeError(str(exc)) from exc


def _safe_string_list(values: list[str] | tuple[str, ...] | set[str], *, field: str) -> list[str]:
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and len(text) <= 256 and text not in result:
            result.append(text)
    if len(result) > 256:
        raise KnowledgeCapabilityBridgeError(f"{field} exceeds 256 items")
    return sorted(result)


def _has_ids(value: object) -> bool:
    return isinstance(value, (list, tuple, set)) and any(str(item or "").strip() for item in value)


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
