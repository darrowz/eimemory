"""Bounded deterministic and optional model graders.

The catalog may reference a grader only by an opaque identifier.  A grader
implementation is registered by trusted Python code, never supplied by a
stored evaluation descriptor.  This keeps the evaluation DSL declarative and
prevents a capability/profile selector from becoming an execution surface.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from eimemory.capabilities.contracts import (
    CapabilityContractError,
    contract_digest,
    normalize_json_payload,
    normalize_opaque_id,
)


SCHEMA_RULE_GRADER_ID = "eimemory.grader.schema-rule.v1"
SCHEMA_RULE_GRADER_REVISION = "v1"
_ALLOWED_GRADER_TYPES = frozenset({"code", "schema_rule", "model"})
_ALLOWED_RULE_OPS = frozenset({"eq", "min", "max", "nonempty", "one_of", "contains"})


class CapabilityGraderError(RuntimeError):
    """A grader registration or invocation is unsafe or unavailable."""


GraderHandler = Callable[[dict[str, Any], tuple[dict[str, Any], ...], str, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class GraderRegistration:
    grader_id: str
    grader_type: str
    revision: str
    contract_digest: str
    handler: GraderHandler


def _normalize_rule(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CapabilityGraderError("evaluation rule must be an object")
    try:
        normalized = normalize_json_payload(value, field="evaluation_rule", reject_executable=True)
        field = normalize_opaque_id(normalized.get("field"), field="evaluation_rule.field")
        operation = normalize_opaque_id(normalized.get("op", "eq"), field="evaluation_rule.op")
    except CapabilityContractError as exc:
        raise CapabilityGraderError(str(exc)) from exc
    if operation not in _ALLOWED_RULE_OPS:
        raise CapabilityGraderError(f"unsupported evaluation rule operation: {operation}")
    if operation not in {"nonempty"} and "value" not in normalized:
        raise CapabilityGraderError(f"evaluation rule {field}_{operation} requires value")
    value_copy = deepcopy(normalized.get("value"))
    if operation == "one_of":
        if not isinstance(value_copy, list) or not value_copy:
            raise CapabilityGraderError("evaluation rule one_of requires a non-empty value list")
    return {"field": field, "op": operation, **({"value": value_copy} if "value" in normalized else {})}


def normalize_rules(values: Sequence[object] | object) -> tuple[dict[str, Any], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise CapabilityGraderError("evaluation rules must be a sequence")
    if not values:
        raise CapabilityGraderError("evaluation rules must not be empty")
    if len(values) > 256:
        raise CapabilityGraderError("evaluation rules exceed 256 items")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in values:
        rule = _normalize_rule(raw)
        name = f"{rule['field']}_{rule['op']}"
        if name in seen:
            raise CapabilityGraderError(f"duplicate evaluation rule: {name}")
        seen.add(name)
        result.append(rule)
    return tuple(result)


def grade_schema_rules(
    output: Mapping[str, Any] | object,
    rules: Sequence[Mapping[str, Any]] | object,
    evidence_ref: str,
) -> dict[str, Any]:
    """Evaluate the small non-executable rule language deterministically."""

    if not isinstance(output, Mapping):
        return {
            "verdict": "fail",
            "checks": [{"name": "executor_output_object", "passed": False, "evidence_ref": evidence_ref}],
            "observation": {},
            "metrics": {"pass_rate": 0.0, "check_count": 1},
            "error": "executor output must be an object",
        }
    try:
        normalized_rules = normalize_rules(rules)
    except CapabilityGraderError as exc:
        return {
            "verdict": "blocked",
            "checks": [{"name": "valid_rules", "passed": False, "evidence_ref": evidence_ref}],
            "observation": {},
            "metrics": {"pass_rate": 0.0, "check_count": 1},
            "error": str(exc),
        }
    checks: list[dict[str, Any]] = []
    observation: dict[str, Any] = {}
    for rule in normalized_rules:
        field = str(rule["field"])
        operation = str(rule["op"])
        observed = deepcopy(output.get(field))
        expected = deepcopy(rule.get("value"))
        observation[field] = observed
        if operation == "eq":
            passed = observed == expected
        elif operation == "min":
            passed = (
                isinstance(observed, (int, float))
                and not isinstance(observed, bool)
                and isinstance(expected, (int, float))
                and not isinstance(expected, bool)
                and observed >= expected
            )
        elif operation == "max":
            passed = (
                isinstance(observed, (int, float))
                and not isinstance(observed, bool)
                and isinstance(expected, (int, float))
                and not isinstance(expected, bool)
                and observed <= expected
            )
        elif operation == "nonempty":
            passed = bool(observed)
        elif operation == "one_of":
            passed = observed in expected if isinstance(expected, list) else False
        elif operation == "contains":
            passed = expected in observed if isinstance(observed, (str, list, tuple, set, dict)) else False
        else:  # ``normalize_rules`` keeps this defensive path unreachable.
            passed = False
        checks.append(
            {
                "name": f"{field}_{operation}",
                "field": field,
                "operation": operation,
                "expected": expected,
                "observed": observed,
                "passed": bool(passed),
                "evidence_ref": evidence_ref,
            }
        )
    passed_count = sum(check["passed"] is True for check in checks)
    return {
        "verdict": "pass" if checks and passed_count == len(checks) else "fail",
        "checks": checks,
        "observation": observation,
        "metrics": {
            "pass_rate": round(passed_count / len(checks), 6) if checks else 0.0,
            "check_count": len(checks),
        },
        "error": "" if checks and passed_count == len(checks) else "schema-rule check failed",
    }


class CapabilityGraderRegistry:
    """In-process allowlist of trusted evaluators.

    Registration is deliberately explicit.  An ``EvaluationSpec`` can choose
    only an existing opaque ID and cannot attach a command, callable, prompt,
    network endpoint, or selector expression.
    """

    def __init__(self) -> None:
        self._registrations: dict[str, GraderRegistration] = {}
        self._sealed = False
        self.register(
            grader_id=SCHEMA_RULE_GRADER_ID,
            grader_type="schema_rule",
            revision=SCHEMA_RULE_GRADER_REVISION,
            handler=lambda output, rules, evidence_ref, _context: grade_schema_rules(output, rules, evidence_ref),
        )

    @property
    def sealed(self) -> bool:
        """Whether trusted grader registration has been closed for this registry."""

        return self._sealed

    def seal(self) -> "CapabilityGraderRegistry":
        """Close the registry after trusted application bootstrap.

        Graders are executable code, so an application catalog must not permit
        a later adapter, stored descriptor, or request payload to add one.
        Directly constructed test/application registries can remain mutable
        until their owner explicitly seals them.
        """

        self._sealed = True
        return self

    def register(
        self,
        *,
        grader_id: str,
        grader_type: str,
        revision: str,
        handler: GraderHandler,
    ) -> GraderRegistration:
        if self._sealed:
            raise CapabilityGraderError("grader_registry_sealed")
        try:
            normalized_id = normalize_opaque_id(grader_id, field="grader_id")
            normalized_revision = normalize_opaque_id(revision, field="grader_revision")
        except CapabilityContractError as exc:
            raise CapabilityGraderError(str(exc)) from exc
        if grader_type not in _ALLOWED_GRADER_TYPES:
            raise CapabilityGraderError("grader_type must be code, schema_rule, or model")
        if not callable(handler):
            raise CapabilityGraderError("grader handler must be a trusted callable")
        registration = GraderRegistration(
            grader_id=normalized_id,
            grader_type=grader_type,
            revision=normalized_revision,
            contract_digest=contract_digest(
                {
                    "grader_id": normalized_id,
                    "grader_type": grader_type,
                    "revision": normalized_revision,
                }
            ),
            handler=handler,
        )
        existing = self._registrations.get(normalized_id)
        if existing is not None:
            if (
                existing.grader_type != registration.grader_type
                or existing.revision != registration.revision
                or existing.contract_digest != registration.contract_digest
            ):
                raise CapabilityGraderError(f"conflicting grader registration: {normalized_id}")
            return existing
        self._registrations[normalized_id] = registration
        return registration

    def describe(self, grader_id: str) -> dict[str, str] | None:
        registration = self._registrations.get(str(grader_id or "").strip())
        if registration is None:
            return None
        return {
            "grader_id": registration.grader_id,
            "grader_type": registration.grader_type,
            "grader_revision": registration.revision,
            "grader_contract_digest": registration.contract_digest,
        }

    def registrations(self) -> tuple[GraderRegistration, ...]:
        """Expose immutable registration metadata for trusted catalog publication."""

        return tuple(self._registrations[key] for key in sorted(self._registrations))

    def grade(
        self,
        *,
        grader_id: str,
        grader_type: str,
        output: Mapping[str, Any] | object,
        rules: Sequence[Mapping[str, Any]] | object,
        evidence_ref: str,
        context: Mapping[str, Any] | None = None,
        model_policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        registration = self._registrations.get(str(grader_id or "").strip())
        if registration is None:
            return _unavailable_grade("blocked", "grader_unavailable", evidence_ref)
        if registration.grader_type != grader_type:
            return _unavailable_grade("blocked", "grader_type_mismatch", evidence_ref)
        if grader_type == "model":
            policy = dict(model_policy or {})
            max_tokens = policy.get("max_tokens")
            if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or not 1 <= max_tokens <= 16_384:
                return _unavailable_grade("blocked", "model_grader_budget_invalid", evidence_ref)
            if policy.get("fail_closed") is not True and not str(policy.get("tie_breaker") or "").strip():
                return _unavailable_grade("blocked", "model_grader_fail_closed_policy_required", evidence_ref)
        try:
            safe_output = normalize_json_payload(dict(output) if isinstance(output, Mapping) else {}, field="grader_output", reject_executable=True)
            safe_context = normalize_json_payload(dict(context or {}), field="grader_context", reject_executable=True)
            result = registration.handler(safe_output, normalize_rules(rules), str(evidence_ref), safe_context)
        except (CapabilityContractError, CapabilityGraderError) as exc:
            return _unavailable_grade("blocked", type(exc).__name__, evidence_ref)
        except Exception as exc:  # Trusted handler failure must never become a pass.
            return _unavailable_grade("blocked", f"grader_exception:{type(exc).__name__}", evidence_ref)
        return _normalize_grade_result(result, registration=registration, evidence_ref=evidence_ref)


def _unavailable_grade(verdict: str, reason: str, evidence_ref: str) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "checks": [{"name": reason, "passed": False, "evidence_ref": evidence_ref}],
        "observation": {},
        "metrics": {"pass_rate": 0.0, "check_count": 1},
        "error": reason,
    }


def _normalize_grade_result(
    value: Mapping[str, Any] | object,
    *,
    registration: GraderRegistration,
    evidence_ref: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return _unavailable_grade("blocked", "grader_result_invalid", evidence_ref)
    verdict = str(value.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "fail", "blocked", "inconclusive"}:
        return _unavailable_grade("blocked", "grader_verdict_invalid", evidence_ref)
    checks = value.get("checks")
    observation = value.get("observation")
    metrics = value.get("metrics")
    if not isinstance(checks, list) or not checks or not isinstance(observation, Mapping) or not isinstance(metrics, Mapping):
        return _unavailable_grade("blocked", "grader_result_shape_invalid", evidence_ref)
    normalized_checks: list[dict[str, Any]] = []
    for raw in checks:
        if not isinstance(raw, Mapping) or not str(raw.get("name") or "").strip() or not isinstance(raw.get("passed"), bool):
            return _unavailable_grade("blocked", "grader_check_invalid", evidence_ref)
        check = deepcopy(dict(raw))
        if str(check.get("evidence_ref") or "") != evidence_ref:
            return _unavailable_grade("blocked", "grader_evidence_ref_mismatch", evidence_ref)
        normalized_checks.append(check)
    if verdict == "pass" and any(check["passed"] is not True for check in normalized_checks):
        return _unavailable_grade("blocked", "grader_pass_without_all_checks", evidence_ref)
    return {
        "verdict": verdict,
        "checks": normalized_checks,
        "observation": deepcopy(dict(observation)),
        "metrics": deepcopy(dict(metrics)),
        "error": str(value.get("error") or ""),
        "grader_id": registration.grader_id,
        "grader_revision": registration.revision,
        "grader_contract_digest": registration.contract_digest,
    }


__all__ = [
    "CapabilityGraderError",
    "CapabilityGraderRegistry",
    "GraderRegistration",
    "SCHEMA_RULE_GRADER_ID",
    "SCHEMA_RULE_GRADER_REVISION",
    "grade_schema_rules",
    "normalize_rules",
]
