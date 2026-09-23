"""Request-local raw-recall authority and outbound rerank budgets."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps
from time import perf_counter
from typing import Any

from eimemory.contracts.recall_boundary import (
    authority_digest, bounded_deadline, exact_ref, finite_float, scope_tuple,
)


class RawRecallUnavailable(RuntimeError):
    """Raw search did not complete; it cannot certify an empty corpus."""


@dataclass
class _Boundary:
    exact_scope: tuple[str, str, str, str] | None
    source_ids: frozenset[str] | None
    deadline: float
    kinds: frozenset[str] | None = None
    snapshots: dict[tuple, str] = field(default_factory=dict)


_CURRENT: ContextVar[_Boundary | None] = ContextVar("eimemory_raw_boundary", default=None)


def raw_ref_allowed(record: Any) -> bool:
    from eimemory.metadata import business_metadata
    meta = record.get("meta") if isinstance(record, dict) else getattr(record, "meta", {})
    quality = business_metadata(meta or {}).get("quality")
    if isinstance(quality, dict) and quality.get("capture_decision") == "reject":
        return False
    state = _CURRENT.get()
    if state is None:
        return True
    key = exact_ref(record)
    kind = record.get("kind") if isinstance(record, dict) else getattr(record, "kind", None)
    return ((state.kinds is None or kind is None or kind in state.kinds)
            and perf_counter() < state.deadline and bool(key[0]) and bool(key[-1])
            and (state.exact_scope is None or key[1:5] == state.exact_scope)
            and (state.source_ids is None or key[-1] in state.source_ids))


def capture_raw_snapshot(record: Any) -> None:
    state = _CURRENT.get()
    if state is not None:
        state.snapshots[exact_ref(record)] = authority_digest(record)


@contextmanager
def raw_read_scope(store):
    state = _CURRENT.get()
    from eimemory.storage.recall_deadline import recall_read_scope
    with recall_read_scope(store, {"_recall_collection_deadline_monotonic": state.deadline if state else 0.0}):
        yield


def raw_request_boundary(search):
    @wraps(search)
    def guarded(store, **kwargs):
        from eimemory.models.records import ScopeRef
        context = dict(kwargs.get("task_context") or {})
        if context.get("_task_recall_mode"):
            return []  # Task evidence must pass the structured admission path.
        outer = _CURRENT.get()
        raw_kinds = context.get("kinds")
        kinds = frozenset(raw_kinds) if isinstance(raw_kinds, (list, tuple)) and raw_kinds else None
        exact = (context.get("exact_scope_only") is True
                 or str(context.get("scope_strategy") or "").strip().lower() == "exact")
        scope = kwargs.get("scope")
        scope = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        sources = kwargs.get("source_ids")
        sources = None if sources is None else frozenset(sources)
        deadline = bounded_deadline(context.get("_recall_deadline_monotonic"), started=perf_counter())
        exact_scope = scope_tuple(scope) if exact else None
        if outer:
            deadline = min(deadline, outer.deadline)
            if outer.exact_scope is not None:
                if exact_scope is not None and exact_scope != outer.exact_scope:
                    return []
                exact_scope = outer.exact_scope
            if outer.kinds is not None:
                kinds = outer.kinds if kinds is None else kinds & outer.kinds
            if outer.source_ids is not None:
                sources = outer.source_ids if sources is None else sources & outer.source_ids
        state = _Boundary(exact_scope, sources, deadline, kinds=kinds)
        token = _CURRENT.set(state)
        try:
            if perf_counter() >= deadline:
                raise RawRecallUnavailable("raw_recall_unavailable")
            if sources == frozenset():
                return []
            results = search(store, **kwargs)
            from eimemory.raw.retrieval import authoritative_raw_payload
            final = []
            changed = False
            for entry in results:
                if not isinstance(entry, dict) or not isinstance(entry.get("record"), dict):
                    continue
                payload = entry["record"]
                key = exact_ref(payload)
                if not raw_ref_allowed(payload) or key not in state.snapshots:
                    continue
                with raw_read_scope(store):
                    record = store.get_by_exact_ref(key[0], scope=ScopeRef.from_dict(payload["scope"]), source_id=key[-1])
                if (record is None or record.status != "active" or exact_ref(record) != key
                        or not raw_ref_allowed(record) or authority_digest(record) != state.snapshots[key]):
                    changed = True
                    continue
                # Never forward text returned by a candidate/rerank provider.
                final.append({**entry, "record": authoritative_raw_payload(record)})
            if changed or perf_counter() >= deadline:
                raise RawRecallUnavailable("raw_recall_unavailable")
            return final
        except TimeoutError:
            raise RawRecallUnavailable("raw_recall_unavailable") from None
        finally:
            _CURRENT.reset(token)
    return guarded


def raw_remaining_seconds(configured: Any, task_context: dict | None = None) -> float:
    now = perf_counter()
    state = _CURRENT.get()
    deadline = state.deadline if state else bounded_deadline(
        (task_context or {}).get("_recall_deadline_monotonic"), started=now)
    return max(0.0, min(finite_float(configured, 3.0), 3.0, deadline - now))


def invoke_raw_reranker(reranker, ranked, *, query, task_context, limit, candidate_count):
    """Choose a compatible signature BEFORE invoking a side-effecting callback."""
    from inspect import signature
    full = dict(query=query, task_context=task_context, limit=limit, candidate_count=candidate_count)
    try:
        sig = signature(reranker)
    except (TypeError, ValueError):
        return reranker(ranked, **full)
    try:
        sig.bind(ranked, **full)
    except TypeError:
        legacy = dict(query=query, candidate_count=candidate_count)
        sig.bind(ranked, **legacy)
        return reranker(ranked, **legacy)
    return reranker(ranked, **full)


def guarded_raw_search(store, *, diagnostics, **kwargs):
    """Preserve an unavailable signal across the list-shaped legacy raw API."""
    try:
        from eimemory.raw.retrieval import search_raw_chunks
        return search_raw_chunks(store, **kwargs)
    except Exception:
        diagnostics["raw_collection_unavailable"] += 1
        return []
