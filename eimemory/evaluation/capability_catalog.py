"""Runtime catalog for bounded capability evaluations.

The catalog separates *what* is evaluated from *how* trusted code evaluates
it.  Specs and profile selectors carry plain JSON data; executor and grader
implementations live in narrow, in-process allowlists.  A missing, ambiguous,
or conflicting selection is an explicit blocked result instead of a fallback
to a capability name, version, host, or newest row.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from threading import RLock
from typing import Any, Callable, Mapping, Sequence

from eimemory.capabilities.contracts import (
    CapabilityContractError,
    contract_digest,
    normalize_capability_id,
    normalize_json_payload,
    normalize_opaque_id,
    normalize_sha256,
)
from eimemory.capabilities.models import EvaluationRun, EvaluationSpec
from eimemory.evaluation.capability_graders import (
    CapabilityGraderError,
    CapabilityGraderRegistry,
    SCHEMA_RULE_GRADER_ID,
    normalize_rules,
)
from eimemory.models.records import ScopeRef


CATALOG_SCHEMA_VERSION = "capability_evaluation_catalog.v1"
DEFAULT_EXECUTOR_REVISION = "v1"
DEFAULT_GRADER_REVISION = "v1"
_ALLOWED_GRADER_TYPES = frozenset({"code", "schema_rule", "model"})


class CatalogResolutionError(RuntimeError):
    """A catalog, profile, executor, or persistence selection failed closed."""


ProbeExecutor = Callable[[dict[str, Any], dict[str, Any], Any], dict[str, Any]]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_mapping(value: Mapping[str, Any] | object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CatalogResolutionError(f"{field} must be an object")
    try:
        return normalize_json_payload(value, field=field, reject_executable=True)
    except CapabilityContractError as exc:
        raise CatalogResolutionError(str(exc)) from exc


def _selector(value: Mapping[str, Any] | object) -> dict[str, Any]:
    """Validate a deliberately tiny, non-executable binding selector DSL."""

    normalized = _safe_mapping(value, field="binding_selector")
    unknown = set(normalized).difference({"binding_ids", "provider_kind", "provider_instance_id", "operations_all"})
    if unknown:
        raise CatalogResolutionError(
            f"binding_selector contains unsupported keys: {', '.join(sorted(unknown))}"
        )
    result: dict[str, Any] = {}
    for key in ("provider_kind", "provider_instance_id"):
        if key in normalized:
            try:
                result[key] = normalize_opaque_id(normalized[key], field=f"binding_selector.{key}")
            except CapabilityContractError as exc:
                raise CatalogResolutionError(str(exc)) from exc
    for key in ("binding_ids", "operations_all"):
        if key not in normalized:
            continue
        values = normalized[key]
        if not isinstance(values, list) or not values or len(values) > 64:
            raise CatalogResolutionError(f"binding_selector.{key} must be a non-empty list of at most 64 IDs")
        try:
            result[key] = tuple(
                sorted({normalize_opaque_id(item, field=f"binding_selector.{key}") for item in values})
            )
        except CapabilityContractError as exc:
            raise CatalogResolutionError(str(exc)) from exc
    return result


def _executor_contract_digest(executor_id: str, executor_revision: str) -> str:
    return contract_digest(
        {
            "schema": CATALOG_SCHEMA_VERSION,
            "executor_id": executor_id,
            "executor_revision": executor_revision,
        }
    )


def execution_evidence_digest(
    *,
    executor_id: str,
    executor_version: str,
    input_data: Mapping[str, Any],
    output: Mapping[str, Any],
    observation: Mapping[str, Any],
    checks: Sequence[Mapping[str, Any]],
) -> str:
    """Canonical execution digest shared by direct and legacy catalog paths."""

    payload = {
        "executor_id": str(executor_id),
        "executor_version": str(executor_version),
        "input": deepcopy(dict(input_data)),
        "output": deepcopy(dict(output)),
        "observation": deepcopy(dict(observation)),
        "checks": [deepcopy(dict(check)) for check in checks],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CatalogCase:
    """One immutable declarative evaluation case.

    ``input_data``, ``fixture``, rules, and selectors are JSON-only.  Neither
    the case nor its storage representation can carry an executable command.
    """

    case_id: str
    capability_id: str
    executor_id: str
    input_data: Mapping[str, Any]
    fixture: Mapping[str, Any]
    expected_invariants: Sequence[Mapping[str, Any]]
    executor_revision: str = DEFAULT_EXECUTOR_REVISION
    executor_contract_digest: str = ""
    grader_type: str = "schema_rule"
    grader_id: str = SCHEMA_RULE_GRADER_ID
    grader_revision: str = DEFAULT_GRADER_REVISION
    eval_spec_id: str = ""
    revision: str = "v1"
    binding_selector: Mapping[str, Any] = field(default_factory=dict)
    retry_policy: Mapping[str, Any] = field(default_factory=lambda: {"max_attempts": 1})
    stability_policy: Mapping[str, Any] = field(default_factory=lambda: {"min_consecutive_passes": 1})
    resource_budget: Mapping[str, Any] = field(
        default_factory=lambda: {"timeout_seconds": 30, "max_memory_mb": 128, "max_artifact_bytes": 262_144}
    )
    model_grader_policy: Mapping[str, Any] = field(default_factory=dict)
    case_digest: str = field(init=False)

    def __post_init__(self) -> None:
        try:
            case_id = normalize_opaque_id(self.case_id, field="case_id")
            capability_id = normalize_capability_id(self.capability_id)
            executor_id = normalize_opaque_id(self.executor_id, field="executor_id")
            executor_revision = normalize_opaque_id(self.executor_revision, field="executor_revision")
            grader_id = normalize_opaque_id(self.grader_id, field="grader_id")
            grader_revision = normalize_opaque_id(self.grader_revision, field="grader_revision")
            revision = normalize_opaque_id(self.revision, field="evaluation_revision")
        except CapabilityContractError as exc:
            raise CatalogResolutionError(str(exc)) from exc
        if self.grader_type not in _ALLOWED_GRADER_TYPES:
            raise CatalogResolutionError("grader_type must be code, schema_rule, or model")
        input_data = _safe_mapping(self.input_data, field="evaluation_input")
        fixture = _safe_mapping(self.fixture, field="evaluation_fixture")
        try:
            rules = normalize_rules(self.expected_invariants)
        except CapabilityGraderError as exc:
            raise CatalogResolutionError(str(exc)) from exc
        selector = _selector(self.binding_selector)
        retry_policy = _safe_mapping(self.retry_policy, field="retry_policy")
        stability_policy = _safe_mapping(self.stability_policy, field="stability_policy")
        resource_budget = _safe_mapping(self.resource_budget, field="resource_budget")
        model_policy = _safe_mapping(self.model_grader_policy, field="model_grader_policy")
        if self.grader_type == "model":
            max_tokens = model_policy.get("max_tokens")
            if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 16_384:
                raise CatalogResolutionError("model_grader_policy.max_tokens must be an integer from 1 to 16384")
            if model_policy.get("fail_closed") is not True and not str(model_policy.get("tie_breaker") or "").strip():
                raise CatalogResolutionError("model grader requires deterministic tie_breaker or fail_closed")
        elif model_policy:
            raise CatalogResolutionError("model_grader_policy is only valid for grader_type=model")
        try:
            max_attempts = retry_policy.get("max_attempts")
            min_passes = stability_policy.get("min_consecutive_passes")
            if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 32:
                raise CatalogResolutionError("retry_policy.max_attempts must be an integer from 1 to 32")
            if isinstance(min_passes, bool) or not isinstance(min_passes, int) or not 1 <= min_passes <= 128:
                raise CatalogResolutionError("stability_policy.min_consecutive_passes must be an integer from 1 to 128")
            allowed_budget = {"timeout_seconds", "max_memory_mb", "max_artifact_bytes"}
            if not resource_budget or set(resource_budget).difference(allowed_budget):
                raise CatalogResolutionError("resource_budget contains unsupported or no fields")
            if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in resource_budget.values()):
                raise CatalogResolutionError("resource_budget values must be positive integers")
        except AttributeError as exc:
            raise CatalogResolutionError("evaluation policy must be an object") from exc
        digest = str(self.executor_contract_digest or "").strip()
        if digest:
            try:
                digest = normalize_sha256(digest, field="executor_contract_digest")
            except CapabilityContractError as exc:
                raise CatalogResolutionError(str(exc)) from exc
        else:
            digest = _executor_contract_digest(executor_id, executor_revision)
        eval_spec_id = str(self.eval_spec_id or "").strip() or f"eval.{case_id}:{revision}"
        try:
            eval_spec_id = normalize_opaque_id(eval_spec_id, field="eval_spec_id")
        except CapabilityContractError as exc:
            raise CatalogResolutionError(str(exc)) from exc
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "capability_id", capability_id)
        object.__setattr__(self, "executor_id", executor_id)
        object.__setattr__(self, "executor_revision", executor_revision)
        object.__setattr__(self, "executor_contract_digest", digest)
        object.__setattr__(self, "grader_id", grader_id)
        object.__setattr__(self, "grader_revision", grader_revision)
        object.__setattr__(self, "eval_spec_id", eval_spec_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "input_data", input_data)
        object.__setattr__(self, "fixture", fixture)
        object.__setattr__(self, "expected_invariants", rules)
        object.__setattr__(self, "binding_selector", selector)
        object.__setattr__(self, "retry_policy", retry_policy)
        object.__setattr__(self, "stability_policy", stability_policy)
        object.__setattr__(self, "resource_budget", resource_budget)
        object.__setattr__(self, "model_grader_policy", model_policy)
        object.__setattr__(
            self,
            "case_digest",
            contract_digest(
                {
                    "schema": CATALOG_SCHEMA_VERSION,
                    "case_id": case_id,
                    "capability_id": capability_id,
                    "executor_id": executor_id,
                    "executor_revision": executor_revision,
                    "executor_contract_digest": digest,
                    "grader_type": self.grader_type,
                    "grader_id": grader_id,
                    "grader_revision": grader_revision,
                    "eval_spec_id": eval_spec_id,
                    "revision": revision,
                    "input": input_data,
                    "fixture": fixture,
                    "expected_invariants": list(rules),
                    "binding_selector": selector,
                    "retry_policy": retry_policy,
                    "stability_policy": stability_policy,
                    "resource_budget": resource_budget,
                    "model_grader_policy": model_policy,
                }
            ),
        )

    def to_artifact(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "capability": self.capability_id,
            "input": deepcopy(dict(self.input_data)),
            "fixture": deepcopy(dict(self.fixture)),
            "expected_invariants": [deepcopy(dict(rule)) for rule in self.expected_invariants],
            "executor_id": self.executor_id,
            "executor_version": self.executor_revision,
            "executor_contract_digest": self.executor_contract_digest,
            "grader_type": self.grader_type,
            "grader_id": self.grader_id,
            "grader_revision": self.grader_revision,
            "eval_spec_id": self.eval_spec_id,
            "evaluation_revision": self.revision,
            "evaluation_case_digest": self.case_digest,
            "binding_selector": deepcopy(dict(self.binding_selector)),
            "retry_policy": deepcopy(dict(self.retry_policy)),
            "stability_policy": deepcopy(dict(self.stability_policy)),
            "resource_budget": deepcopy(dict(self.resource_budget)),
            "model_grader_policy": deepcopy(dict(self.model_grader_policy)),
        }

    def to_evaluation_spec(
        self,
        *,
        capability_revision_id: str,
        capability_scope: str,
        created_at: str | None = None,
    ) -> EvaluationSpec:
        try:
            normalized_revision_id = normalize_opaque_id(capability_revision_id, field="capability_revision_id")
            normalized_scope = normalize_opaque_id(capability_scope, field="capability_scope")
        except CapabilityContractError as exc:
            raise CatalogResolutionError(str(exc)) from exc
        revision_suffix = contract_digest(
            {"case_digest": self.case_digest, "capability_revision_id": normalized_revision_id}
        )[:20]
        spec_id = f"{self.eval_spec_id}.{revision_suffix}"
        checks = tuple(f"{rule['field']}_{rule['op']}" for rule in self.expected_invariants)
        return EvaluationSpec(
            eval_spec_id=spec_id,
            capability_id=self.capability_id,
            capability_revision_id=normalized_revision_id,
            grader_type=self.grader_type,
            executor_id=self.executor_id,
            executor_contract_digest=self.executor_contract_digest,
            fixture_refs=(f"artifact://evaluation/{self.case_digest}.json",),
            checks=checks,
            required_metrics=("pass_rate", "check_count"),
            retry_policy=dict(self.retry_policy),
            stability_policy=dict(self.stability_policy),
            applicability={"capability_scope": normalized_scope, "case_digest": self.case_digest},
            resource_budget=dict(self.resource_budget),
            provenance={
                "source": "eimemory.evaluation.catalog",
                "catalog_schema": CATALOG_SCHEMA_VERSION,
                "case_id": self.case_id,
                "case_digest": self.case_digest,
            },
            created_at=created_at or _utc_now(),
            binding_selector=dict(self.binding_selector),
            model_grader_policy=dict(self.model_grader_policy),
            status="active",
            scope=normalized_scope,
            revision=self.revision,
        )


@dataclass(frozen=True, slots=True)
class ExecutorRegistration:
    executor_id: str
    revision: str
    contract_digest: str
    handler: ProbeExecutor


@dataclass(frozen=True, slots=True)
class EvaluationTarget:
    capability_id: str
    capability_revision_id: str
    provider_binding_id: str
    profile_id: str = ""
    profile_key: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "capability_id", normalize_capability_id(self.capability_id))
            object.__setattr__(
                self,
                "capability_revision_id",
                normalize_opaque_id(self.capability_revision_id, field="capability_revision_id"),
            )
            object.__setattr__(
                self,
                "provider_binding_id",
                normalize_opaque_id(self.provider_binding_id, field="provider_binding_id"),
            )
            if self.profile_id:
                object.__setattr__(self, "profile_id", normalize_opaque_id(self.profile_id, field="profile_id"))
            if self.profile_key:
                object.__setattr__(self, "profile_key", normalize_opaque_id(self.profile_key, field="profile_key"))
        except CapabilityContractError as exc:
            raise CatalogResolutionError(str(exc)) from exc

    def to_dict(self) -> dict[str, str]:
        return {
            "capability_id": self.capability_id,
            "capability_revision_id": self.capability_revision_id,
            "provider_binding_id": self.provider_binding_id,
            "profile_id": self.profile_id,
            "profile_key": self.profile_key,
        }

    @classmethod
    def from_value(cls, value: object) -> "EvaluationTarget | None":
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            return None
        try:
            return cls(
                capability_id=str(value.get("capability_id") or ""),
                capability_revision_id=str(value.get("capability_revision_id") or ""),
                provider_binding_id=str(value.get("provider_binding_id") or ""),
                profile_id=str(value.get("profile_id") or ""),
                profile_key=str(value.get("profile_key") or ""),
            )
        except (CatalogResolutionError, TypeError, ValueError):
            return None


class CapabilityEvaluationCatalog:
    """Immutable-case catalog and bounded executor/grader registrations."""

    def __init__(self, *, graders: CapabilityGraderRegistry | None = None) -> None:
        self._cases: dict[str, CatalogCase] = {}
        self._executors: dict[str, ExecutorRegistration] = {}
        self.graders = graders or CapabilityGraderRegistry()
        self._sealed = False

    @property
    def sealed(self) -> bool:
        """Whether executable registrations have been closed for this catalog."""

        return self._sealed

    def seal(self) -> "CapabilityEvaluationCatalog":
        """Close executor, case, and grader registration after bootstrap.

        Evaluation execution remains available after sealing.  Only the small
        trusted application/bootstrap path may populate an application catalog
        before it is published to normal runtime consumers.
        """

        self._sealed = True
        self.graders.seal()
        return self

    def _require_mutable(self) -> None:
        if self._sealed:
            raise CatalogResolutionError("capability_catalog_sealed")

    def register_executor(
        self,
        *,
        executor_id: str,
        revision: str,
        handler: ProbeExecutor,
        contract_descriptor: Mapping[str, Any] | None = None,
    ) -> ExecutorRegistration:
        self._require_mutable()
        try:
            normalized_id = normalize_opaque_id(executor_id, field="executor_id")
            normalized_revision = normalize_opaque_id(revision, field="executor_revision")
        except CapabilityContractError as exc:
            raise CatalogResolutionError(str(exc)) from exc
        if not callable(handler):
            raise CatalogResolutionError("executor handler must be a trusted callable")
        descriptor = _safe_mapping(contract_descriptor or {}, field="executor_contract")
        digest = contract_digest(
            {
                "schema": CATALOG_SCHEMA_VERSION,
                "executor_id": normalized_id,
                "executor_revision": normalized_revision,
                **({"descriptor": descriptor} if descriptor else {}),
            }
        )
        registration = ExecutorRegistration(normalized_id, normalized_revision, digest, handler)
        existing = self._executors.get(normalized_id)
        if existing is not None:
            if existing.revision != registration.revision or existing.contract_digest != registration.contract_digest:
                raise CatalogResolutionError(f"conflicting executor registration: {normalized_id}")
            return existing
        self._executors[normalized_id] = registration
        return registration

    def register_case(self, case: CatalogCase) -> CatalogCase:
        self._require_mutable()
        if not isinstance(case, CatalogCase):
            raise CatalogResolutionError("catalog_case_must_be_CatalogCase")
        existing = self._cases.get(case.case_id)
        if existing is not None:
            if existing.case_digest != case.case_digest:
                raise CatalogResolutionError(
                    f"conflicting immutable evaluation case: {case.case_id}; create a new revision/case ID"
                )
            return existing
        self._cases[case.case_id] = case
        return case

    def describe_executor(self, executor_id: str) -> dict[str, str] | None:
        registration = self._executors.get(str(executor_id or "").strip())
        if registration is None:
            return None
        return {
            "executor_id": registration.executor_id,
            "executor_revision": registration.revision,
            "executor_contract_digest": registration.contract_digest,
        }

    def register_cases(self, cases: Sequence[CatalogCase]) -> tuple[CatalogCase, ...]:
        return tuple(self.register_case(case) for case in cases)

    def get_case(self, case_id: str) -> CatalogCase | None:
        return self._cases.get(str(case_id or "").strip())

    def case_artifact(self, case_id: str) -> dict[str, Any]:
        case = self.get_case(case_id)
        return case.to_artifact() if case is not None else {}

    def list_cases(self, *, capability_id: str = "") -> list[CatalogCase]:
        expected = str(capability_id or "").strip()
        return [
            case
            for _case_id, case in sorted(self._cases.items())
            if not expected or case.capability_id == expected
        ]

    def validate_artifact(self, artifact: Mapping[str, Any] | object) -> tuple[CatalogCase | None, str]:
        if not isinstance(artifact, Mapping):
            return None, "evaluation artifact must be an object"
        case_id = str(artifact.get("case_id") or "").strip()
        case = self.get_case(case_id)
        if case is None:
            return None, f"unknown evaluation case: {case_id}"
        canonical = case.to_artifact()
        for key in (
            "capability",
            "input",
            "fixture",
            "expected_invariants",
            "executor_id",
            "executor_version",
            "executor_contract_digest",
            "grader_type",
            "grader_id",
            "grader_revision",
            "eval_spec_id",
            "evaluation_revision",
            "evaluation_case_digest",
            "binding_selector",
            "retry_policy",
            "stability_policy",
            "resource_budget",
            "model_grader_policy",
        ):
            if key in artifact and artifact.get(key) != canonical.get(key):
                return None, f"evaluation artifact tampered field: {key}"
        return case, ""

    def execute(
        self,
        artifact: Mapping[str, Any] | object,
        *,
        runtime: Any,
        evidence_ref: str,
    ) -> dict[str, Any]:
        case, error = self.validate_artifact(artifact)
        if case is None:
            return _failed_execution(str(artifact.get("case_id") or "") if isinstance(artifact, Mapping) else "", error, evidence_ref)
        executor = self._executors.get(case.executor_id)
        if executor is None:
            return _failed_execution(case.case_id, "executor_unavailable", evidence_ref, case=case)
        if executor.revision != case.executor_revision or executor.contract_digest != case.executor_contract_digest:
            return _failed_execution(case.case_id, "executor_contract_mismatch", evidence_ref, case=case)
        try:
            raw_output = executor.handler(deepcopy(dict(case.input_data)), deepcopy(dict(case.fixture)), runtime)
        except Exception as exc:  # An executor exception is always closed, never interpreted as a pass.
            return _failed_execution(case.case_id, f"executor_exception:{type(exc).__name__}", evidence_ref, case=case)
        if not isinstance(raw_output, Mapping):
            return _failed_execution(case.case_id, "executor_output_invalid", evidence_ref, case=case)
        grade = self.graders.grade(
            grader_id=case.grader_id,
            grader_type=case.grader_type,
            output=raw_output,
            rules=case.expected_invariants,
            evidence_ref=evidence_ref,
            context={"case_id": case.case_id, "capability_id": case.capability_id, "case_digest": case.case_digest},
            model_policy=case.model_grader_policy,
        )
        verdict = str(grade.get("verdict") or "blocked")
        result = {
            "case_id": case.case_id,
            "capability": case.capability_id,
            "executor_id": executor.executor_id,
            "executor_version": executor.revision,
            "executor_contract_digest": executor.contract_digest,
            "grader_id": str(grade.get("grader_id") or case.grader_id),
            "grader_revision": str(grade.get("grader_revision") or case.grader_revision),
            "grader_type": case.grader_type,
            "input": deepcopy(dict(case.input_data)),
            "output": deepcopy(dict(raw_output)),
            "observation": deepcopy(dict(grade.get("observation") or {})),
            "checks": [deepcopy(dict(item)) for item in grade.get("checks") or [] if isinstance(item, Mapping)],
            "metrics": deepcopy(dict(grade.get("metrics") or {})),
            "passed": verdict == "pass",
            "verdict": verdict,
            "error": str(grade.get("error") or ""),
            "evaluation_case_digest": case.case_digest,
            "eval_spec_id": case.eval_spec_id,
        }
        result["execution_digest"] = execution_evidence_digest(
            executor_id=result["executor_id"],
            executor_version=result["executor_version"],
            input_data=result["input"],
            output=result["output"],
            observation=result["observation"],
            checks=result["checks"],
        )
        return result

    def resolve_profile_cases(
        self,
        runtime: Any,
        *,
        profile_key: str,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        case_ids: Sequence[str] | None = None,
        at_time: str = "",
        max_candidates: int = 100,
    ) -> dict[str, Any]:
        """Return only exactly selected profile targets, otherwise block.

        One evaluation case maps to one revision and one binding.  A profile
        that deliberately selects several revisions/bindings remains valid for
        readiness, but it is not silently collapsed into one evaluation run.
        Callers must register separate profiles or selectors for those targets.
        """

        from eimemory.capabilities.registry import exact_runtime_scope

        scope = exact_runtime_scope(runtime_scope)
        requested = _requested_case_ids(case_ids)
        try:
            resolution = runtime.capabilities.resolve_profile(
                profile_key,
                runtime_scope=scope,
                capability_scope=capability_scope,
                at_time=at_time,
                max_candidates=max_candidates,
            )
        except Exception as exc:
            return _blocked_selection("profile_resolution_failed", detail=type(exc).__name__)
        profile = resolution.get("profile") if isinstance(resolution, Mapping) else {}
        profile_id = str(profile.get("profile_id") or "") if isinstance(profile, Mapping) else ""
        if not profile_id:
            return _blocked_selection("profile_identity_missing")
        entries: list[dict[str, Any]] = []
        errors: list[str] = []
        for requirement in resolution.get("requirements") or []:
            if not isinstance(requirement, Mapping):
                errors.append("profile_requirement_invalid")
                continue
            capability_id = str(requirement.get("capability_id") or "")
            cases = [case for case in self.list_cases(capability_id=capability_id) if not requested or case.case_id in requested]
            if not cases:
                continue
            revisions = requirement.get("revisions") if isinstance(requirement.get("revisions"), list) else []
            bindings = requirement.get("bindings") if isinstance(requirement.get("bindings"), list) else []
            if len(revisions) != 1:
                errors.append(f"ambiguous_or_missing_revision:{capability_id}")
                continue
            revision_id = str((revisions[0] if isinstance(revisions[0], Mapping) else {}).get("entity_id") or "")
            if not revision_id:
                errors.append(f"revision_identity_missing:{capability_id}")
                continue
            for case in cases:
                matched_bindings = [
                    binding
                    for binding in bindings
                    if isinstance(binding, Mapping) and _binding_matches_selector(binding, case.binding_selector)
                ]
                if len(matched_bindings) != 1:
                    errors.append(f"ambiguous_or_missing_binding:{case.case_id}")
                    continue
                binding_id = str(matched_bindings[0].get("entity_id") or "")
                if not binding_id:
                    errors.append(f"binding_identity_missing:{case.case_id}")
                    continue
                entries.append(
                    {
                        "artifact": case.to_artifact(),
                        "target": EvaluationTarget(
                            capability_id=case.capability_id,
                            capability_revision_id=revision_id,
                            provider_binding_id=binding_id,
                            profile_id=profile_id,
                            profile_key=str(profile.get("profile_key") or profile_key),
                        ).to_dict(),
                    }
                )
        found = {str(entry["artifact"]["case_id"]) for entry in entries}
        missing = sorted(requested.difference(found))
        if missing:
            errors.extend(f"case_not_applicable:{case_id}" for case_id in missing)
        if errors:
            return _blocked_selection("profile_evaluation_selection_blocked", errors=errors, profile=dict(profile))
        if not entries:
            return _blocked_selection("profile_has_no_catalog_cases", profile=dict(profile))
        return {
            "ok": True,
            "status": "resolved",
            "profile": dict(profile),
            "cases": entries,
            "registry_watermark": str(resolution.get("registry_watermark") or ""),
            "lifecycle_watermark": str(resolution.get("lifecycle_watermark") or ""),
        }

    def resolve_active_cases(
        self,
        runtime: Any,
        *,
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        case_ids: Sequence[str] | None = None,
        at_time: str = "",
    ) -> dict[str, Any]:
        """Discover active cases only when each registry target is exact."""

        from eimemory.capabilities.registry import exact_runtime_scope

        scope = exact_runtime_scope(runtime_scope)
        requested = _requested_case_ids(case_ids)
        entries: list[dict[str, Any]] = []
        errors: list[str] = []
        cases = [case for case in self.list_cases() if not requested or case.case_id in requested]
        for case in cases:
            try:
                resolution = runtime.capabilities.resolve(
                    case.capability_id,
                    runtime_scope=scope,
                    capability_scope=capability_scope,
                    at_time=at_time,
                )
            except Exception as exc:
                errors.append(f"capability_resolution_failed:{case.case_id}:{type(exc).__name__}")
                continue
            if not resolution.ok or len(resolution.revisions) != 1:
                errors.append(f"capability_unresolved:{case.case_id}:{resolution.reason}")
                continue
            revision_id = str(resolution.revisions[0].get("entity_id") or "")
            matched_bindings = [binding for binding in resolution.bindings if _binding_matches_selector(binding, case.binding_selector)]
            if len(matched_bindings) != 1:
                errors.append(f"ambiguous_or_missing_binding:{case.case_id}")
                continue
            entries.append(
                {
                    "artifact": case.to_artifact(),
                    "target": EvaluationTarget(
                        capability_id=case.capability_id,
                        capability_revision_id=revision_id,
                        provider_binding_id=str(matched_bindings[0].get("entity_id") or ""),
                    ).to_dict(),
                }
            )
        missing = sorted(requested.difference({str(item["artifact"]["case_id"]) for item in entries}))
        if errors or missing:
            return _blocked_selection(
                "active_evaluation_selection_blocked",
                errors=[*errors, *(f"case_not_active:{case_id}" for case_id in missing)],
            )
        if not entries:
            return _blocked_selection("no_active_catalog_cases")
        return {"ok": True, "status": "resolved", "cases": entries}

    def persist_spec_and_run(
        self,
        runtime: Any,
        *,
        artifact: Mapping[str, Any],
        target: EvaluationTarget | Mapping[str, Any],
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        execution: Mapping[str, Any],
        probe_id: str,
        trace_record_id: str,
        execution_id: str,
    ) -> dict[str, Any]:
        """Write an EvaluationSpec/Run after independent trace evidence exists."""

        from eimemory.capabilities.registry import exact_runtime_scope

        scope = exact_runtime_scope(runtime_scope)
        target = EvaluationTarget.from_value(target)
        if target is None:
            raise CatalogResolutionError("evaluation target is invalid")
        case, error = self.validate_artifact(artifact)
        if case is None:
            raise CatalogResolutionError(error)
        if target.capability_id != case.capability_id:
            raise CatalogResolutionError("evaluation target capability mismatch")
        if not target.capability_revision_id or not target.provider_binding_id:
            raise CatalogResolutionError("evaluation target must include one revision and one binding")
        if not probe_id or not trace_record_id:
            raise CatalogResolutionError("evaluation run requires independently persisted probe and trace evidence")
        evidence_validation = _validate_independent_evidence_chain(
            runtime,
            scope=scope,
            probe_id=str(probe_id),
            trace_record_id=str(trace_record_id),
            capability_id=target.capability_id,
            capability_revision_id=target.capability_revision_id,
            provider_binding_id=target.provider_binding_id,
        )
        spec = case.to_evaluation_spec(
            capability_revision_id=target.capability_revision_id,
            capability_scope=capability_scope,
        )
        register_spec = getattr(getattr(runtime, "capabilities", None), "register_evaluation_spec", None)
        if not callable(register_spec):
            raise CatalogResolutionError("runtime capability evaluation storage API unavailable")
        spec_receipt = register_spec(
            spec,
            runtime_scope=scope,
            profile_id=target.profile_id or None,
            request_key=f"evaluation-spec:{spec.eval_spec_id}",
        )
        verdict = str(execution.get("verdict") or "blocked")
        if verdict not in {"pass", "fail", "blocked", "inconclusive", "stale", "invalid"}:
            verdict = "invalid"
        run_identity = contract_digest(
            {
                "eval_spec_id": spec.eval_spec_id,
                "execution_id": execution_id,
                "probe_id": probe_id,
                "trace_record_id": trace_record_id,
                "provider_binding_id": target.provider_binding_id,
            }
        )
        started_at = _utc_now()
        metrics = dict(execution.get("metrics") or {})
        if not metrics:
            metrics = {"pass_rate": 1.0 if verdict == "pass" else 0.0, "check_count": 0}
        run = EvaluationRun(
            run_id=f"evalrun.{run_identity[:40]}",
            eval_spec_id=spec.eval_spec_id,
            capability_id=case.capability_id,
            capability_revision_id=target.capability_revision_id,
            provider_binding_id=target.provider_binding_id,
            idempotency_key=f"evalrun.{run_identity[:48]}",
            verdict=verdict,
            source="eimemory.evaluation.catalog",
            executor_id=str(execution.get("executor_id") or case.executor_id),
            executor_contract_digest=str(execution.get("executor_contract_digest") or case.executor_contract_digest),
            grader_id=str(execution.get("grader_id") or case.grader_id),
            grader_revision=str(execution.get("grader_revision") or case.grader_revision),
            input_digest=contract_digest({"input": dict(execution.get("input") or {})}),
            output_digest=contract_digest({"output": dict(execution.get("output") or {})}),
            evidence_digest=contract_digest(
                {
                    "execution_digest": str(execution.get("execution_digest") or ""),
                    "probe_id": probe_id,
                    "trace_record_id": trace_record_id,
                }
            ),
            evidence_refs=(probe_id, trace_record_id),
            environment_fingerprint={
                "runtime": "capability_acceptance",
                "scope": {
                    "tenant_id": scope.tenant_id,
                    "agent_id": scope.agent_id,
                    "workspace_id": scope.workspace_id,
                    "user_id": scope.user_id,
                },
            },
            provenance={
                "source": "eimemory.evaluation.catalog",
                "case_id": case.case_id,
                "case_digest": case.case_digest,
                "execution_id": execution_id,
                "trace_record_id": trace_record_id,
                "independent_evidence_digest": evidence_validation["digest"],
                "independent_evidence_verifier": evidence_validation["verifier"],
            },
            metrics=metrics,
            error_taxonomy={} if verdict == "pass" else {"reason": str(execution.get("error") or verdict)},
            started_at=started_at,
            finished_at=_utc_now(),
            scope=capability_scope,
        )
        from eimemory.capabilities.observations import CapabilityObservations

        observation_result = CapabilityObservations(runtime.store).record_evaluation_run(
            run,
            runtime_scope=scope,
            profile_id=target.profile_id or None,
            request_key=f"evaluation-run:{run.idempotency_key}",
        )
        return {
            "spec": spec,
            "run": run,
            "spec_receipt": spec_receipt,
            "run_receipt": observation_result.evaluation_run,
            "observation_receipt": observation_result.observation,
            "independent_evidence": evidence_validation,
        }

    def execute_and_persist(
        self,
        runtime: Any,
        *,
        artifact: Mapping[str, Any],
        target: EvaluationTarget | Mapping[str, Any],
        runtime_scope: ScopeRef | Mapping[str, Any],
        capability_scope: str,
        evidence_ref: str,
        probe_id: str,
        trace_record_id: str,
        execution_id: str,
    ) -> dict[str, Any]:
        """Execute one catalog case and persist its spec/run against evidence.

        Callers supply probe and trace IDs produced by an independent evidence
        path.  This method will not manufacture those IDs, so an executor or
        grader cannot bless its own evaluation result.
        """

        execution = self.execute(artifact, runtime=runtime, evidence_ref=evidence_ref)
        stored = self.persist_spec_and_run(
            runtime,
            artifact=artifact,
            target=target,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            execution=execution,
            probe_id=probe_id,
            trace_record_id=trace_record_id,
            execution_id=execution_id,
        )
        return {"execution": execution, **stored}

    def publish_into(self, destination: "CapabilityEvaluationCatalog") -> None:
        """Publish immutable descriptors/registrations into another trusted catalog."""

        for registration in self.graders.registrations():
            destination.graders.register(
                grader_id=registration.grader_id,
                grader_type=registration.grader_type,
                revision=registration.revision,
                handler=registration.handler,
            )
        for registration in self._executors.values():
            destination.register_executor(
                executor_id=registration.executor_id,
                revision=registration.revision,
                handler=registration.handler,
            )
        for case in self._cases.values():
            destination.register_case(case)


def _validate_independent_evidence_chain(
    runtime: Any,
    *,
    scope: ScopeRef,
    probe_id: str,
    trace_record_id: str,
    capability_id: str,
    capability_revision_id: str,
    provider_binding_id: str,
) -> dict[str, Any]:
    """Verify a durable evidence chain before catalog code writes a result.

    The catalog itself must never be the only attester of a result it has just
    executed.  This accepts the existing durable probe/outcome-trace pattern
    and future independently produced records, but rejects missing, inactive,
    cross-target, or self-authored evidence.  The exact source implementation
    is descriptive; the explicit linkage and independent verifier contract are
    the authority.
    """

    getter = getattr(getattr(runtime, "store", None), "get_by_id", None)
    if not callable(getter):
        raise CatalogResolutionError("runtime evidence store is unavailable")
    probe = getter(probe_id, scope=scope)
    trace = getter(trace_record_id, scope=scope)
    if probe is None or trace is None:
        raise CatalogResolutionError("independent evidence records are not durable")
    if probe_id == trace_record_id:
        raise CatalogResolutionError("probe and trace evidence must be distinct records")
    if str(getattr(probe, "status", "") or "") != "active" or str(getattr(trace, "status", "") or "") != "active":
        raise CatalogResolutionError("independent evidence records must be active")
    if str(getattr(probe, "source", "") or "") == "eimemory.evaluation.catalog" or str(getattr(trace, "source", "") or "") == "eimemory.evaluation.catalog":
        raise CatalogResolutionError("catalog-generated evidence cannot independently attest a catalog run")
    probe_content = getattr(probe, "content", None)
    probe_content = probe_content if isinstance(probe_content, Mapping) else {}
    trace_content = getattr(trace, "content", None)
    trace_content = trace_content if isinstance(trace_content, Mapping) else {}
    trace_payload = trace_content.get("payload") if isinstance(trace_content.get("payload"), Mapping) else trace_content
    probe_revision = str(probe_content.get("capability_revision_id") or "")
    probe_binding = str(probe_content.get("provider_binding_id") or "")
    trace_revision = str(trace_payload.get("capability_revision_id") or "")
    trace_binding = str(trace_payload.get("provider_binding_id") or "")
    trace_capability = str(trace_payload.get("capability") or "")
    if (probe_revision and probe_revision != capability_revision_id) or (
        probe_binding and probe_binding != provider_binding_id
    ):
        raise CatalogResolutionError("probe evidence target does not match evaluation target")
    if (
        trace_capability != capability_id
        or trace_revision != capability_revision_id
        or trace_binding != provider_binding_id
    ):
        raise CatalogResolutionError("trace evidence target does not match evaluation target")
    verifier = trace_payload.get("verifier") if isinstance(trace_payload.get("verifier"), Mapping) else {}
    if verifier.get("independent") is not True:
        raise CatalogResolutionError("trace lacks an explicit independent verifier")
    source_ref = str(verifier.get("evidence_ref") or "")
    trace_contract = trace_payload.get("capability_contract") if isinstance(trace_payload.get("capability_contract"), Mapping) else {}
    source_records = trace_contract.get("source_record_ids")
    if source_ref != probe_id or not isinstance(source_records, Sequence) or isinstance(source_records, (str, bytes)) or list(source_records) != [probe_id]:
        raise CatalogResolutionError("trace verifier does not bind exactly to the probe evidence")
    verifier_id = str(verifier.get("id") or verifier.get("method") or "").strip()
    verifier_revision = str(verifier.get("revision") or verifier.get("schema_version") or "").strip()
    verifier_digest = str(verifier.get("contract_digest") or verifier.get("artifact_digest") or "").strip()
    if not verifier_id or not verifier_revision or not verifier_digest:
        raise CatalogResolutionError("independent verifier identity is incomplete")
    material = {
        "probe_id": probe_id,
        "trace_record_id": trace_record_id,
        "capability_id": capability_id,
        "capability_revision_id": capability_revision_id,
        "provider_binding_id": provider_binding_id,
        "verifier": {
            "id": verifier_id,
            "revision": verifier_revision,
            "contract_digest": verifier_digest,
        },
    }
    return {
        "probe_id": probe_id,
        "trace_record_id": trace_record_id,
        "verifier": material["verifier"],
        "digest": contract_digest(material),
    }


def _binding_matches_selector(binding: Mapping[str, Any], selector: Mapping[str, Any]) -> bool:
    if not selector:
        return True
    descriptor = binding.get("descriptor") if isinstance(binding.get("descriptor"), Mapping) else {}
    binding_id = str(binding.get("entity_id") or "")
    if "binding_ids" in selector and binding_id not in set(selector["binding_ids"]):
        return False
    if "provider_kind" in selector and str(descriptor.get("provider_kind") or "") != selector["provider_kind"]:
        return False
    if "provider_instance_id" in selector and str(descriptor.get("provider_instance_id") or "") != selector["provider_instance_id"]:
        return False
    if "operations_all" in selector:
        operations = {str(item) for item in descriptor.get("operations") or []}
        if not set(selector["operations_all"]).issubset(operations):
            return False
    return True


def _requested_case_ids(case_ids: Sequence[str] | None) -> set[str]:
    if case_ids is None:
        return set()
    result = {str(case_id or "").strip() for case_id in case_ids if str(case_id or "").strip()}
    if len(result) > 256:
        raise CatalogResolutionError("case_ids exceed 256 entries")
    return result


def _blocked_selection(reason: str, *, errors: Sequence[str] | None = None, detail: str = "", profile: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": False,
        "status": "blocked",
        "reason": reason,
        "errors": [str(error) for error in errors or ()],
        **({"detail": detail} if detail else {}),
        **({"profile": deepcopy(dict(profile))} if profile is not None else {}),
        "cases": [],
    }


def _failed_execution(case_id: str, reason: str, evidence_ref: str, *, case: CatalogCase | None = None) -> dict[str, Any]:
    result = {
        "case_id": str(case_id or ""),
        "capability": case.capability_id if case is not None else "",
        "executor_id": case.executor_id if case is not None else "",
        "executor_version": case.executor_revision if case is not None else "",
        "executor_contract_digest": case.executor_contract_digest if case is not None else "",
        "grader_id": case.grader_id if case is not None else "",
        "grader_revision": case.grader_revision if case is not None else "",
        "grader_type": case.grader_type if case is not None else "",
        "input": deepcopy(dict(case.input_data)) if case is not None else {},
        "output": {},
        "observation": {},
        "checks": [{"name": reason, "passed": False, "evidence_ref": evidence_ref}],
        "metrics": {"pass_rate": 0.0, "check_count": 1},
        "passed": False,
        "verdict": "blocked",
        "error": reason,
        "evaluation_case_digest": case.case_digest if case is not None else "",
        "eval_spec_id": case.eval_spec_id if case is not None else "",
    }
    result["execution_digest"] = execution_evidence_digest(
        executor_id=result["executor_id"],
        executor_version=result["executor_version"],
        input_data=result["input"],
        output=result["output"],
        observation=result["observation"],
        checks=result["checks"],
    )
    return result


CatalogBootstrapInstaller = Callable[["ApplicationCatalogBootstrap"], None]


class ApplicationCatalogBootstrap:
    """Narrow, code-only writer exposed during application catalog startup.

    The bootstrap object deliberately accepts typed descriptors and Python
    callables only.  It has no parser or persistence hook, so CLI values,
    adapter payloads, profile rows, and database documents cannot become
    executor/case registrations by flowing through this interface.
    """

    def __init__(self, catalog: CapabilityEvaluationCatalog) -> None:
        self._catalog = catalog

    def register_executor(
        self,
        *,
        executor_id: str,
        revision: str,
        handler: ProbeExecutor,
        contract_descriptor: Mapping[str, Any] | None = None,
    ) -> ExecutorRegistration:
        return self._catalog.register_executor(
            executor_id=executor_id,
            revision=revision,
            handler=handler,
            contract_descriptor=contract_descriptor,
        )

    def register_case(self, case: CatalogCase) -> CatalogCase:
        if not isinstance(case, CatalogCase):
            raise CatalogResolutionError("catalog_bootstrap_case_must_be_CatalogCase")
        return self._catalog.register_case(case)

    def register_cases(self, cases: Sequence[CatalogCase]) -> tuple[CatalogCase, ...]:
        if isinstance(cases, (str, bytes)) or not isinstance(cases, Sequence):
            raise CatalogResolutionError("catalog_bootstrap_cases_must_be_a_sequence_of_CatalogCase")
        return tuple(self.register_case(case) for case in cases)

    def register_grader(
        self,
        *,
        grader_id: str,
        grader_type: str,
        revision: str,
        handler: Callable[[dict[str, Any], tuple[dict[str, Any], ...], str, Mapping[str, Any]], Mapping[str, Any]],
    ) -> Any:
        return self._catalog.graders.register(
            grader_id=grader_id,
            grader_type=grader_type,
            revision=revision,
            handler=handler,
        )


_APPLICATION_CATALOG: CapabilityEvaluationCatalog | None = None
_APPLICATION_CATALOG_SOURCE = ""
_APPLICATION_CATALOG_LOCK = RLock()


def _normalize_catalog_source(source_id: str) -> str:
    try:
        return normalize_opaque_id(source_id, field="catalog_bootstrap_source")
    except CapabilityContractError as exc:
        raise CatalogResolutionError(str(exc)) from exc


def _validate_bootstrap_catalog(catalog: CapabilityEvaluationCatalog) -> None:
    """Reject a published catalog that cannot execute any declared case."""

    cases = catalog.list_cases()
    if not cases:
        raise CatalogResolutionError("catalog_bootstrap_empty")
    for case in cases:
        executor = catalog.describe_executor(case.executor_id)
        if executor is None:
            raise CatalogResolutionError(f"catalog_bootstrap_executor_missing:{case.executor_id}")
        if (
            executor["executor_revision"] != case.executor_revision
            or executor["executor_contract_digest"] != case.executor_contract_digest
        ):
            raise CatalogResolutionError(f"catalog_bootstrap_executor_contract_mismatch:{case.executor_id}")


def install_application_capability_catalog(
    catalog: CapabilityEvaluationCatalog,
    *,
    source_id: str,
) -> CapabilityEvaluationCatalog:
    """Publish one process-local application catalog built by trusted code.

    This accepts an already-constructed ``CapabilityEvaluationCatalog`` only;
    it neither reads mappings nor deserializes configuration.  Publishing seals
    the catalog so later request/adapter code cannot add executors, cases, or
    graders.  A process cannot silently replace an installed catalog.
    """

    if not isinstance(catalog, CapabilityEvaluationCatalog):
        raise CatalogResolutionError("catalog_bootstrap_requires_in_process_CapabilityEvaluationCatalog")
    normalized_source = _normalize_catalog_source(source_id)
    global _APPLICATION_CATALOG, _APPLICATION_CATALOG_SOURCE
    with _APPLICATION_CATALOG_LOCK:
        if _APPLICATION_CATALOG is not None:
            if _APPLICATION_CATALOG is catalog and _APPLICATION_CATALOG_SOURCE == normalized_source:
                return catalog
            raise CatalogResolutionError("catalog_already_configured")
        _validate_bootstrap_catalog(catalog)
        catalog.seal()
        _APPLICATION_CATALOG = catalog
        _APPLICATION_CATALOG_SOURCE = normalized_source
        return catalog


def bootstrap_application_capability_catalog(
    *,
    source_id: str,
    installers: Sequence[CatalogBootstrapInstaller],
) -> CapabilityEvaluationCatalog:
    """Build and publish an application catalog from trusted Python installers.

    ``installers`` are imported application/plugin functions, not declarative
    payloads.  Each receives only :class:`ApplicationCatalogBootstrap`, which
    requires a callable handler and a typed :class:`CatalogCase`.  Startup
    fails closed if any installer is not code, throws, or leaves an unusable
    catalog.
    """

    if isinstance(installers, (str, bytes)) or not isinstance(installers, Sequence) or not installers:
        raise CatalogResolutionError("catalog_bootstrap_installers_required")
    catalog = CapabilityEvaluationCatalog()
    bootstrap = ApplicationCatalogBootstrap(catalog)
    for installer in installers:
        if not callable(installer):
            raise CatalogResolutionError("catalog_bootstrap_installer_must_be_callable")
        try:
            installer(bootstrap)
        except CatalogResolutionError:
            raise
        except Exception as exc:
            raise CatalogResolutionError(
                f"catalog_bootstrap_installer_failed:{type(exc).__name__}"
            ) from exc
    return install_application_capability_catalog(catalog, source_id=source_id)


def application_capability_catalog() -> CapabilityEvaluationCatalog:
    """Return the configured process catalog or fail closed with a stable reason."""

    with _APPLICATION_CATALOG_LOCK:
        if _APPLICATION_CATALOG is None:
            raise CatalogResolutionError("catalog_not_configured")
        return _APPLICATION_CATALOG


def application_capability_catalog_status() -> dict[str, Any]:
    """Expose non-executable readiness metadata for health/readiness reports."""

    with _APPLICATION_CATALOG_LOCK:
        catalog = _APPLICATION_CATALOG
        if catalog is None:
            return {"configured": False, "reason": "catalog_not_configured"}
        return {
            "configured": True,
            "source_id": _APPLICATION_CATALOG_SOURCE,
            "sealed": catalog.sealed,
            "case_count": len(catalog.list_cases()),
            "executor_count": len(catalog._executors),
        }


def default_capability_catalog() -> CapabilityEvaluationCatalog:
    """Compatibility name for the explicitly configured application catalog.

    Historically this function created an empty implicit singleton.  That
    would make an unconfigured dynamic L5 path look valid, so it now preserves
    the exact fail-closed ``catalog_not_configured`` result instead.
    """

    return application_capability_catalog()


def resolve_application_capability_catalog(
    catalog: CapabilityEvaluationCatalog | None = None,
) -> CapabilityEvaluationCatalog:
    """Resolve only an in-process catalog authority.

    Profile records and adapter payloads are declarative data, never an
    executable catalog.  Dynamic consumers may either use the configured,
    sealed application catalog or receive an already-constructed catalog from
    trusted application code; mappings and duck-typed payloads are rejected.
    It does not deserialize, register, or otherwise promote caller payloads
    into executable descriptors.
    """

    if catalog is None:
        return application_capability_catalog()
    if not isinstance(catalog, CapabilityEvaluationCatalog):
        raise CatalogResolutionError("capability catalog must be an in-process CapabilityEvaluationCatalog")
    return catalog


__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "ApplicationCatalogBootstrap",
    "CatalogBootstrapInstaller",
    "CapabilityEvaluationCatalog",
    "CatalogCase",
    "CatalogResolutionError",
    "EvaluationTarget",
    "ExecutorRegistration",
    "default_capability_catalog",
    "application_capability_catalog",
    "application_capability_catalog_status",
    "bootstrap_application_capability_catalog",
    "resolve_application_capability_catalog",
    "install_application_capability_catalog",
    "execution_evidence_digest",
]
