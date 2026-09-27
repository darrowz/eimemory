"""Separate, machine-gated capability hypotheses backed by knowledge links.

Knowledge links are evidence attribution only.  This module turns an explicit
link into a separate candidate hypothesis and accepts append-only experiment
feedback.  It never rewrites a source claim, emits capability observations, or
changes maturity.  A behavior gate opens only after an independent verifier
attests a linked evaluation, replay, or bounded candidate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from eimemory.capabilities.contracts import (
    CapabilityContractError,
    normalize_json_payload,
    normalize_opaque_id,
    normalize_sha256,
    normalize_text,
)
from eimemory.capabilities.models import CapabilityKnowledgeLink
from eimemory.capabilities.registry import CapabilityRegistry, CapabilityRegistryError
from eimemory.core.clock import now_iso
from eimemory.knowledge.capabilities import (
    KnowledgeCapabilityBridgeError,
    assess_knowledge_capability_eligibility,
    load_registered_knowledge_link,
)
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.jsonl import payload_digest


CAPABILITY_HYPOTHESIS_SCHEMA = "capability_hypothesis.v1"
CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA = "capability_hypothesis_feedback.v1"

__all__ = [
    "CAPABILITY_HYPOTHESIS_SCHEMA",
    "CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA",
    "CapabilityHypothesisError",
    "HypothesisGate",
    "create_capability_hypothesis",
    "explicit_hypothesis_reference",
    "hypothesis_behavior_gate",
    "list_capability_hypotheses",
    "record_hypothesis_evaluation_artifact",
    "record_hypothesis_experiment_feedback",
    "resolve_capability_hypothesis",
]

_ALLOWED_ARTIFACT_TYPES = frozenset({"evaluation", "replay", "bounded_candidate"})
_ARTIFACT_KINDS = {
    "evaluation": frozenset({"learning_eval", "evaluator_verdict"}),
    "replay": frozenset({"replay_result"}),
    "bounded_candidate": frozenset({"capability_candidate"}),
}
_PASS_VERDICTS = frozenset({"pass", "passed", "ok", "success"})
_FAIL_VERDICTS = frozenset({"fail", "failed", "blocked", "rejected", "invalid", "stale"})


class CapabilityHypothesisError(RuntimeError):
    """Raised when an explicit hypothesis chain cannot be proven safe."""


@dataclass(frozen=True, slots=True)
class HypothesisGate:
    """A non-promoting behavior decision for an explicit hypothesis."""

    hypothesis_id: str
    allowed: bool
    reason: str
    link_id: str = ""
    link_digest: str = ""
    capability_id: str = ""
    capability_revision_id: str = ""
    capability_scope: str = ""
    qualifying_feedback_id: str = ""
    assessment: dict[str, Any] | None = None
    expected_metric: dict[str, Any] | None = None
    candidate_bounds: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_HYPOTHESIS_SCHEMA,
            "hypothesis_id": self.hypothesis_id,
            "allowed": self.allowed,
            "reason": self.reason,
            "link_id": self.link_id,
            "link_digest": self.link_digest,
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "capability_scope": self.capability_scope,
            "qualifying_feedback_id": self.qualifying_feedback_id,
            "assessment": dict(self.assessment or {}),
            "expected_metric": dict(self.expected_metric or {}),
            "candidate_bounds": dict(self.candidate_bounds or {}),
        }


def explicit_hypothesis_reference(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Carry only an explicitly supplied hypothesis reference through planners.

    This intentionally does not derive a hypothesis from goal wording, source
    text, capability names, or accumulated knowledge volume.
    """

    if not isinstance(value, Mapping):
        return {}
    nested = value.get("capability_hypothesis")
    raw = nested if isinstance(nested, Mapping) else value
    raw_id = raw.get("hypothesis_id") or raw.get("capability_hypothesis_id")
    if not str(raw_id or "").strip():
        return {}
    result = {"capability_hypothesis_id": _opaque(raw_id, "capability_hypothesis_id")}
    # Extra identity fields are optional planner context.  The behavior gate
    # always reloads the authoritative hypothesis and link in exact scope.
    aliases = {
        "link_id": "link_id",
        "link_digest": "link_digest",
        "capability_id": "capability_id",
        "capability_revision_id": "capability_revision_id",
        "capability_scope": "capability_scope",
    }
    for key, target in aliases.items():
        if str(raw.get(key) or "").strip():
            result[target] = _opaque(raw[key], key)
    return result


def list_capability_hypotheses(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_id: str = "",
    capability_revision_id: str = "",
    capability_scope: str = "",
    status: str = "",
    limit: int = 100,
) -> list[RecordEnvelope]:
    """List hypothesis records through an exact runtime scope, bounded to 500.

    This is the WP13-facing discovery API.  It only returns durable hypothesis
    records; it does not infer missing attribution or decide behavior.
    """

    store = _store(runtime)
    scope = _exact_scope(runtime_scope)
    normalized_capability_id = _opaque(capability_id, "capability_id") if capability_id else ""
    normalized_revision_id = _opaque(capability_revision_id, "capability_revision_id") if capability_revision_id else ""
    normalized_capability_scope = _opaque(capability_scope, "capability_scope") if capability_scope else ""
    records = store.list_records(
        kinds=["capability_hypothesis"],
        scope=scope,
        status=status or None,
        limit=max(1, min(500, int(limit))),
    )
    result: list[RecordEnvelope] = []
    for record in records:
        if not _same_scope(record.scope, scope):
            continue
        content = record.content if isinstance(record.content, Mapping) else {}
        if normalized_capability_id and str(content.get("capability_id") or "") != normalized_capability_id:
            continue
        if normalized_revision_id and str(content.get("capability_revision_id") or "") != normalized_revision_id:
            continue
        if normalized_capability_scope and str(content.get("capability_scope") or "") != normalized_capability_scope:
            continue
        result.append(record)
    return result


def resolve_capability_hypothesis(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    hypothesis_id: str = "",
    capability_id: str = "",
    capability_revision_id: str = "",
    capability_scope: str = "",
) -> RecordEnvelope:
    """Resolve one hypothesis exactly, refusing an unqualified latest choice."""

    store = _store(runtime)
    scope = _exact_scope(runtime_scope)
    if hypothesis_id:
        record = _load_hypothesis(store, scope=scope, hypothesis_id=hypothesis_id)
        candidates = [record]
    else:
        candidates = list_capability_hypotheses(
            store,
            runtime_scope=scope,
            capability_id=capability_id,
            capability_revision_id=capability_revision_id,
            capability_scope=capability_scope,
            limit=2,
        )
    if len(candidates) != 1:
        raise CapabilityHypothesisError("hypothesis resolution requires exactly one exact-scope candidate")
    record = candidates[0]
    content = record.content if isinstance(record.content, Mapping) else {}
    checks = {
        "capability_id": capability_id,
        "capability_revision_id": capability_revision_id,
        "capability_scope": capability_scope,
    }
    for key, requested in checks.items():
        if requested and str(content.get(key) or "") != str(requested):
            raise CapabilityHypothesisError(f"resolved hypothesis does not match requested {key}")
    return record


def create_capability_hypothesis(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str,
    link_id: str,
    link_digest: str = "",
    statement: str,
    expected_metric: Mapping[str, Any],
    environment_context: Mapping[str, Any] | None,
    candidate_bounds: Mapping[str, Any] | None = None,
    allowed_artifact_types: Sequence[str] = ("evaluation", "replay", "bounded_candidate"),
    loop_id: str = "",
    request_key: str = "",
) -> RecordEnvelope:
    """Persist a candidate/blocked hypothesis which references link and revision.

    A non-applicable link is still represented as a blocked trace when it can
    be loaded safely.  It is never upgraded to active merely because the
    caller supplied more knowledge records.
    """

    store = _store(runtime)
    scope = _exact_scope(runtime_scope)
    normalized_capability_scope = _opaque(capability_scope, "capability_scope")
    normalized_link_id = _opaque(link_id, "link_id")
    normalized_link_digest = str(link_digest or "").strip()
    if normalized_link_digest:
        normalized_link_digest = _sha(normalized_link_digest, "link_digest")
    link = _load_link(
        store,
        scope=scope,
        capability_scope=normalized_capability_scope,
        link_id=normalized_link_id,
        link_digest=normalized_link_digest,
    )
    if link.scope != normalized_capability_scope:
        raise CapabilityHypothesisError("knowledge link capability scope does not match the requested scope")
    try:
        normalized_statement = normalize_text(statement, field="hypothesis.statement", max_chars=8_192)
        normalized_metric = normalize_json_payload(
            expected_metric,
            field="hypothesis.expected_metric",
            reject_executable=True,
        )
    except CapabilityContractError as exc:
        raise CapabilityHypothesisError(str(exc)) from exc
    if not normalized_metric:
        raise CapabilityHypothesisError("hypothesis.expected_metric must not be empty")
    normalized_bounds = _normalize_bounds(candidate_bounds)
    artifact_types = _artifact_types(allowed_artifact_types)
    normalized_environment, environment_error = _normalize_environment_context(environment_context)
    assessment, assessment_error = _current_link_assessment(
        store,
        scope=scope,
        link=link,
        environment_context=normalized_environment,
    )
    revision_error = _active_revision_error(store, scope=scope, link=link)
    blocked_reasons = _hypothesis_block_reasons(
        link=link,
        assessment=assessment,
        environment_error=environment_error,
        assessment_error=assessment_error,
        revision_error=revision_error,
    )
    record_status = "blocked" if blocked_reasons else "candidate"
    identity = {
        "schema_version": CAPABILITY_HYPOTHESIS_SCHEMA,
        "scope": _scope_payload(scope),
        "capability_scope": normalized_capability_scope,
        "link_id": link.link_id,
        "link_digest": link.link_digest,
        "capability_id": link.capability_id,
        "capability_revision_id": link.capability_revision_id,
        "statement": normalized_statement,
        "expected_metric": normalized_metric,
        "environment_context_digest": payload_digest(normalized_environment),
        "candidate_bounds": normalized_bounds,
        "allowed_artifact_types": artifact_types,
    }
    semantic_digest = payload_digest(identity)
    record_id = f"capability_hypothesis_{semantic_digest[:32]}"
    idempotency_key = request_key or f"capability-hypothesis:{semantic_digest}"
    existing = _find_exact_idempotent(
        store,
        scope=scope,
        kind="capability_hypothesis",
        idempotency_key=idempotency_key,
    )
    if existing is not None:
        return existing
    ts = now_iso()
    record = RecordEnvelope(
        record_id=record_id,
        kind="capability_hypothesis",
        status=record_status,
        title=f"Capability hypothesis: {link.capability_id}",
        summary=normalized_statement,
        detail="Knowledge-backed candidate; independent evaluation, replay, or bounded-candidate verification is required before behavior influence.",
        content={
            "schema_version": CAPABILITY_HYPOTHESIS_SCHEMA,
            "link_id": link.link_id,
            "link_digest": link.link_digest,
            "knowledge_record_id": link.knowledge_record_id,
            "capability_id": link.capability_id,
            "capability_revision_id": link.capability_revision_id,
            "capability_scope": normalized_capability_scope,
            "statement": normalized_statement,
            "expected_metric": normalized_metric,
            "candidate_bounds": normalized_bounds,
            "allowed_artifact_types": list(artifact_types),
            "environment_context": normalized_environment,
            "link_assessment": assessment,
            "blocked_reasons": blocked_reasons,
            "behavior_influence": {
                "allowed": False,
                "reason": "requires_independent_verified_eval_replay_or_bounded_candidate",
            },
        },
        tags=["capability", "hypothesis", "knowledge"],
        links=[
            LinkRef(relation="hypothesizes_from", target_kind="capability_knowledge_link", target_id=link.link_id),
            LinkRef(relation="targets", target_kind="capability_revision", target_id=link.capability_revision_id),
            LinkRef(relation="derived_from", target_kind="record", target_id=link.knowledge_record_id),
        ],
        evidence=[f"knowledge_link:{link.link_id}", *[str(item) for item in link.evidence_refs]],
        source="eimemory.governance.capability_hypotheses",
        scope=scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        provenance={
            "schema_version": CAPABILITY_HYPOTHESIS_SCHEMA,
            "link_digest": link.link_digest,
            "knowledge_link_source_status": link.source_status,
            "knowledge_link_applicability": link.applicability,
            "loop_id": str(loop_id or ""),
        },
        meta={
            "schema_version": CAPABILITY_HYPOTHESIS_SCHEMA,
            "idempotency_key": idempotency_key,
            "capability_id": link.capability_id,
            "capability_revision_id": link.capability_revision_id,
            "capability_scope": normalized_capability_scope,
            "link_id": link.link_id,
            "link_digest": link.link_digest,
            "loop_id": str(loop_id or ""),
            "behavior_influence_allowed": False,
        },
    )
    return store.append(record)


def record_hypothesis_evaluation_artifact(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    hypothesis_id: str,
    provider_binding_id: str,
    execution: Mapping[str, Any],
    probe_id: str,
    trace_record_id: str,
    verifier: Mapping[str, Any] | None = None,
    evaluation_run_id: str = "",
    evaluation_observation_id: str = "",
    request_key: str = "",
) -> RecordEnvelope:
    """Persist a derived evaluator artifact for one hypothesis.

    The artifact is deliberately *not* an independent authority by itself.
    It is a stable, queryable bridge from an independently persisted outcome
    trace to the exact hypothesis whose feedback it may later influence.  The
    supplied execution digest must equal the independently attested trace
    digest, so an evolution loop cannot attach a different result to a real
    verifier record after the fact.
    """

    store = _store(runtime)
    scope = _exact_scope(runtime_scope)
    hypothesis = _load_hypothesis(store, scope=scope, hypothesis_id=hypothesis_id)
    content = hypothesis.content if isinstance(hypothesis.content, Mapping) else {}
    normalized_binding_id = _opaque(provider_binding_id, "provider_binding_id")
    normalized_probe_id = _opaque(probe_id, "probe_id")
    normalized_trace_id = _opaque(trace_record_id, "trace_record_id")
    if normalized_probe_id == normalized_trace_id:
        raise CapabilityHypothesisError("probe_id and trace_record_id must be distinct")
    normalized_execution = _safe_payload(execution, field="hypothesis.evaluation_execution")
    execution_digest = str(normalized_execution.get("execution_digest") or "").strip()
    if not execution_digest:
        raise CapabilityHypothesisError("evaluation execution_digest is required")
    execution_digest = _sha(execution_digest, "evaluation.execution_digest")
    case_id = _opaque(normalized_execution.get("case_id"), "evaluation.case_id")
    case_digest = _sha(
        normalized_execution.get("evaluation_case_digest"),
        "evaluation.evaluation_case_digest",
    )
    normalized_verdict = _verdict(str(normalized_execution.get("verdict") or ""))
    if normalized_verdict == "blocked":
        raise CapabilityHypothesisError("evaluation verdict must be an explicit pass or failure")
    trace = _load_exact_record(store, scope=scope, record_id=normalized_trace_id)
    probe = _load_exact_record(store, scope=scope, record_id=normalized_probe_id)
    trace_verifier = _validate_independent_evaluation_trace(
        trace,
        probe_id=normalized_probe_id,
        capability_id=str(content.get("capability_id") or ""),
        capability_revision_id=str(content.get("capability_revision_id") or ""),
        provider_binding_id=normalized_binding_id,
        execution_digest=execution_digest,
    )
    supplied_verifier, supplied_verifier_errors = _normalize_verifier(verifier)
    if verifier is not None:
        if supplied_verifier_errors or not _same_verifier_identity(supplied_verifier, trace_verifier):
            raise CapabilityHypothesisError("supplied verifier does not match the independently persisted trace")
    if str(getattr(probe, "status", "") or "") != "active":
        raise CapabilityHypothesisError("independent probe record is not active")
    identity = {
        "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
        "artifact": "hypothesis_evaluation",
        "scope": _scope_payload(scope),
        "hypothesis_id": hypothesis.record_id,
        "provider_binding_id": normalized_binding_id,
        "probe_id": normalized_probe_id,
        "trace_record_id": normalized_trace_id,
        "execution_digest": execution_digest,
        "evaluation_case_digest": case_digest,
        "verdict": normalized_verdict,
    }
    semantic_digest = payload_digest(identity)
    idempotency_key = request_key or f"capability-hypothesis-evaluation:{semantic_digest}"
    existing = _find_exact_idempotent(store, scope=scope, kind="learning_eval", idempotency_key=idempotency_key)
    if existing is not None:
        if str(existing.meta.get("semantic_digest") or "") != semantic_digest:
            raise CapabilityHypothesisError("evaluation artifact idempotency key conflicts with a different payload")
        return existing
    hypothesis_context = {
        "hypothesis_id": hypothesis.record_id,
        "link_id": str(content.get("link_id") or ""),
        "link_digest": str(content.get("link_digest") or ""),
        "capability_id": str(content.get("capability_id") or ""),
        "capability_revision_id": str(content.get("capability_revision_id") or ""),
        "provider_binding_id": normalized_binding_id,
        "capability_scope": str(content.get("capability_scope") or ""),
    }
    ts = now_iso()
    record = RecordEnvelope(
        record_id=f"capability_hypothesis_evaluation_{semantic_digest[:32]}",
        kind="learning_eval",
        # The record is active when the evidence chain is valid.  Its verdict
        # is carried separately so a valid failure can restrict a hypothesis.
        status="active",
        title=f"Capability hypothesis evaluation: {hypothesis.record_id}",
        summary=f"{case_id}: {normalized_verdict}",
        detail="Derived evaluator result bound to an independently persisted trace; it is not a self-authorizing observation.",
        content={
            "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
            "report_type": "capability_hypothesis_evaluation",
            "verdict": normalized_verdict,
            "execution": normalized_execution,
            "execution_digest": execution_digest,
            "case_id": case_id,
            "evaluation_case_digest": case_digest,
            "provider_binding_id": normalized_binding_id,
            "probe_id": normalized_probe_id,
            "trace_record_id": normalized_trace_id,
            "evaluation_run_id": str(evaluation_run_id or ""),
            "evaluation_observation_id": str(evaluation_observation_id or ""),
            "verifier": {**trace_verifier, "independent": True},
            "capability_hypothesis": hypothesis_context,
        },
        tags=["capability", "hypothesis", "evaluation"],
        links=[
            LinkRef(relation="evaluates", target_kind="capability_hypothesis", target_id=hypothesis.record_id),
            LinkRef(relation="attested_by", target_kind=trace.kind, target_id=trace.record_id),
            LinkRef(relation="uses_probe", target_kind=probe.kind, target_id=probe.record_id),
        ],
        evidence=[
            f"hypothesis:{hypothesis.record_id}",
            f"probe:{normalized_probe_id}",
            f"trace:{normalized_trace_id}",
            f"execution:{execution_digest}",
        ],
        source="eimemory.governance.capability_hypotheses.evaluation_bridge",
        scope=scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        provenance={
            "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
            "derived_from_independent_trace": True,
            "trace_record_id": normalized_trace_id,
            "trace_verifier": trace_verifier,
            "execution_digest": execution_digest,
        },
        meta={
            "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
            "idempotency_key": idempotency_key,
            "semantic_digest": semantic_digest,
            "hypothesis_id": hypothesis.record_id,
            "capability_id": hypothesis_context["capability_id"],
            "capability_revision_id": hypothesis_context["capability_revision_id"],
            "provider_binding_id": normalized_binding_id,
            "verdict": normalized_verdict,
        },
    )
    return store.append(record)


def record_hypothesis_experiment_feedback(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    hypothesis_id: str,
    artifact_type: str,
    artifact_id: str,
    verdict: str,
    verifier: Mapping[str, Any] | None = None,
    details: Mapping[str, Any] | None = None,
    request_key: str = "",
) -> RecordEnvelope:
    """Append feedback; never mutate the knowledge claim or hypothesis itself."""

    store = _store(runtime)
    scope = _exact_scope(runtime_scope)
    hypothesis = _load_hypothesis(store, scope=scope, hypothesis_id=hypothesis_id)
    normalized_type = str(artifact_type or "").strip().lower()
    if normalized_type not in _ALLOWED_ARTIFACT_TYPES:
        raise CapabilityHypothesisError("artifact_type must be evaluation, replay, or bounded_candidate")
    allowed_types = {str(item) for item in hypothesis.content.get("allowed_artifact_types") or []}
    if normalized_type not in allowed_types:
        raise CapabilityHypothesisError("artifact type is not permitted by the hypothesis")
    artifact = _load_exact_record(store, scope=scope, record_id=artifact_id)
    normalized_verdict = _verdict(verdict)
    normalized_details = _safe_payload(details, field="feedback.details")
    artifact_valid, artifact_reasons = _validate_artifact(
        normalized_type,
        artifact,
        hypothesis,
        expected_verdict=normalized_verdict,
    )
    verifier_payload, verifier_reasons = _normalize_verifier(verifier)
    qualified = bool(
        artifact_valid
        and normalized_verdict == "pass"
        and verifier_payload.get("independent") is True
        and not verifier_reasons
    )
    if qualified:
        feedback_effect = "eligible"
    elif (
        artifact_valid
        and normalized_verdict != "pass"
        and verifier_payload.get("independent") is True
        and not verifier_reasons
    ):
        feedback_effect = "restrictive"
    else:
        feedback_effect = "trace_only"
    feedback_status = "active" if artifact_valid else "blocked"
    identity = {
        "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
        "scope": _scope_payload(scope),
        "hypothesis_id": hypothesis.record_id,
        "artifact_type": normalized_type,
        "artifact_id": artifact.record_id,
        "artifact_digest": payload_digest(artifact.to_dict()),
        "verdict": normalized_verdict,
        "verifier": verifier_payload,
        "details": normalized_details,
    }
    semantic_digest = payload_digest(identity)
    idempotency_key = request_key or f"capability-hypothesis-feedback:{semantic_digest}"
    existing = _find_exact_idempotent(store, scope=scope, kind="feedback", idempotency_key=idempotency_key)
    if existing is not None:
        return existing
    link_id = str(hypothesis.content.get("link_id") or "")
    link_digest = str(hypothesis.content.get("link_digest") or "")
    revision_id = str(hypothesis.content.get("capability_revision_id") or "")
    # Feedback ordering is behavior-relevant: a later independent failure must
    # be able to narrow an earlier pass.  The shared clock intentionally has
    # second precision, so use a local immutable microsecond timestamp here.
    ts = _next_feedback_timestamp(store, scope=scope, hypothesis_id=hypothesis.record_id)
    record = RecordEnvelope(
        record_id=f"capability_hypothesis_feedback_{semantic_digest[:32]}",
        kind="feedback",
        status=feedback_status,
        title=f"Capability hypothesis feedback: {hypothesis.record_id}",
        summary=f"{normalized_type} {normalized_verdict}",
        detail="Append-only feedback for a knowledge-backed capability hypothesis.",
        content={
            "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
            "hypothesis_id": hypothesis.record_id,
            "link_id": link_id,
            "link_digest": link_digest,
            "capability_id": str(hypothesis.content.get("capability_id") or ""),
            "capability_revision_id": revision_id,
            "capability_scope": str(hypothesis.content.get("capability_scope") or ""),
            "artifact_type": normalized_type,
            "artifact_id": artifact.record_id,
            "artifact_digest": payload_digest(artifact.to_dict()),
            "verdict": normalized_verdict,
            "verifier": verifier_payload,
            "details": normalized_details,
            "artifact_valid": artifact_valid,
            "blocked_reasons": sorted(set([*artifact_reasons, *verifier_reasons])),
            "qualifies_behavior": qualified,
            "applicability_feedback": {
                "effect": feedback_effect,
                "immutable": True,
                "requires_newer_independent_pass": feedback_effect == "restrictive",
            },
        },
        tags=["capability", "hypothesis", "feedback", normalized_type],
        links=[
            LinkRef(relation="feedback_for", target_kind="capability_hypothesis", target_id=hypothesis.record_id),
            LinkRef(relation="evaluates", target_kind=artifact.kind, target_id=artifact.record_id),
        ],
        evidence=[f"hypothesis:{hypothesis.record_id}", f"artifact:{artifact.record_id}", f"link:{link_id}"],
        source="eimemory.governance.capability_hypotheses",
        scope=scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        provenance={
            "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
            "source_claims_mutated": False,
            "hypothesis_mutated": False,
        },
        meta={
            "schema_version": CAPABILITY_HYPOTHESIS_FEEDBACK_SCHEMA,
            "idempotency_key": idempotency_key,
            "hypothesis_id": hypothesis.record_id,
            "link_id": link_id,
            "link_digest": link_digest,
            "capability_revision_id": revision_id,
            "artifact_type": normalized_type,
            "qualifies_behavior": qualified,
            "applicability_feedback_effect": feedback_effect,
        },
    )
    return store.append(record)


def hypothesis_behavior_gate(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    hypothesis_id: str,
) -> dict[str, Any]:
    """Return whether a hypothesis may influence behavior, without mutation."""

    store = _store(runtime)
    scope = _exact_scope(runtime_scope)
    try:
        hypothesis = _load_hypothesis(store, scope=scope, hypothesis_id=hypothesis_id)
    except CapabilityHypothesisError as exc:
        return HypothesisGate(hypothesis_id=str(hypothesis_id or ""), allowed=False, reason=str(exc)).to_dict()
    content = hypothesis.content if isinstance(hypothesis.content, Mapping) else {}
    link_id = str(content.get("link_id") or "")
    link_digest = str(content.get("link_digest") or "")
    capability_scope = str(content.get("capability_scope") or "")
    try:
        link = _load_link(
            store,
            scope=scope,
            capability_scope=capability_scope,
            link_id=link_id,
            link_digest=link_digest,
        )
    except CapabilityHypothesisError as exc:
        return _gate_from_content(content, hypothesis.record_id, reason=str(exc)).to_dict()
    revision_error = _active_revision_error(store, scope=scope, link=link)
    if revision_error:
        return _gate_from_content(content, hypothesis.record_id, reason=revision_error).to_dict()
    environment_context = content.get("environment_context") if isinstance(content.get("environment_context"), Mapping) else {}
    assessment, assessment_error = _current_link_assessment(
        store,
        scope=scope,
        link=link,
        environment_context=environment_context,
    )
    if hypothesis.status != "candidate":
        return _gate_from_content(content, hypothesis.record_id, reason="hypothesis_not_candidate", assessment=assessment).to_dict()
    if assessment_error or not assessment or assessment.get("applicability") != "applicable":
        return _gate_from_content(
            content,
            hypothesis.record_id,
            reason=assessment_error or "knowledge_link_not_currently_applicable",
            assessment=assessment,
        ).to_dict()
    allowed_types = {str(item) for item in content.get("allowed_artifact_types") or []}
    feedback_gate = _latest_independent_feedback_gate(
        store,
        scope=scope,
        hypothesis=hypothesis,
        link=link,
        allowed_types=allowed_types,
        assessment=assessment,
    )
    if feedback_gate is not None:
        return feedback_gate.to_dict()
    return _gate_from_content(
        content,
        hypothesis.record_id,
        reason="requires_independent_verified_eval_replay_or_bounded_candidate",
        assessment=assessment,
    ).to_dict()


def _store(runtime: Any) -> Any:
    store = getattr(runtime, "store", runtime)
    required = ("get_by_id", "list_records", "append")
    if any(not callable(getattr(store, attribute, None)) for attribute in required):
        raise CapabilityHypothesisError("runtime store does not expose the required hypothesis APIs")
    return store


def _exact_scope(value: ScopeRef | Mapping[str, Any]) -> ScopeRef:
    if isinstance(value, ScopeRef):
        if not str(value.tenant_id or "").strip():
            raise CapabilityHypothesisError("runtime scope tenant_id is required")
        return value
    if not isinstance(value, Mapping):
        raise CapabilityHypothesisError("runtime_scope must be an exact scope mapping")
    required = {"tenant_id", "agent_id", "workspace_id", "user_id"}
    if set(value) != required or any(not isinstance(value[key], str) for key in required):
        raise CapabilityHypothesisError("runtime_scope requires exact tenant/agent/workspace/user strings")
    if not str(value["tenant_id"] or "").strip():
        raise CapabilityHypothesisError("runtime_scope.tenant_id is required")
    return ScopeRef(
        tenant_id=str(value["tenant_id"]),
        agent_id=str(value["agent_id"]),
        workspace_id=str(value["workspace_id"]),
        user_id=str(value["user_id"]),
    )


def _load_link(
    store: Any,
    *,
    scope: ScopeRef,
    capability_scope: str,
    link_id: str,
    link_digest: str,
) -> CapabilityKnowledgeLink:
    try:
        return load_registered_knowledge_link(
            store,
            runtime_scope=scope,
            capability_scope=capability_scope,
            link_id=link_id,
            link_digest=link_digest,
        )
    except KnowledgeCapabilityBridgeError as exc:
        raise CapabilityHypothesisError(str(exc)) from exc


def _current_link_assessment(
    store: Any,
    *,
    scope: ScopeRef,
    link: CapabilityKnowledgeLink,
    environment_context: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], str]:
    try:
        assessment = assess_knowledge_capability_eligibility(
            store,
            knowledge_record=link.knowledge_record_id,
            runtime_scope=scope,
            source_trust=link.source_trust,
            review_state=link.review_state,
            temporal_validity=link.temporal_validity,
            environment_constraints=link.environment_constraints,
            environment_context=environment_context,
        )
        return assessment.to_dict(), ""
    except KnowledgeCapabilityBridgeError as exc:
        return {}, str(exc)


def _hypothesis_block_reasons(
    *,
    link: CapabilityKnowledgeLink,
    assessment: dict[str, Any],
    environment_error: str,
    assessment_error: str,
    revision_error: str,
) -> list[str]:
    reasons: list[str] = []
    if link.applicability != "applicable":
        reasons.append("knowledge_link_not_applicable")
    if link.source_status != "active":
        reasons.append(f"knowledge_link_source_{link.source_status}")
    if link.review_state not in {"reviewed", "approved"}:
        reasons.append(f"knowledge_link_review_{link.review_state}")
    if link.source_trust not in {"medium", "high"}:
        reasons.append(f"knowledge_link_trust_{link.source_trust}")
    if link.contradiction_state == "contradicted":
        reasons.append("knowledge_link_contradicted")
    if environment_error:
        reasons.append(environment_error)
    if revision_error:
        reasons.append(revision_error)
    if assessment_error:
        reasons.append(assessment_error)
    elif assessment.get("applicability") != "applicable":
        reasons.append("knowledge_link_not_currently_applicable")
    return sorted(set(reasons))


def _active_revision_error(store: Any, *, scope: ScopeRef, link: CapabilityKnowledgeLink) -> str:
    """Refuse a link whose exact capability revision is no longer active."""

    try:
        resolution = CapabilityRegistry(store).resolve(
            link.capability_id,
            runtime_scope=scope,
            capability_scope=link.scope,
            revision_id=link.capability_revision_id,
            limit=2,
        )
    except (CapabilityRegistryError, CapabilityContractError):
        return "capability_revision_resolution_failed"
    matches = [
        item
        for item in resolution.revisions
        if str(item.get("entity_id") or "") == link.capability_revision_id and str(item.get("status") or "") == "active"
    ]
    if not resolution.ok or len(matches) != 1:
        return "capability_revision_not_active"
    return ""


def _normalize_environment_context(value: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
    try:
        normalized = normalize_json_payload(value or {}, field="hypothesis.environment_context", reject_executable=True)
    except CapabilityContractError:
        return {}, "hypothesis_environment_context_invalid"
    if normalized.get("supported") is not True:
        return normalized, "hypothesis_environment_unsupported"
    return normalized, ""


def _normalize_bounds(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = value if value is not None else {"side_effect_class": "none", "max_changes": 0}
    try:
        normalized = normalize_json_payload(raw, field="hypothesis.candidate_bounds", reject_executable=True)
    except CapabilityContractError as exc:
        raise CapabilityHypothesisError(str(exc)) from exc
    if not normalized:
        raise CapabilityHypothesisError("hypothesis.candidate_bounds must not be empty")
    return normalized


def _artifact_types(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise CapabilityHypothesisError("allowed_artifact_types must be a sequence")
    normalized = tuple(sorted({str(item or "").strip().lower() for item in values if str(item or "").strip()}))
    if not normalized or not set(normalized).issubset(_ALLOWED_ARTIFACT_TYPES):
        raise CapabilityHypothesisError("allowed_artifact_types contains an unsupported artifact type")
    return normalized


def _load_hypothesis(store: Any, *, scope: ScopeRef, hypothesis_id: str) -> RecordEnvelope:
    record = _load_exact_record(store, scope=scope, record_id=hypothesis_id)
    if record.kind != "capability_hypothesis":
        raise CapabilityHypothesisError("record is not a capability hypothesis")
    return record


def _load_exact_record(store: Any, *, scope: ScopeRef, record_id: str) -> RecordEnvelope:
    normalized_id = str(record_id or "").strip()
    if not normalized_id:
        raise CapabilityHypothesisError("record_id is required")
    record = store.get_by_id(normalized_id, scope=scope)
    if record is None or not _same_scope(record.scope, scope):
        raise CapabilityHypothesisError("record was not found in the requested exact scope")
    return record


def _validate_artifact(
    artifact_type: str,
    artifact: RecordEnvelope,
    hypothesis: RecordEnvelope,
    *,
    expected_verdict: str,
) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if artifact.kind not in _ARTIFACT_KINDS[artifact_type]:
        reasons.append(f"artifact_kind_invalid:{artifact.kind}")
    artifact_status = str(artifact.status or "").strip().lower()
    # A failed or blocked evaluation is still useful negative evidence.  It
    # must remain append-only and be allowed to narrow a hypothesis.  Such an
    # artifact can never support a passing behavior decision, though.
    if artifact_status in {"rejected", "deprecated", "invalid", "stale"}:
        reasons.append(f"artifact_status_invalid:{artifact_status}")
    if expected_verdict == "pass" and artifact_status in {"blocked", "failed"}:
        reasons.append(f"artifact_status_not_passing:{artifact_status}")
    payload = artifact.content if isinstance(artifact.content, Mapping) else {}
    reported_verdict = str(payload.get("verdict") or artifact.meta.get("verdict") or "").strip().lower()
    if expected_verdict == "pass" and reported_verdict and reported_verdict not in _PASS_VERDICTS:
        reasons.append("artifact_verdict_not_pass")
    if artifact_type == "bounded_candidate":
        patch = payload.get("candidate_patch") if isinstance(payload.get("candidate_patch"), Mapping) else {}
        bounds = payload.get("candidate_bounds") if isinstance(payload.get("candidate_bounds"), Mapping) else patch.get("candidate_bounds")
        replay_case_ids = payload.get("replay_case_ids") if isinstance(payload.get("replay_case_ids"), (list, tuple)) else patch.get("replay_case_ids")
        if not isinstance(bounds, Mapping) or not bounds:
            reasons.append("bounded_candidate_bounds_missing")
        if not isinstance(replay_case_ids, (list, tuple)) or not any(str(item or "").strip() for item in replay_case_ids):
            reasons.append("bounded_candidate_replay_missing")
    # An artifact must carry its own explicit bridge context.  The feedback
    # record is written *after* this check and therefore cannot manufacture the
    # missing capability provenance for an otherwise unrelated evaluator or
    # replay result.  This prevents a later caller from attaching a real but
    # unrelated passing artifact to a knowledge hypothesis merely by naming its
    # ID in feedback.
    artifact_context = artifact.content.get("capability_hypothesis") if isinstance(artifact.content, Mapping) else None
    if not isinstance(artifact_context, Mapping) and isinstance(payload.get("candidate_patch"), Mapping):
        artifact_context = payload["candidate_patch"].get("capability_hypothesis")
    if not isinstance(artifact_context, Mapping):
        reasons.append("artifact_capability_provenance_missing")
        return not reasons, reasons
    artifact_hypothesis_id = str(
        artifact_context.get("hypothesis_id") or artifact_context.get("capability_hypothesis_id") or ""
    )
    if artifact_hypothesis_id != hypothesis.record_id:
        reasons.append("artifact_hypothesis_mismatch")
    if str(artifact_context.get("link_id") or "") != str(hypothesis.content.get("link_id") or ""):
        reasons.append("artifact_link_mismatch")
    if str(artifact_context.get("link_digest") or "") != str(hypothesis.content.get("link_digest") or ""):
        reasons.append("artifact_link_digest_mismatch")
    if str(artifact_context.get("capability_id") or "") != str(hypothesis.content.get("capability_id") or ""):
        reasons.append("artifact_capability_mismatch")
    if str(artifact_context.get("capability_revision_id") or "") != str(hypothesis.content.get("capability_revision_id") or ""):
        reasons.append("artifact_revision_mismatch")
    if str(artifact_context.get("capability_scope") or "") != str(hypothesis.content.get("capability_scope") or ""):
        reasons.append("artifact_capability_scope_mismatch")
    return not reasons, reasons


def _validate_independent_evaluation_trace(
    trace: RecordEnvelope,
    *,
    probe_id: str,
    capability_id: str,
    capability_revision_id: str,
    provider_binding_id: str,
    execution_digest: str,
) -> dict[str, Any]:
    """Load the independent authority rather than trusting a caller payload."""

    if str(trace.status or "") != "active":
        raise CapabilityHypothesisError("independent trace record is not active")
    if str(trace.source or "") == "eimemory.evaluation.capability_catalog":
        raise CapabilityHypothesisError("catalog-generated trace cannot independently attest its own result")
    content = trace.content if isinstance(trace.content, Mapping) else {}
    payload = content.get("payload") if isinstance(content.get("payload"), Mapping) else content
    if str(payload.get("capability") or "") != capability_id:
        raise CapabilityHypothesisError("independent trace capability does not match hypothesis")
    if str(payload.get("capability_revision_id") or "") != capability_revision_id:
        raise CapabilityHypothesisError("independent trace revision does not match hypothesis")
    if str(payload.get("provider_binding_id") or "") != provider_binding_id:
        raise CapabilityHypothesisError("independent trace binding does not match hypothesis")
    verifier = payload.get("verifier") if isinstance(payload.get("verifier"), Mapping) else {}
    normalized, reasons = _normalize_verifier(verifier)
    if reasons:
        raise CapabilityHypothesisError("independent trace verifier is incomplete or invalid")
    if str(normalized.get("evidence_ref") or "") != probe_id:
        raise CapabilityHypothesisError("independent trace verifier does not bind the supplied probe")
    contract = payload.get("capability_contract") if isinstance(payload.get("capability_contract"), Mapping) else {}
    source_ids = contract.get("source_record_ids")
    if not isinstance(source_ids, Sequence) or isinstance(source_ids, (str, bytes)) or list(source_ids) != [probe_id]:
        raise CapabilityHypothesisError("independent trace contract does not bind exactly one supplied probe")
    if str(normalized.get("contract_digest") or "") != execution_digest:
        raise CapabilityHypothesisError("independent trace digest does not match evaluated execution")
    return {
        "id": str(normalized.get("id") or ""),
        "revision": str(normalized.get("revision") or ""),
        "contract_digest": str(normalized.get("contract_digest") or ""),
    }


def _same_verifier_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(
        str(left.get(key) or "").strip() == str(right.get(key) or "").strip()
        for key in ("id", "revision", "contract_digest")
    ) and left.get("independent") is True


def _normalize_verifier(value: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    try:
        normalized = normalize_json_payload(value or {}, field="feedback.verifier", reject_executable=True)
    except CapabilityContractError:
        return {}, ["verifier_payload_invalid"]
    reasons: list[str] = []
    if normalized.get("independent") is not True:
        reasons.append("verifier_not_independent")
    for key in ("id", "revision", "contract_digest"):
        if not str(normalized.get(key) or "").strip():
            reasons.append(f"verifier_{key}_missing")
    if str(normalized.get("contract_digest") or "").strip():
        try:
            normalized["contract_digest"] = _sha(normalized["contract_digest"], "verifier.contract_digest")
        except CapabilityHypothesisError:
            reasons.append("verifier_contract_digest_invalid")
    return normalized, reasons


def _verdict(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _PASS_VERDICTS:
        return "pass"
    if normalized in _FAIL_VERDICTS:
        return "fail" if normalized not in {"blocked", "stale", "invalid"} else normalized
    return "blocked"


def _safe_payload(value: Mapping[str, Any] | None, *, field: str) -> dict[str, Any]:
    try:
        return normalize_json_payload(value or {}, field=field, reject_executable=True)
    except CapabilityContractError as exc:
        raise CapabilityHypothesisError(str(exc)) from exc


def _exact_feedback_records(store: Any, *, scope: ScopeRef, hypothesis_id: str) -> list[RecordEnvelope]:
    # A bounded scan is intentional.  There is no fallback to a different user
    # scope, and the stable link/revision fields are checked again by the gate.
    records = store.list_records(kinds=["feedback"], scope=scope, limit=500)
    return [
        record
        for record in records
        if _same_scope(record.scope, scope)
        and isinstance(record.content, Mapping)
        and str(record.content.get("hypothesis_id") or "") == hypothesis_id
    ]


def _latest_independent_feedback_gate(
    store: Any,
    *,
    scope: ScopeRef,
    hypothesis: RecordEnvelope,
    link: CapabilityKnowledgeLink,
    allowed_types: set[str],
    assessment: Mapping[str, Any],
) -> HypothesisGate | None:
    """Use the newest independently verified feedback as a restrictive gate.

    Feedback is immutable and additive.  A newer independent failure therefore
    closes a previously opened path until a later independent success is
    recorded, rather than allowing an old pass to outlive negative evidence.
    Non-independent loop feedback remains a trace only and cannot either open
    or close the behavior path by itself.
    """

    records = sorted(
        _exact_feedback_records(store, scope=scope, hypothesis_id=hypothesis.record_id),
        key=_feedback_sort_key,
        reverse=True,
    )
    for feedback in records:
        payload = feedback.content if isinstance(feedback.content, Mapping) else {}
        verifier = payload.get("verifier") if isinstance(payload.get("verifier"), Mapping) else {}
        if verifier.get("independent") is not True:
            continue
        if not _canonical_feedback_timestamp(feedback):
            return _gate_from_content(
                hypothesis.content,
                hypothesis.record_id,
                reason="independent_feedback_timestamp_invalid",
                assessment=assessment,
            )
        trace_error = _feedback_trace_error(
            payload,
            hypothesis=hypothesis,
            link=link,
            allowed_types=allowed_types,
        )
        if trace_error:
            return _gate_from_content(
                hypothesis.content,
                hypothesis.record_id,
                reason=trace_error,
                assessment=assessment,
            )
        _verifier_payload, verifier_reasons = _normalize_verifier(verifier)
        if verifier_reasons:
            return _gate_from_content(
                hypothesis.content,
                hypothesis.record_id,
                reason="independent_feedback_verifier_invalid",
                assessment=assessment,
            )
        try:
            artifact = _load_exact_record(
                store,
                scope=scope,
                record_id=str(payload.get("artifact_id") or ""),
            )
        except CapabilityHypothesisError:
            return _gate_from_content(
                hypothesis.content,
                hypothesis.record_id,
                reason="independent_feedback_artifact_not_found",
                assessment=assessment,
            )
        expected_verdict = _verdict(str(payload.get("verdict") or ""))
        artifact_valid, _artifact_reasons = _validate_artifact(
            str(payload.get("artifact_type") or ""),
            artifact,
            hypothesis,
            expected_verdict=expected_verdict,
        )
        if (
            feedback.status != "active"
            or not artifact_valid
            or str(payload.get("artifact_digest") or "") != payload_digest(artifact.to_dict())
        ):
            return _gate_from_content(
                hypothesis.content,
                hypothesis.record_id,
                reason="independent_feedback_artifact_invalid",
                assessment=assessment,
            )
        if expected_verdict != "pass" or payload.get("qualifies_behavior") is not True:
            return _gate_from_content(
                hypothesis.content,
                hypothesis.record_id,
                reason="latest_independent_feedback_not_pass",
                assessment=assessment,
            )
        return HypothesisGate(
            hypothesis_id=hypothesis.record_id,
            allowed=True,
            reason="independent_verified_hypothesis_feedback",
            link_id=link.link_id,
            link_digest=link.link_digest,
            capability_id=link.capability_id,
            capability_revision_id=link.capability_revision_id,
            capability_scope=link.scope,
            qualifying_feedback_id=feedback.record_id,
            assessment=dict(assessment),
            expected_metric=_mapping_payload(hypothesis.content.get("expected_metric")),
            candidate_bounds=_mapping_payload(hypothesis.content.get("candidate_bounds")),
        )
    return None


def _feedback_trace_error(
    payload: Mapping[str, Any],
    *,
    hypothesis: RecordEnvelope,
    link: CapabilityKnowledgeLink,
    allowed_types: set[str],
) -> str:
    """Verify that an independently attested feedback record still binds this trace."""

    expected = {
        "hypothesis_id": hypothesis.record_id,
        "link_id": link.link_id,
        "link_digest": link.link_digest,
        "capability_id": link.capability_id,
        "capability_revision_id": link.capability_revision_id,
        "capability_scope": str(hypothesis.content.get("capability_scope") or ""),
    }
    for field, value in expected.items():
        if str(payload.get(field) or "") != value:
            return f"independent_feedback_{field}_mismatch"
    artifact_type = str(payload.get("artifact_type") or "")
    if artifact_type not in allowed_types:
        return "independent_feedback_artifact_type_invalid"
    return ""


def _feedback_sort_key(record: RecordEnvelope) -> tuple[str, str]:
    timestamp = _canonical_feedback_timestamp(record)
    # An independently attested record with malformed time must be considered
    # restrictive rather than letting an older pass silently win the ordering.
    return (timestamp or "9999-12-31T23:59:59.999999Z", record.record_id)


def _next_feedback_timestamp(store: Any, *, scope: ScopeRef, hypothesis_id: str) -> str:
    """Allocate a durable monotonic feedback time within one hypothesis trace."""

    current = datetime.now(timezone.utc)
    previous = [
        timestamp
        for timestamp in (
            _canonical_feedback_timestamp(record)
            for record in _exact_feedback_records(store, scope=scope, hypothesis_id=hypothesis_id)
        )
        if timestamp
    ]
    if previous:
        latest = datetime.fromisoformat(max(previous).replace("Z", "+00:00"))
        if latest >= current:
            current = latest + timedelta(microseconds=1)
    return current.isoformat(timespec="microseconds")


def _canonical_feedback_timestamp(record: RecordEnvelope) -> str:
    raw = str(record.time.created_at or record.time.updated_at or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _find_exact_idempotent(
    store: Any,
    *,
    scope: ScopeRef,
    kind: str,
    idempotency_key: str,
) -> RecordEnvelope | None:
    records = store.list_records(kinds=[kind], scope=scope, limit=500)
    for record in records:
        if _same_scope(record.scope, scope) and str(record.meta.get("idempotency_key") or "") == idempotency_key:
            return record
    return None


def _gate_from_content(
    content: Mapping[str, Any],
    hypothesis_id: str,
    *,
    reason: str,
    assessment: Mapping[str, Any] | None = None,
) -> HypothesisGate:
    return HypothesisGate(
        hypothesis_id=hypothesis_id,
        allowed=False,
        reason=reason,
        link_id=str(content.get("link_id") or ""),
        link_digest=str(content.get("link_digest") or ""),
        capability_id=str(content.get("capability_id") or ""),
        capability_revision_id=str(content.get("capability_revision_id") or ""),
        capability_scope=str(content.get("capability_scope") or ""),
        assessment=dict(assessment or {}),
        expected_metric=_mapping_payload(content.get("expected_metric")),
        candidate_bounds=_mapping_payload(content.get("candidate_bounds")),
    )


def _opaque(value: object, field: str) -> str:
    try:
        return normalize_opaque_id(value, field=field)
    except CapabilityContractError as exc:
        raise CapabilityHypothesisError(str(exc)) from exc


def _sha(value: object, field: str) -> str:
    try:
        return normalize_sha256(value, field=field)
    except CapabilityContractError as exc:
        raise CapabilityHypothesisError(str(exc)) from exc


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


def _mapping_payload(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
