"""Private, single-invocation observations of one authorized recall target.

The observer never supplies candidates or reads source records. Query hashes,
exact scopes and the engine/store identity exist only in the temporary frame.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from hashlib import sha256
from math import isfinite
from threading import get_ident
from typing import Any

from eimemory.models.records import RecordEnvelope
from eimemory.models.source_partitions import normalize_source_ids
from .contracts import CandidateHit, CandidateRef, CandidateRequest, ExactScope


SCHEMA = "controlled_target_stage_trace.v1"
STAGES = ("provider_returned", "provider_bounded", "hydrated", "filtered",
          "fusion_input", "selector_input", "selector_output", "final_items")
PRESENCES = frozenset({"present", "absent_in_observed_window", "unknown"})
STATUSES = frozenset({"not_observed", "binding_mismatch", "completed", "aborted"})
DROP_REASONS = frozenset({
    "provider_over_limit", "scope_not_allowed", "source_not_allowed",
    "missing_or_corrupt_record", "ref_mismatch", "candidate_projection_digest_mismatch",
    "inactive_record", "quality_rejected", "kind_not_allowed", "score",
    "top_score_margin", "grounding", "requested_attribute_missing",
})
MAX_INSPECTED_REFS = 5000
_armed: ContextVar[Any] = ContextVar("eimemory_armed_target_trace", default=None)
_active: ContextVar[Any] = ContextVar("eimemory_active_target_trace", default=None)


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def _number(value, *, fractional=False):
    if fractional:
        return (round(value, 12) if type(value) in (int, float) and 0 <= value <= 1
                and isfinite(value) else None)
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def safe_target_trace_report(value: Any) -> dict[str, Any]:
    """Fixed privacy/type projection, also used by the nightly summary."""
    value = value if isinstance(value, dict) and value.get("schema") == SCHEMA else {}
    rows = value.get("stages")
    rows = rows if isinstance(rows, list) else []
    by_stage = {row["stage"]: row for row in rows[:len(STAGES)]
                if isinstance(row, dict) and isinstance(row.get("stage"), str)
                and row["stage"] in STAGES}
    result = []
    for name in STAGES:
        row = by_stage.get(name, {})
        drops = row.get("drop_reasons")
        drops = drops if isinstance(drops, list) else []
        presence = row.get("target_presence")
        result.append({"stage": name,
            "target_presence": presence if isinstance(presence, str) and presence in PRESENCES else "unknown",
            "observed_count": _number(row.get("observed_count")),
            "inspected_count": _number(row.get("inspected_count")),
            "window_complete": row.get("window_complete") if type(row.get("window_complete")) is bool else None,
            "truncated": row.get("truncated") if type(row.get("truncated")) is bool else None,
            "drop_reasons": sorted({reason for reason in drops[:len(DROP_REASONS)]
                                    if isinstance(reason, str) and reason in DROP_REASONS}),
            "grounding_score": _number(row.get("grounding_score"), fractional=True),
            "grounding_threshold": _number(row.get("grounding_threshold"), fractional=True)})
    status = value.get("status")
    return {"schema": SCHEMA, "status": status if isinstance(status, str) and status in STATUSES else "not_observed",
            "does_not_certify_answer": True, "stages": result}


class _TargetTrace:
    def __init__(self, engine, target: CandidateRef, query: str, source_ids):
        self.engine, self.target = engine, target
        try:
            self.store = getattr(engine, "store", None)
        except Exception:
            self.engine, self.store = None, None
        self.query_digest = sha256(query.encode("utf-8")).digest()
        self.source_ids = normalize_source_ids(source_ids)
        self.thread, self.task = get_ident(), _task()
        self.claimed = False
        self.status = "not_observed"
        self.rows: dict[str, dict[str, Any]] = {}

    def owned(self) -> bool:
        return self.thread == get_ident() and self.task is _task()

    def matches(self, engine, request: CandidateRequest) -> bool:
        context = request.task_context_dict()
        return (self.owned() and self.engine is engine and self.store is getattr(engine, "store", None)
                and self.target.scope == request.scope and request.source_ids is not None
                and self.source_ids is not None and request.source_ids == self.source_ids
                and self.target.source_id in request.source_ids
                and request.target_source_id == self.target.source_id
                and (context.get("exact_scope_only") is True or context.get("scope_strategy") == "exact")
                and sha256(request.query.encode("utf-8")).digest() == self.query_digest)

    def _matches_ref(self, item) -> bool:
        if isinstance(item, CandidateHit):
            item = item.ref
        if isinstance(item, RecordEnvelope):
            return (item.record_id == self.target.record_id and item.source_id == self.target.source_id
                    and ExactScope.from_scope(item.scope) == self.target.scope)
        return isinstance(item, CandidateRef) and item == self.target

    def _row(self, stage):
        return self.rows.setdefault(stage, {"stage": stage, "target_presence": "unknown",
            "observed_count": None, "inspected_count": None, "window_complete": None,
            "truncated": None, "drop_reasons": [], "grounding_score": None, "grounding_threshold": None})

    def observe(self, stage, items, *, append=False, complete=True, truncated=False):
        """Only inspect a bounded existing concrete window; never consume iterators."""
        if stage not in STAGES or not self.owned():
            return
        try:
            row = self._row(stage)
            if not isinstance(items, (list, tuple)):
                row.update(target_presence="present" if row["target_presence"] == "present" else "unknown",
                           observed_count=None, window_complete=None, truncated=None,
                           _coverage_unknown=True, _count_unknown=True)
                return
            before = (row["inspected_count"] or 0) if append else 0
            limit = max(0, MAX_INSPECTED_REFS - before)
            inspected = items[:limit]
            found = any(self._matches_ref(item) for item in inspected)
            observed = ((row["observed_count"] or 0) if append else 0) + len(items)
            prior_found = append and row["target_presence"] == "present"
            covered = len(inspected) == len(items) and complete is True
            coverage_unknown = append and row.get("_coverage_unknown") is True
            count_unknown = (append and row.get("_count_unknown") is True) or observed > 1_000_000
            if append and row["window_complete"] is False:
                covered = False
            clipped = bool(truncated) or len(inspected) < len(items) or bool(append and row["truncated"])
            row.update(observed_count=observed if not count_unknown and not coverage_unknown else None,
                _count_unknown=count_unknown,
                inspected_count=before + len(inspected), window_complete=None if coverage_unknown else covered,
                truncated=True if clipped else None if coverage_unknown else False,
                target_presence="present" if found or prior_found
                    else "absent_in_observed_window" if covered and not coverage_unknown else "unknown")
        except Exception:
            # Diagnostic failure cannot change retrieval decisions.
            self._row(stage).update(target_presence="unknown", observed_count=None,
                                    window_complete=None, truncated=None,
                                    _coverage_unknown=True, _count_unknown=True)

    def finish(self, stage, *, complete):
        if not self.owned():
            return
        row = self.rows.get(stage)
        if row is not None and complete is False:
            row["window_complete"] = False
            if row["target_presence"] != "present":
                row["target_presence"] = "unknown"

    def drop(self, stage, item, reason):
        try:
            if self.owned() and stage in STAGES and reason in DROP_REASONS and self._matches_ref(item):
                row = self._row(stage)
                if reason not in row["drop_reasons"]:
                    row["drop_reasons"].append(reason)
        except Exception:
            pass

    def grounding(self, item, score, threshold):
        try:
            if self.owned() and self._matches_ref(item):
                row = self._row("selector_input")
                row.update(grounding_score=_number(score, fractional=True),
                           grounding_threshold=_number(threshold, fractional=True))
        except Exception:
            pass

    def report(self):
        return safe_target_trace_report({"schema": SCHEMA, "status": self.status,
                                         "stages": list(self.rows.values())})


@contextmanager
def target_trace_scope(engine, target: CandidateRef, *, query: str, source_ids):
    try:
        trace = _TargetTrace(engine, target, query, source_ids)
    except Exception:
        class UnobservedTrace:
            def report(self):
                return safe_target_trace_report(None)
        trace = UnobservedTrace()
    token = _armed.set(trace if isinstance(trace, _TargetTrace) else None)
    active_token = _active.set(None)
    try:
        yield trace
    finally:
        _active.reset(active_token)
        _armed.reset(token)


@contextmanager
def native_target_trace_scope(engine, request: CandidateRequest):
    armed, trace = _armed.get(), None
    if (isinstance(armed, _TargetTrace) and armed.engine is not None and armed.store is not None
            and not armed.claimed and armed.owned()):
        armed.claimed = True
        try:
            if armed.matches(engine, request):
                trace = armed
            else:
                armed.status = "binding_mismatch"
        except Exception:
            armed.status = "binding_mismatch"
    # Even a nested/mismatching invocation must hide its parent's active frame.
    token = _active.set(trace)
    try:
        yield
    except BaseException:
        if trace is not None:
            trace.status = "aborted"
        raise
    else:
        if trace is not None:
            trace.status = "completed"
    finally:
        _active.reset(token)


def current_target_trace(engine):
    trace = _active.get()
    return trace if isinstance(trace, _TargetTrace) and trace.engine is engine and trace.owned() else None
