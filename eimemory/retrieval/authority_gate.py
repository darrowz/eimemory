"""Authority checks shared by every recall selector return path.

This establishes a read-time authority boundary, not a transaction lasting until
an external consumer uses the memory. Callers needing that guarantee need a
versioned receipt and another check at the effect owner.
"""
from __future__ import annotations

from collections import Counter
from functools import wraps
from itertools import islice
from time import perf_counter
from typing import Any

from eimemory.contracts.recall_boundary import (
    authority_digest, bounded_deadline, exact_ref, normalize_retrieval_state, scope_tuple,
)
from eimemory.core.budgets import recall_budget_seconds
from eimemory.metadata import business_metadata
from eimemory.models.identity_aliases import normalize_identity_text


def enforce_selection_authority(select):
    """Filter before scoring, then batch-read again after any external verifier."""
    @wraps(select)
    def guarded(self, items, **kwargs):
        original = list(islice(iter(items or ()), 5000))
        deadline = bounded_deadline(
            kwargs.get("deadline_at"), started=perf_counter(), seconds=recall_budget_seconds(),
        )
        kwargs["deadline_at"] = deadline
        dropped: Counter[str] = Counter()
        initial_check = kwargs.get("validate")
        snapshots: dict[Any, str] = {}
        accepted = []
        try:
            # The callback supplied by _recall carries the request's exact
            # scope/source authorization; a fresh read alone is not permission.
            if initial_check is None:
                initial_check = lambda _item: True
            for item in original:
                if perf_counter() >= deadline:
                    dropped["selection_deadline_exceeded"] += 1
                    break
                key = self._record_key(item)
                if key in snapshots:
                    continue
                if getattr(item, "status", None) != "active" or initial_check(item) is not True:
                    dropped["authority_changed"] += 1
                    continue
                snapshots[key] = authority_digest(item)
                accepted.append(item)
            fresh_before = self._hydrate_records_batch(accepted, deadline_at=deadline)
            checked = []
            for item in accepted:
                row = fresh_before.get(self._record_key(item))
                if (row is None or row.status != "active"
                        or authority_digest(row) != snapshots[self._record_key(item)]):
                    dropped["authority_changed"] += 1
                    continue
                checked.append(item)
            accepted = checked
            snapshots = {self._record_key(item): snapshots[self._record_key(item)] for item in accepted}
        except Exception:
            return [], _unavailable(len(original), dropped, "authority_unavailable")

        if perf_counter() >= deadline:
            return [], _unavailable(len(original), dropped, "selection_deadline_exceeded")
        if not accepted and dropped:
            return [], _unavailable(len(original), dropped, "authority_changed")
        # Pool membership is useful for diagnostics, but a sibling's identity
        # does not prove that the representative itself answers the query.
        fusion = dict(kwargs.get("fusion_state") or {})
        fusion["pool_members"] = {self._record_key(item): [item] for item in accepted}
        kwargs["fusion_state"] = fusion
        def check_snapshot(item):
            key = self._record_key(item)
            return (key in snapshots and getattr(item, "status", None) == "active"
                    and authority_digest(item) == snapshots[key]
                    and initial_check(item) is True)
        kwargs["validate"] = check_snapshot
        try:
            chosen, state = select(self, accepted, **kwargs)
            chosen = list(islice(iter(chosen or ()), max(0, min(1000, int(kwargs.get("limit", 0))))))
            state = dict(state or {})
        except Exception:
            return [], _unavailable(len(original), dropped, "selection_unavailable")
        final = []
        # Retrieval bounds admission to the verifier; the verifier has its own
        # completion timeout. Once it finishes, give the mandatory fresh read
        # one bounded window rather than silently dropping a finished verdict.
        assistance = state.get("caller_assistance") or {}
        if (isinstance(assistance, dict) and assistance.get("calls") == 1
                and assistance.get("status") in {"evidence_found", "no_evidence"}
                and state.get("status") in {"evidence_found", "no_evidence"}):
            deadline = bounded_deadline(
                None, started=perf_counter(), seconds=recall_budget_seconds(),
            )
        try:
            if perf_counter() >= deadline:
                return [], _unavailable(len(original), dropped, "selection_deadline_exceeded", state)
            # This must be a NEW authority read. Reusing _recall's cached
            # _hydrated_for_validate misses writes made during a remote call.
            rows = self._hydrate_records_batch(chosen, deadline_at=deadline)
            seen = set()
            for item in chosen:
                key = self._record_key(item)
                row = rows.get(key)
                before = snapshots.get(key)
                if (before is None or row is None or row.status != "active"
                        or authority_digest(item) != before or authority_digest(row) != before):
                    dropped["authority_changed"] += 1
                    continue
                if key not in seen:
                    seen.add(key)
                    final.append(item)
        except Exception:
            return [], _unavailable(len(original), dropped, "authority_unavailable", state)
        if perf_counter() >= deadline:
            return [], _unavailable(len(original), dropped, "selection_deadline_exceeded", state)
        reasons = Counter(state.get("dropped_reasons") or {})
        reasons.update(dropped)
        state.update(input_count=len(original), selected_count=len(final),
                     dropped_count=max(0, len(original) - len(final)),
                     dropped_reasons=dict(sorted(reasons.items())))
        return final, normalize_retrieval_state(state, selected_count=len(final), incomplete=bool(dropped))
    return guarded


def _unavailable(count, dropped, reason, state=None):
    reasons = Counter((state or {}).get("dropped_reasons") or {})
    reasons.update(dropped)
    reasons[reason] = max(1, reasons.get(reason, 0))
    return {**(state or {}), "status": "unavailable", "collection_complete": False,
            "input_count": count, "selected_count": 0, "dropped_count": count,
            "dropped_reasons": dict(sorted(reasons.items()))}


def authoritative_identity_exists(engine, *, query, request, target_source_id,
                                  deadline_at=0.0) -> bool | None:
    """Unique identity in the requested exact partition; None means unavailable."""
    if request.source_ids is not None and target_source_id not in request.source_ids:
        return None
    store = engine.store
    lookup = getattr(store, "search_identity_candidates", None)
    if not callable(lookup):
        lookup = getattr(getattr(store, "sqlite", None), "search_identity_candidates", None)
    get_exact = getattr(store, "get_by_exact_ref", None)
    stale = getattr(engine._callbacks, "_is_temporally_stale_memory", None)
    if not all(callable(fn) for fn in (lookup, get_exact, stale)):
        return None
    normalized = normalize_identity_text(query)
    if not normalized:
        return False
    deadline = bounded_deadline(deadline_at, started=perf_counter(), seconds=recall_budget_seconds())
    wanted_scope = request.scope.to_scope_ref()
    verified = set()
    try:
        with engine._local_read_scope(deadline):
            rows = lookup(query=query, kinds=list(request.kinds) or None, scope=wanted_scope,
                          limit=8, source_ids=[target_source_id],
                          recall_filters={"_exact_scope": True,
                              "_recall_collection_deadline_monotonic": deadline})
            for row in islice(iter(rows or ()), 8):
                if perf_counter() >= deadline:
                    return None
                if (not isinstance(row, dict) or not isinstance(row.get("scope"), dict)
                        or not all(key in row["scope"] for key in ("tenant_id", "agent_id", "workspace_id", "user_id"))):
                    continue
                if (not row.get("record_id") or row.get("source_id") != target_source_id
                        or scope_tuple(row["scope"]) != scope_tuple(wanted_scope)):
                    continue
                evidence = set(row.get("evidence") or ())
                if not evidence & {"exact_title", "alias_hit"}:
                    continue
                record = get_exact(str(row["record_id"]), scope=wanted_scope, source_id=target_source_id)
                if (record is None or record.status != "active" or exact_ref(record) != exact_ref(row)
                        or (request.kinds and record.kind not in request.kinds)):
                    continue
                quality = business_metadata(record.meta).get("quality")
                if isinstance(quality, dict) and quality.get("capture_decision") == "reject":
                    continue
                if stale(record):
                    continue
                if (("exact_title" in evidence and normalize_identity_text(record.title) == normalized)
                        or ("alias_hit" in evidence and normalized in record.aliases)):
                    verified.add(exact_ref(record))
    except Exception:
        return None
    if perf_counter() >= deadline:
        return None
    return len(verified) == 1


def revalidate_auxiliary_outputs(engine, records, *, snapshots, authorized, deadline_at):
    """Fresh batch check for rules/raw evidence, not just selected main items."""
    if not records:
        return [], 0
    try:
        rows = engine._hydrate_records_batch(records, deadline_at=deadline_at)
        result = []
        for record in records:
            key = engine._record_key(record)
            current = rows.get(key)
            digest = snapshots.get(key)
            if (digest is not None and authorized(record) is True
                    and current is not None and current.status == "active"
                    and authority_digest(record) == digest and authority_digest(current) == digest):
                result.append(record)
        if deadline_at and perf_counter() >= deadline_at:
            return [], len(records)
        return result, len(records) - len(result)
    except Exception:
        return [], len(records)
