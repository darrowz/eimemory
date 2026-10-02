"""Capability discovery, reuse, and trend. None of these block release closure.

A collected capability is discovered immediately. One verified use with a
terminal receipt counts as reuse. A later use is compared with the previous
use. Missing reuse, a short sample, or a pass rate under the inspection line
is a state or an alert, not a closure failure. An unreported or malformed
collection still fails closed, because that is missing evidence, not a new
capability.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


INSPECTION_PASS_RATE = 0.8
COLLECTION_UNREPORTED = "capability_collection_unreported"
REUSE_EVIDENCE_MALFORMED = "capability_reuse_evidence_malformed"
PASS_RATE_ALERT = "verified_real_task_pass_rate_below_inspection_threshold"

DISCOVERED = "discovered"
REUSED = "reused"
NOT_REUSED = "not_reused"
IMPROVING = "improving"
STABLE = "stable"
REGRESSING = "regressing"
INSUFFICIENT_HISTORY = "insufficient_history"


def _enabled_types(value: object) -> tuple[frozenset[str] | None, str]:
    if value is None:
        return None, COLLECTION_UNREPORTED
    if not isinstance(value, (list, tuple, set, frozenset)):
        return None, COLLECTION_UNREPORTED
    types = [str(item).strip() for item in value]
    if any(not item for item in types) or len(set(types)) != len(types):
        return None, REUSE_EVIDENCE_MALFORMED
    return frozenset(types), ""


def _counts(value: object) -> dict[str, int] | None:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return None
    counts: dict[str, int] = {}
    for key, raw in value.items():
        task_type = str(key).strip()
        if not task_type or type(raw) is not int or raw < 0:
            return None
        counts[task_type] = raw
    return counts


def _outcomes(value: object) -> dict[str, list[bool]] | None:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        return None
    outcomes: dict[str, list[bool]] = {}
    for key, raw in value.items():
        task_type = str(key).strip()
        if not task_type or not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return None
        verdicts: list[bool] = []
        for item in raw:
            if type(item) is not bool:
                return None
            verdicts.append(item)
        outcomes[task_type] = verdicts
    return outcomes


def _trend(verdicts: list[bool]) -> str:
    if len(verdicts) < 2:
        return INSUFFICIENT_HISTORY
    previous, latest = verdicts[-2], verdicts[-1]
    if latest is previous:
        return STABLE
    return IMPROVING if latest else REGRESSING


def assess_capability_lifecycle(
    enabled_types: object,
    per_type_sample_counts: object = None,
    per_type_outcomes: object = None,
) -> dict[str, Any]:
    """Record discovery, reuse, and adjacent-use trend. Never a closure verdict."""

    collected, collection_error = _enabled_types(enabled_types)
    counts = _counts(per_type_sample_counts)
    outcomes = _outcomes(per_type_outcomes)
    if collection_error or counts is None or outcomes is None or collected is None:
        return {
            "ok": False,
            "reason": collection_error or REUSE_EVIDENCE_MALFORMED,
            "capabilities": {},
            "inspection_alerts": [],
        }
    capabilities: dict[str, dict[str, object]] = {}
    verified_uses = 0
    passed_uses = 0
    for task_type in sorted(collected):
        verdicts = list(outcomes.get(task_type) or [])
        reuse_count = len(verdicts) if task_type in outcomes else int(counts.get(task_type, 0))
        verified_uses += reuse_count
        passed_uses += sum(1 for item in verdicts if item)
        capabilities[task_type] = {
            "state": REUSED if reuse_count else DISCOVERED,
            "reuse": REUSED if reuse_count else NOT_REUSED,
            "reuse_count": reuse_count,
            "trend": _trend(verdicts) if task_type in outcomes else INSUFFICIENT_HISTORY,
        }
    alerts: list[str] = []
    if verified_uses and task_type_outcomes_present(outcomes) and passed_uses / verified_uses < INSPECTION_PASS_RATE:
        alerts.append(PASS_RATE_ALERT)
    return {
        "ok": True,
        "reason": "",
        "capabilities": capabilities,
        "inspection_alerts": alerts,
    }


def task_type_outcomes_present(outcomes: dict[str, list[bool]]) -> bool:
    return any(outcomes.values())


def real_task_type_coverage_deficits(
    enabled_types: object,
    per_type_sample_counts: object,
) -> list[str]:
    """Return evidence defects only. Short samples are not defects."""

    collected, collection_error = _enabled_types(enabled_types)
    if collection_error:
        return [collection_error]
    if collected is None or _counts(per_type_sample_counts) is None:
        return [REUSE_EVIDENCE_MALFORMED]
    return []


def real_task_type_coverage_met(
    enabled_types: object,
    per_type_sample_counts: object,
) -> bool:
    return not real_task_type_coverage_deficits(enabled_types, per_type_sample_counts)


def pass_rate_inspection_alerts(pass_rate: object, *, sample_count: object) -> list[str]:
    if type(sample_count) is not int or sample_count <= 0:
        return []
    if isinstance(pass_rate, bool) or not isinstance(pass_rate, (int, float)):
        return []
    if pass_rate < INSPECTION_PASS_RATE:
        return [PASS_RATE_ALERT]
    return []
