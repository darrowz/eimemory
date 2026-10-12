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
_source_active: ContextVar[Any] = ContextVar("eimemory_source_target_trace", default=None)
_storage_active: ContextVar[Any] = ContextVar("eimemory_sqlite_target_trace", default=None)
_hybrid_expected: ContextVar[Any] = ContextVar("eimemory_expected_hybrid_trace", default=None)
SOURCE_SCHEMA = "sqlite_target_source_trace.v1"
SOURCE_STAGES = ("identity_index_rows", "identity_verified", "hybrid_collected", "hybrid_capped",
                 "hybrid_joined", "scored", "ranked_selected", "score_ref_binding", "merged_hits", "returned_hits")
SOURCE_DROPS = DROP_REASONS | frozenset({"identity_unverified", "identity_authority_rejected",
    "identity_projection_rejected", "identity_return_limit", "anchor_reserve_swap",
    "projection_join_excluded", "corrupt_record", "projection_payload_mismatch", "request_scope_mismatch",
    "request_kind_mismatch", "request_source_mismatch", "legacy_lane_filtered", "legacy_visibility_filtered",
    "lexical_grounding_missing", "insufficient_lexical_grounding", "rank_limit", "source_return_limit",
    "inactive_or_superseded", "stale_rule", "stale_memory", "source:blocked", "source:not_allowed",
    "memory_type:not_allowed", "organ:not_allowed", "recall_lane:not_allowed"})


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
    report = {"schema": SCHEMA, "status": status if isinstance(status, str) and status in STATUSES else "not_observed",
              "does_not_certify_answer": True, "stages": result}
    if isinstance(value.get("source_trace"), dict) and value["source_trace"].get("schema") == SOURCE_SCHEMA:
        report["source_trace"] = safe_source_trace_report(value["source_trace"])
    return report


def safe_source_trace_report(value):
    value = value if isinstance(value, dict) and value.get("schema") == SOURCE_SCHEMA else {}
    rows = value.get("stages") if isinstance(value.get("stages"), list) else []
    by_stage = {row["stage"]: row for row in rows[:len(SOURCE_STAGES)]
                if isinstance(row, dict) and isinstance(row.get("stage"), str) and row["stage"] in SOURCE_STAGES}
    result = []
    for stage in SOURCE_STAGES:
        row = by_stage.get(stage, {})
        drops = row.get("drop_reasons") if isinstance(row.get("drop_reasons"), list) else []
        presence, binding = row.get("target_presence"), row.get("binding_status")
        result.append({"stage": stage,
            "target_presence": presence if isinstance(presence, str) and presence in PRESENCES else "unknown",
            **{key: _number(row.get(key)) for key in ("observed_count", "inspected_count", "source_rank")},
            **{key: row.get(key) if type(row.get(key)) is bool else None for key in ("window_complete", "truncated")},
            "drop_reasons": sorted({reason for reason in drops[:len(SOURCE_DROPS)]
                                    if isinstance(reason, str) and reason in SOURCE_DROPS}),
            "source_score": _number(row.get("source_score"), fractional=True),
            "binding_status": binding if isinstance(binding, str) and binding in {"bound", "not_bound"} else "unknown"})
    arms = value.get("collection_arms") if isinstance(value.get("collection_arms"), dict) else {}
    status = value.get("status")
    return {"schema": SOURCE_SCHEMA, "status": status if isinstance(status, str) and status in STATUSES else "not_observed",
            "does_not_certify_answer": True, "stages": result,
            "collection_arms": {arm: arms.get(arm) if isinstance(arms.get(arm), str)
                and arms[arm] in {"completed", "incomplete"} else "unknown"
                for arm in ("fts", "anchor", "lane_seed", "recent")}}


class _TargetTrace:
    _stages, _drop_reasons = STAGES, DROP_REASONS
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
        self.source_trace = None

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
        if stage not in self._stages or not self.owned():
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
            if self.owned() and stage in self._stages and reason in self._drop_reasons and self._matches_ref(item):
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
        report = {"schema": SCHEMA, "status": self.status, "stages": list(self.rows.values())}
        if self.source_trace is not None:
            report["source_trace"] = self.source_trace.report()
        return safe_target_trace_report(report)


class _SourceTrace(_TargetTrace):
    _stages, _drop_reasons = SOURCE_STAGES, SOURCE_DROPS

    def __init__(self, parent, source, request):
        self.parent, self.source, self.request = parent, source, request
        self.engine, self.store, self.target = parent.engine, parent.store, parent.target
        self.thread, self.task = parent.thread, parent.task
        self.rows, self.arms, self.status = {}, {}, "not_observed"
        self.target_key = None
        self.collection_seen, self.collection_unknown = False, False
        self.reader_claimed = False

    def _ref(self, row):
        if not hasattr(row, "keys"):
            return None
        keys = row.keys()
        if not all(key in keys for key in ("record_id", "source_id", "tenant_id", "agent_id", "workspace_id", "user_id")):
            # Identity rows use an explicit nested scope; never parse storage keys.
            if (isinstance(row, dict) and isinstance(row.get("scope"), dict)
                    and all(key in row["scope"] and isinstance(row["scope"][key], str)
                            for key in ("tenant_id", "agent_id", "workspace_id", "user_id"))
                    and isinstance(row.get("record_id"), str) and row["record_id"]
                    and isinstance(row.get("source_id"), str) and row["source_id"]):
                return CandidateRef(row["record_id"], ExactScope.from_scope(row["scope"]), row["source_id"])
            return None
        if (any(not isinstance(row[key], str) for key in (
                "record_id", "source_id", "tenant_id", "agent_id", "workspace_id", "user_id"))
                or not row["record_id"] or not row["source_id"]):
            return None
        return CandidateRef(row["record_id"], ExactScope(row["tenant_id"], row["agent_id"],
                            row["workspace_id"], row["user_id"]), row["source_id"])

    def observe_rows(self, stage, rows):
        if not isinstance(rows, (list, tuple)):
            self.observe(stage, None)
            return
        try:
            refs = [self._ref(row) for row in rows[:MAX_INSPECTED_REFS]]
            self.observe(stage, refs, complete=len(rows) <= MAX_INSPECTED_REFS,
                         truncated=len(rows) > MAX_INSPECTED_REFS)
            self._row(stage)["observed_count"] = len(rows) if len(rows) <= 1_000_000 else None
            if any(ref is None for ref in refs):
                self._unknown(stage)
        except Exception:
            self._unknown(stage)

    def _unknown(self, stage):
        row = self._row(stage)
        row.update(target_presence="present" if row["target_presence"] == "present" else "unknown",
                   window_complete=None, _coverage_unknown=True, _count_unknown=True)

    def collect(self, arm, rows):
        if arm not in {"fts", "anchor", "lane_seed", "recent"} or not self.owned():
            return
        self.collection_seen = True
        try:
            if not isinstance(rows, (list, tuple)):
                raise ValueError("unobserved_window")
            incomplete = len(rows) > MAX_INSPECTED_REFS
            for row in rows[:MAX_INSPECTED_REFS]:
                ref = self._ref(row)
                incomplete = incomplete or ref is None
                if ref is not None and self._matches_ref(ref):
                    key = row["storage_key"] if "storage_key" in row.keys() else None
                    if not isinstance(key, str):
                        incomplete = True
                    elif self.target_key is None or self.target_key == key:
                        self.target_key = key
                    else:
                        incomplete = True
            self.collection_unknown = self.collection_unknown or incomplete
            self.arms[arm] = "incomplete" if incomplete or self.arms.get(arm) == "incomplete" else "completed"
        except Exception:
            self.collection_unknown = True
            self.arms[arm] = "incomplete"

    def observe_keys(self, stage, keys):
        if not isinstance(keys, (list, tuple, dict)) or not self.owned():
            self._unknown(stage)
            return
        row = self._row(stage)
        complete = self.collection_seen and not self.collection_unknown and len(keys) <= MAX_INSPECTED_REFS
        window = keys if isinstance(keys, dict) else keys[:MAX_INSPECTED_REFS]
        present = self.target_key is not None and self.target_key in window
        row.update(target_presence="present" if present else "absent_in_observed_window" if complete else "unknown",
                   observed_count=len(keys) if len(keys) <= 1_000_000 else None,
                   inspected_count=(1 if self.target_key is not None else 0) if isinstance(keys, dict)
                       else min(len(keys), MAX_INSPECTED_REFS), window_complete=bool(complete),
                   truncated=len(keys) > MAX_INSPECTED_REFS)

    def drop_row(self, stage, row, reason):
        try:
            ref = self._ref(row)
            if ref is not None:
                self.drop(stage, ref, reason)
        except Exception:
            pass

    def score(self, stage, record, *, value=None, rank=None, binding=None):
        try:
            if self.owned() and self._matches_ref(record):
                row = self._row(stage)
                if value is not None:
                    row["source_score"] = _number(value, fractional=True)
                if rank is not None:
                    row["source_rank"] = _number(rank)
                if binding in {"bound", "not_bound"}:
                    row["binding_status"] = binding
        except Exception:
            pass

    def report(self):
        return safe_source_trace_report({"schema": SOURCE_SCHEMA, "status": self.status,
                                        "stages": list(self.rows.values()), "collection_arms": self.arms})


@contextmanager
def sqlite_source_trace_scope(source, request):
    parent, trace = _active.get(), None
    try:
        if (isinstance(parent, _TargetTrace) and parent.owned() and _source_active.get() is None
                and parent.source_trace is None and parent.engine.candidate_source is source
                and source.store is parent.store and parent.matches(parent.engine, request)):
            trace = _SourceTrace(parent, source, request)
            parent.source_trace = trace
    except Exception:
        trace = None
    source_token = _source_active.set(trace)
    storage_token = _storage_active.set((trace, source.store.sqlite) if trace is not None else None)
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
        _storage_active.reset(storage_token)
        _source_active.reset(source_token)


def current_source_trace(source):
    trace = _source_active.get()
    return trace if (isinstance(trace, _SourceTrace) and trace.source is source and trace.owned()
                     and _active.get() is trace.parent) else None


@contextmanager
def sqlite_hybrid_trace_scope(source, request, *, limit):
    trace = current_source_trace(source)
    expected = (trace, object(), tuple(request.kinds), limit) if trace is not None else None
    token = _hybrid_expected.set(expected)
    try:
        yield
    finally:
        _hybrid_expected.reset(token)


@contextmanager
def sqlite_reader_trace_scope(store, reader, *, query, scope, source_ids, kinds, limit):
    trace = _source_active.get()
    expected = _hybrid_expected.get()
    bound = None
    try:
        if (isinstance(trace, _SourceTrace) and trace.owned() and not trace.reader_claimed
                and _active.get() is trace.parent
                and isinstance(expected, tuple) and len(expected) == 4 and expected[0] is trace
                and tuple(kinds or ()) == expected[2] and limit == expected[3]
                and trace.store is store and query == trace.request.query
                and ExactScope.from_scope(scope) == trace.request.scope
                and normalize_source_ids(source_ids) == trace.request.source_ids):
            trace.reader_claimed = True
            bound = (trace, reader)
    except Exception:
        bound = None
    token = _storage_active.set(bound)
    expected_token = _hybrid_expected.set(None)
    try:
        yield
    finally:
        _hybrid_expected.reset(expected_token)
        _storage_active.reset(token)


def current_sqlite_trace(sqlite, *, query=None, scope=None, source_ids=None, kinds=None, verify_request=False):
    bound = _storage_active.get()
    if not (isinstance(bound, tuple) and len(bound) == 2 and bound[1] is sqlite
            and bound[0].owned() and _active.get() is bound[0].parent and _source_active.get() is bound[0]):
        return None
    if verify_request:
        try:
            request = bound[0].request
            if (query != request.query or ExactScope.from_scope(scope) != request.scope
                    or normalize_source_ids(source_ids) != request.source_ids or tuple(kinds or ()) != request.kinds):
                return None
        except Exception:
            return None
    return bound[0]


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
