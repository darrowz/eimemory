"""Pure recall-boundary primitives; no storage or integration dependencies."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from hashlib import sha256
from math import isfinite
from typing import Any
import json

SCOPE_FIELDS = ("tenant_id", "agent_id", "workspace_id", "user_id")
ExactRef = tuple[str, str, str, str, str, str]


def finite_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if isfinite(result) else default


def scope_tuple(scope: Any) -> tuple[str, str, str, str]:
    if isinstance(scope, Mapping):
        values = [scope.get(key) for key in SCOPE_FIELDS]
    else:
        values = [getattr(scope, key, None) for key in SCOPE_FIELDS]
    return (str(values[0] or "default"), *(str(value or "") for value in values[1:]))


def exact_ref(record: Any) -> ExactRef:
    def get(key: str) -> Any:
        return record.get(key) if isinstance(record, Mapping) else getattr(record, key, None)
    return (str(get("record_id") or ""), *scope_tuple(get("scope")), str(get("source_id") or ""))


def authority_digest(record: Any) -> str:
    # Do not retain a mutable record object as a pre-verification snapshot.
    payload = asdict(record) if is_dataclass(record) else record
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def bounded_deadline(value: Any, *, started: float, seconds: float = 3.0) -> float:
    supplied = finite_float(value)
    ceiling = started + seconds
    # A positive expired deadline stays expired; NaN/Inf cannot remove the cap.
    return min(supplied, ceiling) if supplied > 0.0 else ceiling


def bind_score_entries(records: list[Any], entries: Any) -> dict[ExactRef, dict[str, Any]]:
    """Join by the full reference; legacy ID-only rows require uniqueness.

    Partial/malformed explicit identities never fall back to an ID-only join.
    A duplicate score row is ambiguous even when it names the same reference.
    """
    records_by_id: dict[str, set[ExactRef]] = defaultdict(set)
    for record in records:
        key = exact_ref(record)
        records_by_id[key[0]].add(key)
    rows: dict[ExactRef, list[dict[str, Any]]] = defaultdict(list)
    legacy: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries or ():
        if not isinstance(entry, Mapping):
            continue
        rid = str(entry.get("record_id") or "")
        if not rid or rid not in records_by_id:
            continue
        if "scope" in entry or "source_id" in entry:
            scope = entry.get("scope")
            if (not isinstance(scope, Mapping) or not all(field in scope for field in SCOPE_FIELDS)
                    or not isinstance(entry.get("source_id"), str) or not entry["source_id"]):
                continue
            key = exact_ref(entry)
            if key in records_by_id[rid]:
                rows[key].append(dict(entry))
        else:
            legacy[rid].append(dict(entry))
    bound = {key: values[0] for key, values in rows.items() if len(values) == 1}
    for rid, values in legacy.items():
        refs = records_by_id[rid]
        if len(refs) == 1 and len(values) == 1:
            key = next(iter(refs))
            if key not in rows:
                bound[key] = values[0]
    return bound


_INCOMPLETE_REASONS = frozenset({
    "recall_budget_exhausted", "candidate_hydration_timeout", "candidate_collection_incomplete",
    "authority_unavailable", "authority_changed", "selection_deadline_exceeded",
})


def normalize_retrieval_state(state: Mapping[str, Any], *, selected_count: int,
                              incomplete: bool = False) -> dict[str, Any]:
    result = dict(state)
    reasons = result.get("dropped_reasons")
    reasons = reasons if isinstance(reasons, Mapping) else {}
    incomplete = (incomplete or result.get("collection_complete") is False
                  or result.get("status") in {"unavailable", "degraded"}
                  or any(reasons.get(key) for key in _INCOMPLETE_REASONS))
    status = result.get("status")
    if not status or status in {"evidence_found", "no_evidence"}:
        status = "evidence_found" if selected_count else "no_evidence"
    if incomplete:
        status = "degraded" if selected_count else "unavailable"
    result.update(status=status, selected_count=selected_count,
                  collection_complete=not incomplete)
    from eimemory.contracts.recall_evidence import invalidate_empty_selection
    return invalidate_empty_selection(result, selected_count=selected_count)


def source_collection_incomplete(reports: Any) -> bool:
    for report in reports or ():
        if not isinstance(report, Mapping):
            return True
        if report.get("retrieval_mode") == "deadline_exhausted":
            return True
        drops = report.get("drops")
        if isinstance(drops, Mapping) and any(drops.get(key) for key in _INCOMPLETE_REASONS):
            return True
        if report.get("status") in {"unavailable", "timeout", "error"} or report.get("error"):
            return True
    return False


def may_capture_recall_gap(state: Mapping[str, Any], *, selected_count: int,
                           now: float, deadline_at: float) -> bool:
    """A completed, explicit absence can produce a gap; failure/ambiguity cannot."""
    return (selected_count == 0 and state.get("status") == "no_evidence"
            and state.get("collection_complete") is True
            and not source_collection_incomplete([state])
            and (not deadline_at or now < deadline_at))


def intersect_requested_kinds(request_kinds: Any, context_kinds: Any) -> tuple[str, ...] | None:
    """None means no explicit filter; an empty tuple is a conflicting filter."""
    arms = []
    for value in (request_kinds, context_kinds):
        if not isinstance(value, (list, tuple)) or not value:
            continue
        arms.append(tuple(dict.fromkeys(str(item).strip() for item in value if isinstance(item, str) and item.strip())))
    if not arms:
        return None
    first = arms[0]
    return tuple(kind for kind in first if all(kind in arm for arm in arms[1:]))
