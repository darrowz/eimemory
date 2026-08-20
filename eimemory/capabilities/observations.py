"""One fail-closed boundary for dynamic capability observations.

Raw outcomes, evaluator results, and adapter lifecycle events remain in their
native streams.  This module only adds an immutable v3 observation when the
caller supplies an explicit capability revision, provider binding, verdict, and
independent evidence context.  It deliberately never infers a capability from
text, a host name, a package version, or a legacy score.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any

from eimemory.capabilities.contracts import normalize_opaque_id, normalize_sha256, require_timestamp
from eimemory.capabilities.models import CapabilityObservation, EvaluationRun
from eimemory.capabilities.registry import MutationReceipt, exact_runtime_scope
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


class CapabilityObservationError(ValueError):
    """An outcome cannot be safely normalized into an L5 observation."""


@dataclass(frozen=True, slots=True)
class ObservationNormalizationResult:
    """A bounded result that makes skipped/unclassified input explicit."""

    status: str
    reason: str
    observation: MutationReceipt | None = None
    evaluation_run: MutationReceipt | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "capability.observation_normalization.v1",
            "status": self.status,
            "reason": self.reason,
            "observation": self.observation.to_dict() if self.observation is not None else None,
            "evaluation_run": self.evaluation_run.to_dict() if self.evaluation_run is not None else None,
        }


class CapabilityObservations:
    """Runtime-facing observation and v3-ledger facade.

    The facade is intentionally narrow.  It has no general record scan and no
    fallback attribution rules, so an unknown outcome remains unclassified
    rather than acquiring a guessed cognitive meaning.
    """

    def __init__(self, store: RuntimeStore) -> None:
        self._store = store

    def append(
        self,
        observation: CapabilityObservation,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        request_key: str = "",
    ) -> ObservationNormalizationResult:
        scope = exact_runtime_scope(runtime_scope)
        stored = self._store.mutate_capabilities_atomically(
            lambda repository: repository.append_observation(
                observation,
                scope=scope,
                request_key=request_key,
            )
        )
        return ObservationNormalizationResult(
            status="recorded",
            reason="explicit_observation",
            observation=MutationReceipt.from_stored(stored),
        )

    def record_evaluation_run(
        self,
        run: EvaluationRun,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        profile_id: str | None = None,
        request_key: str = "",
    ) -> ObservationNormalizationResult:
        """Persist a terminal evaluation and its derived observation atomically."""

        scope = exact_runtime_scope(runtime_scope)
        observation = observation_from_evaluation_run(run)
        normalized_request_key = str(request_key or "").strip()

        def mutation(repository):
            stored_run = repository.record_evaluation_run(
                run,
                scope=scope,
                profile_id=profile_id,
                request_key=normalized_request_key or f"evaluation-run:{run.source}:{run.idempotency_key}",
            )
            stored_observation = repository.append_observation(
                observation,
                scope=scope,
                request_key=(
                    f"evaluation-observation:{run.source}:{run.idempotency_key}"
                    if not normalized_request_key
                    else f"{normalized_request_key}:observation"
                ),
            )
            return stored_run, stored_observation

        stored_run, stored_observation = self._store.mutate_capabilities_atomically(mutation)
        return ObservationNormalizationResult(
            status="recorded",
            reason="evaluation_run_normalized",
            evaluation_run=MutationReceipt.from_stored(stored_run),
            observation=MutationReceipt.from_stored(stored_observation),
        )

    def normalize_outcome(
        self,
        outcome: Mapping[str, Any],
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str = "global",
        request_key: str = "",
    ) -> ObservationNormalizationResult:
        """Normalize an explicitly attributed, independently verified outcome.

        This is deliberately not a text classifier.  Missing revision/binding
        context, evidence, or a verifier leaves the original outcome untouched
        and returns ``unclassified``.  Callers can later attach an explicit
        attribution or migration adapter without inventing a capability.
        """

        scope = exact_runtime_scope(runtime_scope)
        if not isinstance(outcome, Mapping):
            raise CapabilityObservationError("outcome must be a mapping")
        attribution = outcome.get("capability_attribution")
        verifier = outcome.get("verifier")
        if not isinstance(attribution, Mapping):
            return ObservationNormalizationResult("unclassified", "missing_explicit_capability_attribution")
        if not isinstance(verifier, Mapping) or verifier.get("independent") is not True:
            return ObservationNormalizationResult("unclassified", "missing_independent_verifier")
        required_attribution = (
            "capability_id",
            "capability_revision_id",
            "provider_binding_id",
            "idempotency_key",
            "observed_at",
            "evidence_refs",
            "environment_fingerprint",
            "provenance",
        )
        missing = [key for key in required_attribution if key not in attribution]
        if missing:
            return ObservationNormalizationResult("unclassified", f"missing_attribution_fields:{','.join(missing)}")
        evidence_refs = attribution.get("evidence_refs")
        environment = attribution.get("environment_fingerprint")
        provenance = attribution.get("provenance")
        if not isinstance(evidence_refs, Sequence) or isinstance(evidence_refs, (str, bytes)) or not evidence_refs:
            return ObservationNormalizationResult("unclassified", "missing_evidence_refs")
        if not isinstance(environment, Mapping) or not environment:
            return ObservationNormalizationResult("unclassified", "missing_environment_fingerprint")
        if not isinstance(provenance, Mapping) or not provenance:
            return ObservationNormalizationResult("unclassified", "missing_provenance")
        verifier_id = str(verifier.get("id") or "").strip()
        verifier_revision = str(verifier.get("revision") or "").strip()
        verifier_digest = str(verifier.get("contract_digest") or "").strip()
        if not verifier_id or not verifier_revision or not verifier_digest:
            return ObservationNormalizationResult("unclassified", "incomplete_verifier_identity")
        # Absence of a verdict is not negative evidence.  Treating it as a
        # failure would let a partially populated integration regress a
        # capability merely by omitting one field from an otherwise trusted
        # verifier envelope.
        if not isinstance(verifier.get("passed"), bool):
            return ObservationNormalizationResult("unclassified", "verifier_verdict_missing_or_invalid")
        try:
            normalized_verifier_digest = normalize_sha256(verifier_digest, field="verifier.contract_digest")
            observed_at = require_timestamp(attribution.get("observed_at"), field="observed_at")
            normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        except Exception as exc:
            raise CapabilityObservationError(str(exc)) from exc

        verdict = "pass" if verifier["passed"] is True else "fail"
        output_payload = _plain_json(outcome, field="outcome")
        evidence_payload = _plain_json(
            {"evidence_refs": list(evidence_refs), "verifier": dict(verifier)},
            field="outcome evidence",
        )
        input_payload = _plain_json(dict(attribution), field="outcome attribution")
        identity = _stable_digest(
            {
                "scope": _scope_payload(scope),
                "capability_scope": normalized_scope,
                "idempotency_key": attribution.get("idempotency_key"),
                "attribution": input_payload,
                "verifier": dict(verifier),
            }
        )
        observation = CapabilityObservation(
            observation_id=f"outcome-observation-{identity[:40]}",
            capability_id=str(attribution["capability_id"]),
            capability_revision_id=str(attribution["capability_revision_id"]),
            provider_binding_id=str(attribution["provider_binding_id"]),
            idempotency_key=str(attribution["idempotency_key"]),
            verdict=verdict,
            source="outcome_normalization",
            executor_id=normalize_opaque_id(verifier_id, field="verifier.id"),
            executor_contract_digest=normalized_verifier_digest,
            grader_id=normalize_opaque_id(verifier_id, field="verifier.id"),
            grader_revision=normalize_opaque_id(verifier_revision, field="verifier.revision"),
            input_digest=_stable_digest(input_payload),
            output_digest=_stable_digest(output_payload),
            evidence_digest=_stable_digest(evidence_payload),
            evidence_refs=tuple(str(item) for item in evidence_refs),
            environment_fingerprint=dict(environment),
            provenance={
                **dict(provenance),
                "normalizer": "capability.observation.v1",
                "verifier_id": verifier_id,
                "verifier_revision": verifier_revision,
            },
            metrics={"verified": 1.0 if verdict == "pass" else 0.0},
            error_taxonomy={} if verdict == "pass" else {"outcome": "independent_verifier_failed"},
            observed_at=observed_at,
            scope=normalized_scope,
            deployment_authority=(
                dict(attribution["deployment_authority"])
                if isinstance(attribution.get("deployment_authority"), Mapping)
                else {}
            ),
        )
        return self.append(
            observation,
            runtime_scope=scope,
            request_key=request_key or f"outcome-observation:{identity}",
        )

    def list(
        self,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        capability_id: str = "",
        capability_revision_id: str = "",
        provider_binding_id: str = "",
        since: str = "",
        until: str = "",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        scope = exact_runtime_scope(runtime_scope)
        return self._store.read_capabilities(
            lambda repository: repository.list_observations(
                scope=scope,
                capability_scope=capability_scope,
                capability_id=capability_id,
                capability_revision_id=capability_revision_id,
                provider_binding_id=provider_binding_id,
                since=since,
                until=until,
                limit=limit,
            )
        )

    def build_ledger(
        self,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Build a bounded, reproducible v3 observation projection.

        It is deliberately a ledger, not a maturity score.  WP9 owns profile
        applicability, dependencies, freshness, and maturity synthesis.  The
        input watermark makes any late failure visible to that later projector.
        """

        rows = self.list(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            limit=limit,
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
            capability_id = str(payload.get("capability_id") or "")
            revision_id = str(payload.get("capability_revision_id") or "")
            binding_id = str(payload.get("provider_binding_id") or "")
            if not capability_id or not revision_id or not binding_id:
                # A malformed row is a storage integrity error at the source;
                # never project it under a synthetic capability label.
                continue
            grouped[capability_id].append(row)

        capabilities: dict[str, dict[str, Any]] = {}
        for capability_id, capability_rows in sorted(grouped.items()):
            revisions: dict[str, dict[str, Any]] = {}
            for row in sorted(capability_rows, key=_observation_sort_key, reverse=True):
                payload = row["payload"]
                revision_id = str(payload["capability_revision_id"])
                binding_id = str(payload["provider_binding_id"])
                revision = revisions.setdefault(
                    revision_id,
                    {"bindings": {}, "observation_count": 0, "pass_count": 0, "failure_count": 0},
                )
                binding = revision["bindings"].setdefault(
                    binding_id,
                    {
                        "observation_count": 0,
                        "decisive_count": 0,
                        "pass_count": 0,
                        "failure_count": 0,
                        "inconclusive_count": 0,
                        "latest": None,
                        "watermark": "",
                        "evidence_refs": set(),
                    },
                )
                verdict = str(payload.get("verdict") or "")
                binding["observation_count"] += 1
                revision["observation_count"] += 1
                if verdict == "pass":
                    binding["decisive_count"] += 1
                    binding["pass_count"] += 1
                    revision["pass_count"] += 1
                elif verdict == "fail":
                    binding["decisive_count"] += 1
                    binding["failure_count"] += 1
                    revision["failure_count"] += 1
                else:
                    binding["inconclusive_count"] += 1
                if binding["latest"] is None:
                    binding["latest"] = {
                        "observation_id": row["observation_id"],
                        "verdict": verdict,
                        "observed_at": row["observed_at"],
                        "ledger_event_id": row["ledger_event_id"],
                    }
                binding["watermark"] = max(binding["watermark"], _watermark(row))
                binding["evidence_refs"].update(str(ref) for ref in payload.get("evidence_refs", []))

            normalized_revisions: dict[str, Any] = {}
            for revision_id, revision in sorted(revisions.items()):
                bindings: dict[str, Any] = {}
                for binding_id, binding in sorted(revision["bindings"].items()):
                    decisive = int(binding["decisive_count"])
                    pass_rate = round(int(binding["pass_count"]) / decisive, 6) if decisive else None
                    bindings[binding_id] = {
                        "observation_count": int(binding["observation_count"]),
                        "decisive_count": decisive,
                        "pass_count": int(binding["pass_count"]),
                        "failure_count": int(binding["failure_count"]),
                        "inconclusive_count": int(binding["inconclusive_count"]),
                        "pass_rate": pass_rate,
                        "latest": binding["latest"],
                        "watermark": binding["watermark"],
                        "evidence_refs": sorted(binding["evidence_refs"]),
                    }
                normalized_revisions[revision_id] = {
                    "observation_count": int(revision["observation_count"]),
                    "pass_count": int(revision["pass_count"]),
                    "failure_count": int(revision["failure_count"]),
                    "bindings": bindings,
                }
            capabilities[capability_id] = {"revisions": normalized_revisions}

        overall_watermark = max((_watermark(row) for row in rows), default="")
        return {
            "schema": "capability.ledger.v3",
            "ok": True,
            "capability_scope": capability_scope,
            "observation_count": len(rows),
            "input_watermark": overall_watermark,
            "capabilities": capabilities,
            "projection_digest": _stable_digest(
                {
                    "capability_scope": capability_scope,
                    "input_watermark": overall_watermark,
                    "capabilities": capabilities,
                }
            ),
        }


def observation_from_evaluation_run(run: EvaluationRun) -> CapabilityObservation:
    """Create the one deterministic observation corresponding to a terminal run."""

    identity = sha256(run.run_digest.encode("ascii")).hexdigest()
    return CapabilityObservation(
        observation_id=f"evaluation-observation-{identity[:40]}",
        capability_id=run.capability_id,
        capability_revision_id=run.capability_revision_id,
        provider_binding_id=run.provider_binding_id,
        idempotency_key=f"evaluation-run-{identity[:40]}",
        verdict=run.verdict,
        source="evaluation_run",
        executor_id=run.executor_id,
        executor_contract_digest=run.executor_contract_digest,
        grader_id=run.grader_id,
        grader_revision=run.grader_revision,
        input_digest=run.input_digest,
        output_digest=run.output_digest,
        evidence_digest=run.evidence_digest,
        evidence_refs=run.evidence_refs,
        environment_fingerprint=run.environment_fingerprint,
        provenance={
            **dict(run.provenance),
            "normalized_from": "evaluation_run",
            "evaluation_run_id": run.run_id,
            "evaluation_run_digest": run.run_digest,
        },
        metrics=run.metrics,
        error_taxonomy=run.error_taxonomy,
        observed_at=run.finished_at,
        scope=run.scope,
        deployment_authority=run.deployment_authority,
    )


def _observation_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return str(row.get("observed_at") or ""), str(row.get("observation_id") or "")


def _watermark(row: Mapping[str, Any]) -> str:
    return f"{row.get('observed_at') or ''}|{row.get('observation_id') or ''}"


def _plain_json(value: object, *, field: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapabilityObservationError(f"{field} must be JSON-serializable") from exc


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _scope_payload(scope: ScopeRef) -> dict[str, str]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }


__all__ = [
    "CapabilityObservationError",
    "CapabilityObservations",
    "ObservationNormalizationResult",
    "observation_from_evaluation_run",
]
