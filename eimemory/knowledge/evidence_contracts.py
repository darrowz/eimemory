"""Pure evidence identity and confidence contracts shared by knowledge consumers.

These helpers classify evidence; they do not assign policy thresholds, resolve
storage, promote records, or replace missing confidence with inferred trust.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any


@dataclass(frozen=True, slots=True)
class ConfidenceAssessment:
    state: str  # "valid", "missing", or "invalid"
    value: float | None = None


def _container(record: Any, name: str) -> Mapping[str, Any]:
    value = record.get(name) if isinstance(record, Mapping) else getattr(record, name, None)
    return value if isinstance(value, Mapping) else {}


def declared_confidences(record: Any) -> tuple[object, ...]:
    """Preserve explicit null; only a genuinely absent field is omitted."""
    return tuple(
        container[key]
        for container, key in (
            (_container(record, "meta"), "reliability"),
            (_container(record, "meta"), "confidence"),
            (_container(record, "content"), "confidence"),
        )
        if key in container
    )


def assess_confidence_values(*values: object) -> ConfidenceAssessment:
    if not values:
        return ConfidenceAssessment("missing")
    numbers: list[float] = []
    for value in values:
        if value is None or isinstance(value, bool):
            return ConfidenceAssessment("invalid")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return ConfidenceAssessment("invalid")
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return ConfidenceAssessment("invalid")
        numbers.append(number)
    return ConfidenceAssessment("valid", min(numbers))


def assess_record_confidence(record: Any) -> ConfidenceAssessment:
    return assess_confidence_values(*declared_confidences(record))


def finite_confidence_floor(*values: object) -> float:
    """Compatibility numeric gate for projector v2; invalid/missing is zero."""
    assessment = assess_confidence_values(*values)
    return assessment.value if assessment.value is not None else 0.0


def record_version_digest(record: Any) -> str:
    """Hash the exact full envelope with the existing projector-v2 encoding."""
    payload = json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True,
                         default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def versioned_record_ref(record: Any) -> dict[str, Any]:
    return {
        "record_id": record.record_id,
        "kind": record.kind,
        "scope": {
            "tenant_id": record.scope.tenant_id,
            "agent_id": record.scope.agent_id,
            "workspace_id": record.scope.workspace_id,
            "user_id": record.scope.user_id,
        },
        "source_id": record.source_id,
        "version_digest": record_version_digest(record),
    }
