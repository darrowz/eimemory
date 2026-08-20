"""Bounded, fail-closed reconstruction of the scoped L5 v3 entity graph.

The durable source for a historical v3 entity is its self-contained
``capability.audit.v1`` record.  This runner deliberately does *not* infer
definitions, revisions, relations, bindings, profiles, evaluations, or L5
state from prose, legacy scores, package versions, or machine fingerprints.
It replays only auditable entities in dependency order.  The earlier explicit
legacy-observation adapter remains as a final compatibility phase and still
requires a complete independent attribution.

Each exact runtime/capability scope has one durable state row.  Its phase and
phase-local cursor are checkpointed after successful batches only.  A failed
replay therefore leaves the cursor in place; a retry may repeat committed work
but repository request identities make that repeat idempotent.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import sqlite3
import time
from typing import Any

from eimemory.capabilities.models import CapabilityObservation
from eimemory.capabilities.observations import CapabilityObservations
from eimemory.capabilities.registry import CapabilityRegistryError, exact_runtime_scope
from eimemory.capabilities.contracts import (
    CapabilityContractError,
    normalize_opaque_id,
    normalize_sha256,
    require_timestamp,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.capability_store import CapabilityStoreError
from eimemory.storage.migrations.capability_v3 import (
    CAPABILITY_V3_BACKFILL_MIGRATION,
    CAPABILITY_V3_BACKFILL_CONTEXT_SCHEMA,
    capability_v3_backfill_state,
    is_capability_v3_schema_ready,
)
from eimemory.storage.runtime_store import _capability_audit_from_record


BACKFILL_SCHEMA = "capability.v3.backfill.v1"
DUAL_WRITE_SCHEMA = "capability.v3.dual_write_report.v1"
BACKFILL_PLAN_SCHEMA = "capability.v3.backfill.plan.v2"
BACKFILL_CURSOR_SCHEMA = "capability.v3.backfill.phase_cursor.v1"
_MAX_BATCH_SIZE = 2_000
_MAX_SECONDS = 60.0
_MAX_REASON_BUCKETS = 128
_MAX_RESULT_SAMPLES = 64


@dataclass(frozen=True, slots=True)
class _BackfillPhase:
    """One dependency-ordered, resumable source pass."""

    name: str
    entity_types: tuple[str, ...] = ()
    mode: str = "audit"


# One entity type per pass prevents a lexical record key from accidentally
# placing a child before its parent.  Relation state transitions share the
# relation pass because their CAS predecessor is ordering-sensitive.
_BACKFILL_PHASES: tuple[_BackfillPhase, ...] = (
    _BackfillPhase("audit_definition", ("definition",)),
    _BackfillPhase("audit_revision", ("revision",)),
    _BackfillPhase("audit_relation", ("relation", "lifecycle_transition")),
    _BackfillPhase("audit_binding", ("binding",)),
    _BackfillPhase("audit_advertisement", ("advertisement",)),
    _BackfillPhase("audit_profile", ("profile",)),
    _BackfillPhase("audit_evaluation_spec", ("evaluation_spec",)),
    _BackfillPhase("audit_evaluation_run", ("evaluation_run",)),
    _BackfillPhase("audit_observation", ("observation",)),
    _BackfillPhase("audit_knowledge_link", ("knowledge_link",)),
    _BackfillPhase("audit_snapshot", ("snapshot",)),
    _BackfillPhase("audit_assessment", ("assessment",)),
    _BackfillPhase("audit_lifecycle_transition", ("lifecycle_transition",)),
    _BackfillPhase("legacy_explicit_observation", mode="legacy_observation"),
)
_BACKFILL_PHASE_BY_NAME = {phase.name: phase for phase in _BACKFILL_PHASES}
_SUPPORTED_AUDIT_ENTITY_TYPES = frozenset(
    entity_type
    for phase in _BACKFILL_PHASES
    for entity_type in phase.entity_types
)


class CapabilityBackfillError(RuntimeError):
    """The v3 backfill could not safely advance its durable cursor."""


@dataclass(frozen=True, slots=True)
class BackfillRowResult:
    storage_key: str
    status: str
    reason: str
    observation_id: str = ""
    observation_digest: str = ""
    entity_type: str = ""
    entity_id: str = ""
    entity_digest: str = ""


def capability_v3_backfill_context(
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
) -> dict[str, Any]:
    """Return the exact durable cursor identity for one backfill stream.

    A storage root can contain multiple tenant/agent/workspace/user scopes.
    The cursor must therefore be scoped too; a global cursor would allow one
    caller to silently skip another caller's historical records.
    """

    try:
        scope = exact_runtime_scope(runtime_scope)
        normalized_capability_scope = normalize_opaque_id(
            capability_scope,
            field="capability_scope",
        )
    except (CapabilityContractError, CapabilityRegistryError, TypeError, ValueError) as exc:
        raise CapabilityBackfillError(str(exc)) from exc
    context = {
        "schema": CAPABILITY_V3_BACKFILL_CONTEXT_SCHEMA,
        "runtime_scope": _scope_dict(scope),
        "capability_scope": normalized_capability_scope,
    }
    context_digest = _digest(context)
    return {
        **context,
        "context_digest": context_digest,
        # Keep the migration family stable for deployment policy, while the
        # deterministic suffix partitions durable progress by exact scope.
        "context_migration_id": f"{CAPABILITY_V3_BACKFILL_MIGRATION}:{context_digest[:24]}",
    }


def capability_v3_backfill_plan() -> dict[str, Any]:
    """Return the immutable phase contract used to interpret durable state.

    The plan is stored beside each scoped cursor.  Changing its digest forces
    an idempotent restart at the definition phase instead of accidentally
    treating an old observation-only cursor as a complete entity graph.
    """

    payload = {
        "schema": BACKFILL_PLAN_SCHEMA,
        "source_schema": "capability.audit.v1",
        "cursor_schema": BACKFILL_CURSOR_SCHEMA,
        "phases": [
            {
                "name": phase.name,
                "mode": phase.mode,
                "entity_types": list(phase.entity_types),
            }
            for phase in _BACKFILL_PHASES
        ],
        "legacy_observation_policy": "complete_independent_attribution_only",
        "inference_policy": "no_text_or_legacy_score_entity_inference",
    }
    return {**payload, "digest": _digest(payload)}


def _plan_matches(state: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    return str(state.get("backfill_plan_digest") or "") == str(plan["digest"])


def _state_phase_stats(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = state.get("phase_stats_json")
    if isinstance(raw, Mapping):
        payload: object = raw
    else:
        try:
            payload = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(payload, Mapping):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for name, value in payload.items():
        if name not in _BACKFILL_PHASE_BY_NAME or not isinstance(value, Mapping):
            continue
        normalized[str(name)] = {
            "status": str(value.get("status") or "running"),
            "cursor": str(value.get("cursor") or ""),
            "rows_scanned": max(0, _safe_int(value.get("rows_scanned"))),
            "rows_written": max(0, _safe_int(value.get("rows_written"))),
            "rows_unmappable": max(0, _safe_int(value.get("rows_unmappable"))),
            "rows_ignored": max(0, _safe_int(value.get("rows_ignored"))),
            "source_digest": str(value.get("source_digest") or ""),
            "target_digest": str(value.get("target_digest") or ""),
            "updated_at": str(value.get("updated_at") or ""),
        }
    return normalized


def _safe_int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _full_migration_complete(state: Mapping[str, Any], plan: Mapping[str, Any]) -> bool:
    """Only a fully checkpointed current plan can make the terminal claim."""

    if not _plan_matches(state, plan):
        return False
    if str(state.get("status") or "") != "completed":
        return False
    if str(state.get("phase") or "") != "completed":
        return False
    phase_stats = _state_phase_stats(state)
    return all(
        str(phase_stats.get(phase.name, {}).get("status") or "") == "completed"
        for phase in _BACKFILL_PHASES
    )


def capability_v3_backfill_status(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
) -> dict[str, Any]:
    """Read one scoped backfill state without starting or resuming a batch."""

    try:
        context = capability_v3_backfill_context(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
        )
        conn = runtime.store.sqlite.conn
        schema_ready = is_capability_v3_schema_ready(conn)
        state = capability_v3_backfill_state(
            conn,
            migration_id=str(context["context_migration_id"]),
        )
        plan = capability_v3_backfill_plan()
    except Exception as exc:
        return _failure_report(
            status="blocked",
            reason="capability_v3_backfill_status_unavailable",
            error=exc,
        )
    status = str(state.get("status") or "not_installed")
    reason = ""
    if not schema_ready:
        reason = "capability_v3_schema_not_ready"
    elif status in {"not_installed", "not_scheduled"}:
        reason = "scoped_backfill_not_started"
    elif status == "failed":
        reason = "last_backfill_batch_failed"
    elif status == "blocked":
        reason = "backfill_blocked"
    complete = bool(schema_ready) and _full_migration_complete(state, plan)
    plan_current = _plan_matches(state, plan)
    if status not in {"not_installed", "not_scheduled"} and not plan_current:
        reason = "backfill_plan_upgrade_required"
    if status == "completed" and not complete:
        reason = "backfill_plan_incomplete_or_superseded"
    return {
        "schema": BACKFILL_SCHEMA,
        "ok": bool(schema_ready)
        and status in {"running", "completed"}
        and plan_current
        and (status != "completed" or complete),
        "status": status if schema_ready else "blocked",
        "reason": reason,
        "migration_id": CAPABILITY_V3_BACKFILL_MIGRATION,
        "context_migration_id": str(context["context_migration_id"]),
        "context": context,
        "state": state,
        "plan": plan,
        "plan_current": plan_current,
        "phase": str(state.get("phase") or "not_installed"),
        "phase_stats": _state_phase_stats(state),
        "coverage": _coverage(),
        "full_migration_complete": complete,
    }


def inspect_capability_v3_dual_write(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    limit: int = 200,
    cursor: str = "",
) -> dict[str, Any]:
    """Compare the central raw-outcome owner with its v3 observation write.

    This is a bounded, read-only page.  It never guesses an observation for an
    unclassified raw record and it never promotes or changes an L5 reader.
    A green page is not a whole-stream claim until ``complete`` is true.
    """

    try:
        context = capability_v3_backfill_context(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
        )
        bounded_limit = _bounded_dual_write_limit(limit)
        normalized_cursor = _bounded_cursor(cursor)
        conn = runtime.store.sqlite.conn
        if not is_capability_v3_schema_ready(conn):
            return {
                "schema": DUAL_WRITE_SCHEMA,
                "ok": False,
                "page_ok": False,
                "status": "blocked",
                "reason": "capability_v3_schema_not_ready",
                "context": context,
                "limit": bounded_limit,
                "cursor": normalized_cursor,
            }
        scope = exact_runtime_scope(runtime_scope)
        rows = _load_records(conn, scope=scope, cursor=normalized_cursor, limit=bounded_limit)
        eligible = 0
        aligned = 0
        missing: list[dict[str, str]] = []
        unmappable: list[BackfillRowResult] = []
        next_cursor = normalized_cursor
        for row in rows:
            next_cursor = str(row["storage_key"])
            row_result = _dual_write_row(
                conn,
                row=row,
                runtime_scope=scope,
                capability_scope=str(context["capability_scope"]),
            )
            if row_result.status == "unmappable":
                unmappable.append(row_result)
                continue
            # Capability audit records belong to the entity-graph replay
            # phases, not to the raw-outcome/observation dual-write stream.
            # Counting them as eligible drift would make a healthy scoped
            # store report a false missing destination for every audit record.
            if row_result.status == "ignored":
                continue
            eligible += 1
            if row_result.status == "aligned":
                aligned += 1
            else:
                missing.append(
                    {
                        "storage_key": row_result.storage_key,
                        "record_id": row_result.observation_id,
                        "reason": row_result.reason,
                    }
                )
        complete = len(rows) < bounded_limit
        page_ok = not missing
        return {
            "schema": DUAL_WRITE_SCHEMA,
            "ok": bool(page_ok and complete),
            "page_ok": page_ok,
            "status": "aligned" if page_ok else "drift",
            "reason": "dual_write_aligned" if page_ok else "dual_write_destination_missing",
            "context": context,
            "runtime_scope": dict(context["runtime_scope"]),
            "capability_scope": str(context["capability_scope"]),
            "limit": bounded_limit,
            "cursor": normalized_cursor,
            "next_cursor": next_cursor,
            "complete": complete,
            "source_total": _source_count(conn, scope=scope),
            "scanned": len(rows),
            "eligible": eligible,
            "aligned": aligned,
            "missing": len(missing),
            "missing_samples": missing[:_MAX_RESULT_SAMPLES],
            "omitted_missing_count": max(0, len(missing) - _MAX_RESULT_SAMPLES),
            "unmappable": len(unmappable),
            "unmappable_by_reason": _reason_counts(unmappable),
            "unmappable_samples": [_result_payload(item) for item in unmappable[:_MAX_RESULT_SAMPLES]],
            "coverage": _coverage(),
        }
    except Exception as exc:
        return {
            "schema": DUAL_WRITE_SCHEMA,
            "ok": False,
            "page_ok": False,
            "status": "failed",
            "reason": "capability_v3_dual_write_inspection_failed",
            "error": type(exc).__name__,
            "detail": str(exc)[:1_000],
            "limit": int(limit) if isinstance(limit, int) and not isinstance(limit, bool) else 0,
            "cursor": str(cursor)[:1_024],
        }


def run_capability_v3_backfill_batch(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    batch_size: int = 200,
    max_seconds: float = 10.0,
    raise_on_error: bool = False,
) -> dict[str, Any]:
    """Replay one bounded phase page for one exact runtime/capability scope.

    ``capability.audit.v1`` data is never converted by heuristic.  Each audit
    row is validated by the same parser used by RuntimeStore reconstruction,
    then replayed through the normal capability transaction boundary.  The
    phase order supplies the parent-before-child dependency order that a
    record storage key cannot provide by itself.
    """

    started = time.monotonic()
    context: dict[str, Any] = {}
    plan: Mapping[str, Any] = {}
    rows_limit = 0
    seconds_limit = 0.0
    results: list[BackfillRowResult] = []
    scope: ScopeRef | None = None
    previous: Mapping[str, Any] = {}
    active: Mapping[str, Any] = {}
    executed_phase: _BackfillPhase | None = None
    conn: Any = None
    try:
        context = capability_v3_backfill_context(
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
        )
        plan = capability_v3_backfill_plan()
        scope = exact_runtime_scope(runtime_scope)
        normalized_capability_scope = str(context["capability_scope"])
        rows_limit = _bounded_batch_size(batch_size)
        seconds_limit = _bounded_seconds(max_seconds)
        conn = runtime.store.sqlite.conn
        if not is_capability_v3_schema_ready(conn):
            return _blocked_report(
                context=context,
                plan=plan,
                batch_size=rows_limit,
                max_seconds=seconds_limit,
                reason="capability_v3_schema_not_ready",
            )
        previous = capability_v3_backfill_state(
            conn,
            migration_id=str(context["context_migration_id"]),
        )
        if _full_migration_complete(previous, plan):
            return _terminal_report(
                context=context,
                plan=plan,
                state=previous,
                batch_size=rows_limit,
                max_seconds=seconds_limit,
            )

        active = _begin_state(
            conn,
            previous=previous,
            context=context,
            plan=plan,
        )
        # Another worker may have completed the same exact scoped plan after
        # the optimistic state read above but before this runner obtained the
        # state-row write lock.  Return its durable terminal result rather
        # than trying to reopen or mark that successful run as failed.
        if _full_migration_complete(active, plan):
            return _terminal_report(
                context=context,
                plan=plan,
                state=active,
                batch_size=rows_limit,
                max_seconds=seconds_limit,
            )
        executed_phase = _active_phase(active, plan)
        cursor = str(active.get("cursor") or "")
        source_total = _source_count(conn, scope=scope)
        if executed_phase.mode == "audit":
            rows = _load_audit_phase_records(
                conn,
                scope=scope,
                cursor=cursor,
                phase=executed_phase,
                limit=rows_limit,
            )
            results, last_cursor, processed_all = _run_audit_phase(
                runtime,
                rows=rows,
                phase=executed_phase,
                initial_cursor=cursor,
                runtime_scope=scope,
                capability_scope=normalized_capability_scope,
                started=started,
                max_seconds=seconds_limit,
            )
        else:
            rows = _load_records(conn, scope=scope, cursor=cursor, limit=rows_limit)
            observations = CapabilityObservations(runtime.store)
            results, last_cursor, processed_all = _run_legacy_observation_phase(
                rows=rows,
                observations=observations,
                initial_cursor=cursor,
                runtime_scope=scope,
                capability_scope=normalized_capability_scope,
                started=started,
                max_seconds=seconds_limit,
            )

        phase_complete = bool(processed_all and len(rows) < rows_limit)
        next_phase = _next_phase(executed_phase) if phase_complete else executed_phase
        migration_complete = bool(phase_complete and next_phase is None)
        persisted_phase = "completed" if migration_complete else (
            next_phase.name if phase_complete and next_phase is not None else executed_phase.name
        )
        persisted_cursor = "" if phase_complete else last_cursor
        mapped = sum(1 for item in results if item.status == "mapped")
        skipped = sum(1 for item in results if item.status == "unmappable")
        source_digest = _fold_digest(str(active.get("source_digest") or ""), results)
        target_digest = _fold_digest(str(active.get("target_digest") or ""), results, target=True)
        duration_ms = _duration_ms(started)
        destination_total = _destination_count(
            conn,
            scope=scope,
            capability_scope=normalized_capability_scope,
        )
        destination_counts = _destination_entity_counts(
            conn,
            scope=scope,
            capability_scope=normalized_capability_scope,
        )
        phase_stats = _merge_phase_stats(
            active,
            phase=executed_phase,
            # The state cursor resets when switching phases, while the phase
            # ledger retains the terminal checkpoint for recovery evidence.
            cursor=last_cursor,
            results=results,
            source_digest=source_digest,
            target_digest=target_digest,
            completed=phase_complete,
        )
        batch_summary = _batch_summary(
            context=context,
            plan=plan,
            phase=executed_phase,
            next_phase=next_phase,
            results=results,
            source_total=source_total,
            destination_total=destination_total,
            destination_counts=destination_counts,
            duration_ms=duration_ms,
            complete=migration_complete,
            phase_complete=phase_complete,
        )
        updated = _write_state(
            conn,
            previous=active,
            migration_id=str(context["context_migration_id"]),
            context=context,
            plan=plan,
            phase_stats=phase_stats,
            cursor=persisted_cursor,
            scanned_delta=len(results),
            written_delta=mapped,
            skipped_delta=skipped,
            source_digest=source_digest,
            target_digest=target_digest,
            source_total=source_total,
            destination_total=destination_total,
            duration_ms=duration_ms,
            batch_summary=batch_summary,
            status="completed" if migration_complete else "running",
            phase=persisted_phase,
            last_error="",
            finished=migration_complete,
        )
        return _batch_report(
            context=context,
            plan=plan,
            state=updated,
            batch_size=rows_limit,
            max_seconds=seconds_limit,
            executed_phase=executed_phase,
            next_phase=next_phase,
            results=results,
            phase_complete=phase_complete,
            complete=migration_complete,
            duration_ms=duration_ms,
            source_total=source_total,
            destination_total=destination_total,
            destination_counts=destination_counts,
            reason="backfill_completed" if migration_complete else (
                "phase_completed" if phase_complete else "batch_progressed"
            ),
        )
    except Exception as exc:
        duration_ms = _duration_ms(started)
        updated: Mapping[str, Any] = active or previous
        if conn is not None and context and scope is not None and plan:
            try:
                active = capability_v3_backfill_state(
                    conn,
                    migration_id=str(context["context_migration_id"]),
                )
                if str(active.get("status") or "") != "not_installed":
                    source_total = _source_count(conn, scope=scope)
                    destination_total = _destination_count(
                        conn,
                        scope=scope,
                        capability_scope=str(context["capability_scope"]),
                    )
                    destination_counts = _destination_entity_counts(
                        conn,
                        scope=scope,
                        capability_scope=str(context["capability_scope"]),
                    )
                    failed_phase = (
                        executed_phase.name
                        if executed_phase is not None
                        else _state_phase_name(active, plan)
                    )
                    updated = _write_state(
                        conn,
                        previous=active,
                        migration_id=str(context["context_migration_id"]),
                        context=context,
                        plan=plan,
                        phase_stats=_state_phase_stats(active),
                        cursor=str(active.get("cursor") or ""),
                        scanned_delta=0,
                        written_delta=0,
                        skipped_delta=0,
                        source_digest=str(active.get("source_digest") or ""),
                        target_digest=str(active.get("target_digest") or ""),
                        source_total=source_total,
                        destination_total=destination_total,
                        duration_ms=duration_ms,
                        batch_summary=_batch_summary(
                            context=context,
                            plan=plan,
                            phase=_BACKFILL_PHASE_BY_NAME.get(failed_phase),
                            next_phase=None,
                            results=results,
                            source_total=source_total,
                            destination_total=destination_total,
                            destination_counts=destination_counts,
                            duration_ms=duration_ms,
                            complete=False,
                            phase_complete=False,
                            partial_failure=True,
                        ),
                        status="failed",
                        phase=failed_phase,
                        last_error=f"{type(exc).__name__}:{exc}",
                        finished=False,
                    )
            except Exception:
                # Preserve the original failure; a status-write failure is
                # deliberately surfaced in the returned reason below.
                updated = {**dict(previous), "status": "failed", "phase": "state_write_failed"}
        invalid_request = conn is None and isinstance(exc, CapabilityBackfillError)
        report = _failure_report(
            status="blocked" if invalid_request else "failed",
            reason="invalid_capability_v3_backfill_request" if invalid_request else "capability_v3_backfill_failed",
            error=exc,
            context=context,
            plan=plan,
            state=updated,
            batch_size=rows_limit,
            max_seconds=seconds_limit,
            results=results,
            duration_ms=duration_ms,
        )
        if raise_on_error:
            raise CapabilityBackfillError(
                f"capability v3 backfill failed before cursor advance: {type(exc).__name__}"
            ) from exc
        return report


def _active_phase(state: Mapping[str, Any], plan: Mapping[str, Any]) -> _BackfillPhase:
    if not _plan_matches(state, plan):
        raise CapabilityBackfillError("backfill state plan does not match the active entity-graph plan")
    phase_name = _state_phase_name(state, plan)
    if phase_name == "completed":
        raise CapabilityBackfillError("completed backfill state lacks a complete phase checkpoint")
    phase = _BACKFILL_PHASE_BY_NAME.get(phase_name)
    if phase is None:
        raise CapabilityBackfillError(f"unknown durable backfill phase: {phase_name}")
    return phase


def _state_phase_name(state: Mapping[str, Any], plan: Mapping[str, Any]) -> str:
    raw = str(state.get("phase") or "")
    if raw in _BACKFILL_PHASE_BY_NAME or raw == "completed":
        return raw
    if not _plan_matches(state, plan):
        return _BACKFILL_PHASES[0].name
    return raw or _BACKFILL_PHASES[0].name


def _next_phase(phase: _BackfillPhase) -> _BackfillPhase | None:
    for index, candidate in enumerate(_BACKFILL_PHASES):
        if candidate.name == phase.name:
            return _BACKFILL_PHASES[index + 1] if index + 1 < len(_BACKFILL_PHASES) else None
    raise CapabilityBackfillError(f"backfill phase is not in the active plan: {phase.name}")


def _decode_audit_cursor(cursor: str, *, phase: _BackfillPhase) -> tuple[str, str, str]:
    if not cursor:
        return "", "", ""
    try:
        payload = json.loads(cursor)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapabilityBackfillError("audit phase cursor is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise CapabilityBackfillError("audit phase cursor must be an object")
    if str(payload.get("schema") or "") != BACKFILL_CURSOR_SCHEMA:
        raise CapabilityBackfillError("audit phase cursor schema is unsupported")
    if str(payload.get("phase") or "") != phase.name:
        raise CapabilityBackfillError("audit phase cursor belongs to another phase")
    scan_rowid = payload.get("scan_rowid")
    if isinstance(scan_rowid, bool):
        raise CapabilityBackfillError("audit phase cursor is malformed")
    try:
        parsed_rowid = int(scan_rowid)
    except (TypeError, ValueError) as exc:
        raise CapabilityBackfillError("audit phase cursor is malformed") from exc
    if parsed_rowid < 0:
        raise CapabilityBackfillError("audit phase cursor is malformed")
    return str(parsed_rowid), "", ""


def _encode_audit_cursor(row: Any, *, phase: _BackfillPhase) -> str:
    return _canonical_json(
        {
            "schema": BACKFILL_CURSOR_SCHEMA,
            "phase": phase.name,
            "scan_rowid": int(row["scan_rowid"]),
            "record_id": str(row["record_id"] or ""),
            "storage_key": str(row["storage_key"] or ""),
        }
    )


def _load_audit_phase_records(
    conn: Any,
    *,
    scope: ScopeRef,
    cursor: str,
    phase: _BackfillPhase,
    limit: int,
) -> list[Any]:
    """Read one deterministic audit-source page without trusting key order."""

    scan_rowid_text, _record_id, _storage_key = _decode_audit_cursor(cursor, phase=phase)
    scan_rowid = int(scan_rowid_text or "0")
    return conn.execute(
        """
        SELECT rowid AS scan_rowid, storage_key, record_id, created_at, payload_json
        FROM records
        WHERE tenant_id = ?
          AND agent_id = ?
          AND workspace_id = ?
          AND user_id = ?
          AND kind = 'capability_audit'
          AND source = 'eimemory.capability.v3'
          AND rowid > ?
        ORDER BY rowid ASC
        LIMIT ?
        """,
        (
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            scan_rowid,
            limit,
        ),
    ).fetchall()


def _run_audit_phase(
    runtime: Any,
    *,
    rows: list[Any],
    phase: _BackfillPhase,
    initial_cursor: str,
    runtime_scope: ScopeRef,
    capability_scope: str,
    started: float,
    max_seconds: float,
) -> tuple[list[BackfillRowResult], str, bool]:
    results: list[BackfillRowResult] = []
    last_cursor = initial_cursor
    for row in rows:
        if time.monotonic() - started >= max_seconds:
            return results, last_cursor, False
        result = _backfill_audit_row(
            runtime,
            row=row,
            phase=phase,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
        )
        results.append(result)
        # Only a successful or explicitly ignored source record advances the
        # phase cursor.  A malformed/replay-failed audit raises above instead.
        last_cursor = _encode_audit_cursor(row, phase=phase)
    return results, last_cursor, True


def _run_legacy_observation_phase(
    *,
    rows: list[Any],
    observations: CapabilityObservations,
    initial_cursor: str,
    runtime_scope: ScopeRef,
    capability_scope: str,
    started: float,
    max_seconds: float,
) -> tuple[list[BackfillRowResult], str, bool]:
    results: list[BackfillRowResult] = []
    last_cursor = initial_cursor
    for row in rows:
        if time.monotonic() - started >= max_seconds:
            return results, last_cursor, False
        result = _backfill_row(
            row,
            observations=observations,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
        )
        results.append(result)
        last_cursor = str(row["storage_key"])
    return results, last_cursor, True


def _backfill_audit_row(
    runtime: Any,
    *,
    row: Any,
    phase: _BackfillPhase,
    runtime_scope: ScopeRef,
    capability_scope: str,
) -> BackfillRowResult:
    """Replay one structurally verified audit entity or explicitly ignore it."""

    storage_key = str(row["storage_key"])
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
        record = RecordEnvelope.from_dict(payload)
    except Exception as exc:
        # The source query already selects capability-audit rows.  A malformed
        # payload is therefore corruption, not a legacy row that can safely be
        # skipped or interpreted through a textual fallback.
        raise CapabilityBackfillError("capability audit source row payload is malformed") from exc
    if record.kind != "capability_audit":
        raise CapabilityBackfillError("capability audit source row has a non-audit payload kind")
    if record.source != "eimemory.capability.v3":
        raise CapabilityBackfillError("capability audit source row has a mismatched payload source")
    if record.status != "archived":
        raise CapabilityBackfillError("capability audit source row is not immutable archived evidence")
    if _scope_dict(record.scope) != _scope_dict(runtime_scope):
        raise CapabilityBackfillError("capability audit record runtime scope does not match selected scope")
    metadata = record.meta if isinstance(record.meta, Mapping) else {}
    try:
        audit = _capability_audit_from_record(
            record,
            scanned_operation_id=str(metadata.get("operation_id") or ""),
        )
    except Exception as exc:
        raise CapabilityBackfillError(
            f"capability audit record {record.record_id} failed structural validation: {exc}"
        ) from exc
    if audit is None:
        raise CapabilityBackfillError("capability audit record unexpectedly has no audit payload")
    try:
        audit_capability_scope = normalize_opaque_id(
            audit.get("capability_scope"),
            field="audit.capability_scope",
        )
    except (CapabilityContractError, TypeError, ValueError) as exc:
        raise CapabilityBackfillError("capability audit logical scope is malformed") from exc
    if audit_capability_scope != capability_scope:
        return BackfillRowResult(storage_key, "ignored", "audit_capability_scope_not_selected")
    entity_type = str(audit.get("entity_type") or "")
    if entity_type not in _SUPPORTED_AUDIT_ENTITY_TYPES:
        raise CapabilityBackfillError(f"unsupported capability audit entity type: {entity_type}")
    if not _phase_accepts_audit(phase, audit):
        return BackfillRowResult(
            storage_key,
            "ignored",
            "audit_entity_not_in_phase",
            entity_type=entity_type,
            entity_id=str(audit.get("entity_id") or ""),
            entity_digest=str(audit.get("entity_digest") or ""),
        )
    try:
        receipt = runtime.store.mutate_capabilities_atomically(
            lambda repository: repository.replay_audit(audit)
        )
    except Exception as exc:
        # This deliberately propagates: the source has a claimed immutable
        # dependency, and a failure to reproduce it must not be recorded as a
        # skipped historical fact or move the cursor beyond it.
        raise CapabilityBackfillError(
            f"capability audit replay failed for {entity_type}/{audit.get('entity_id')}: {exc}"
        ) from exc
    return BackfillRowResult(
        storage_key,
        "mapped",
        "capability_audit_replayed",
        observation_id=(
            str(audit.get("entity_id") or "") if entity_type == "observation" else ""
        ),
        observation_digest=(
            str(audit.get("entity_digest") or "") if entity_type == "observation" else ""
        ),
        entity_type=entity_type,
        entity_id=str(getattr(receipt, "entity_id", "") or audit.get("entity_id") or ""),
        entity_digest=str(getattr(receipt, "entity_digest", "") or audit.get("entity_digest") or ""),
    )


def _phase_accepts_audit(phase: _BackfillPhase, audit: Mapping[str, Any]) -> bool:
    entity_type = str(audit.get("entity_type") or "")
    if phase.name == "audit_relation":
        if entity_type == "relation":
            return True
        if entity_type != "lifecycle_transition":
            return False
        transition = audit.get("entity")
        return isinstance(transition, Mapping) and str(transition.get("entity_type") or "") == "relation"
    if phase.name == "audit_lifecycle_transition":
        if entity_type != "lifecycle_transition":
            return False
        transition = audit.get("entity")
        return not (
            isinstance(transition, Mapping)
            and str(transition.get("entity_type") or "") == "relation"
        )
    return entity_type in phase.entity_types


def _bounded_batch_size(value: object) -> int:
    if isinstance(value, bool):
        raise CapabilityBackfillError("batch_size must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityBackfillError("batch_size must be an integer") from exc
    if not 1 <= parsed <= _MAX_BATCH_SIZE:
        raise CapabilityBackfillError(f"batch_size must be from 1 to {_MAX_BATCH_SIZE}")
    return parsed


def _bounded_seconds(value: object) -> float:
    if isinstance(value, bool):
        raise CapabilityBackfillError("max_seconds must be numeric")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityBackfillError("max_seconds must be numeric") from exc
    if not 0.001 <= parsed <= _MAX_SECONDS:
        raise CapabilityBackfillError(f"max_seconds must be from 0.001 to {_MAX_SECONDS}")
    return parsed


def _bounded_dual_write_limit(value: object) -> int:
    if isinstance(value, bool):
        raise CapabilityBackfillError("dual-write limit must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CapabilityBackfillError("dual-write limit must be an integer") from exc
    if not 1 <= parsed <= 500:
        raise CapabilityBackfillError("dual-write limit must be from 1 to 500")
    return parsed


def _bounded_cursor(value: object) -> str:
    if not isinstance(value, str):
        raise CapabilityBackfillError("cursor must be text")
    if len(value) > 1_024:
        raise CapabilityBackfillError("cursor exceeds the 1024-character bound")
    return value


def _dual_write_row(
    conn: Any,
    *,
    row: Any,
    runtime_scope: ScopeRef,
    capability_scope: str,
) -> BackfillRowResult:
    storage_key = str(row["storage_key"])
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
        record = RecordEnvelope.from_dict(payload)
    except Exception:
        return BackfillRowResult(storage_key, "unmappable", "invalid_record_payload")
    if record.kind == "capability_audit" and record.source == "eimemory.capability.v3":
        return BackfillRowResult(storage_key, "ignored", "capability_audit_owned_by_entity_phase")
    if _scope_dict(record.scope) != _scope_dict(runtime_scope):
        return BackfillRowResult(storage_key, "unmappable", "record_scope_mismatch")
    observation, reason = _explicit_observation_from_record(record, capability_scope=capability_scope)
    if observation is None:
        return BackfillRowResult(storage_key, "unmappable", reason)
    receipt = _find_dual_write_observation(
        conn,
        scope=runtime_scope,
        capability_scope=capability_scope,
        observation=observation,
        record_id=record.record_id,
    )
    if receipt is None:
        return BackfillRowResult(
            storage_key,
            "missing",
            "dual_write_destination_missing",
            observation_id=record.record_id,
            observation_digest=observation.observation_digest,
        )
    return BackfillRowResult(
        storage_key,
        "aligned",
        "dual_write_destination_present",
        observation_id=str(receipt["observation_id"]),
        observation_digest=str(receipt["observation_digest"]),
    )


def _find_dual_write_observation(
    conn: Any,
    *,
    scope: ScopeRef,
    capability_scope: str,
    observation: CapabilityObservation,
    record_id: str,
) -> Mapping[str, Any] | None:
    rows = conn.execute(
        """
        SELECT observation_id, observation_digest, provenance_json
        FROM capability_observations
        WHERE tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
          AND capability_scope = ?
          AND capability_id = ? AND capability_revision_id = ? AND provider_binding_id = ?
          AND source IN ('outcome_normalization', 'legacy_explicit_backfill')
        ORDER BY created_at DESC, observation_id DESC
        LIMIT 32
        """,
        (
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            capability_scope,
            observation.capability_id,
            observation.capability_revision_id,
            observation.provider_binding_id,
        ),
    ).fetchall()
    for candidate in rows:
        try:
            provenance = json.loads(str(candidate["provenance_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(provenance, Mapping):
            continue
        if str(provenance.get("outcome_trace_record_id") or "") == record_id:
            return candidate
        if str(provenance.get("legacy_record_id") or "") == record_id:
            return candidate
    return None


def _is_completed_without_pending_rows(
    conn: Any,
    *,
    scope: ScopeRef,
    state: Mapping[str, Any],
) -> bool:
    """Compatibility predicate for callers still checking old state rows."""

    del conn, scope
    return _full_migration_complete(state, capability_v3_backfill_plan())


def _begin_state(
    conn: Any,
    *,
    previous: Mapping[str, Any],
    context: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    # The caller's read is only an optimistic preflight.  Derive every
    # reset/restart decision from a fresh row read while holding BEGIN
    # IMMEDIATE, otherwise a second worker that observed an older
    # ``not_installed`` row can erase the first worker's checkpoint.
    del previous
    migration_id = str(context["context_migration_id"])
    context_digest = str(context["context_digest"])
    context_json = _canonical_json(
        {
            "schema": context["schema"],
            "runtime_scope": context["runtime_scope"],
            "capability_scope": context["capability_scope"],
        }
    )
    now = _utc_now()
    plan_json = _canonical_json(
        {key: value for key, value in plan.items() if key != "digest"}
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT OR IGNORE INTO capability_v3_migration_state (
                migration_id, status, phase, context_digest, context_json, updated_at
            ) VALUES (?, 'not_scheduled', 'not_scheduled', ?, ?, ?)
            """,
            (migration_id, context_digest, context_json, now),
        )
        current = capability_v3_backfill_state(conn, migration_id=migration_id)
        observed_context = str(current.get("context_digest") or "")
        if observed_context and observed_context != context_digest:
            raise CapabilityBackfillError("backfill state context does not match requested exact scope")
        current_status = str(current.get("status") or "")
        current_phase = str(current.get("phase") or "")
        reset_for_plan = (
            current_status == "not_installed"
            or not _plan_matches(current, plan)
            or (
                current_phase not in _BACKFILL_PHASE_BY_NAME
                and current_phase != "completed"
            )
            or (
                current_phase == "completed"
                and not _full_migration_complete(current, plan)
            )
        )
        restart_delta = 1 if (
            current_status == "failed"
            or (reset_for_plan and current_status not in {"", "not_installed"})
        ) else 0
        result = conn.execute(
            """
            UPDATE capability_v3_migration_state
            SET status = 'running',
                phase = CASE WHEN ? THEN ? ELSE phase END,
                cursor = CASE WHEN ? THEN '' ELSE cursor END,
                context_digest = ?, context_json = ?,
                backfill_plan_digest = ?, backfill_plan_json = ?,
                phase_stats_json = CASE WHEN ? THEN '{}' ELSE phase_stats_json END,
                rows_scanned = CASE WHEN ? THEN 0 ELSE rows_scanned END,
                rows_written = CASE WHEN ? THEN 0 ELSE rows_written END,
                rows_skipped = CASE WHEN ? THEN 0 ELSE rows_skipped END,
                skipped_reasons_json = CASE WHEN ? THEN '{}' ELSE skipped_reasons_json END,
                batch_count = CASE WHEN ? THEN 0 ELSE batch_count END,
                source_watermark = CASE WHEN ? THEN '' ELSE source_watermark END,
                source_digest = CASE WHEN ? THEN '' ELSE source_digest END,
                target_digest = CASE WHEN ? THEN '' ELSE target_digest END,
                source_total = CASE WHEN ? THEN 0 ELSE source_total END,
                destination_total = CASE WHEN ? THEN 0 ELSE destination_total END,
                last_duration_ms = CASE WHEN ? THEN 0 ELSE last_duration_ms END,
                last_batch_digest = CASE WHEN ? THEN '' ELSE last_batch_digest END,
                last_batch_json = CASE WHEN ? THEN '{}' ELSE last_batch_json END,
                last_error = '',
                started_at = CASE WHEN started_at = '' OR ? THEN ? ELSE started_at END,
                updated_at = ?, finished_at = '',
                restart_count = restart_count + ?
            WHERE migration_id = ?
            """,
            (
                1 if reset_for_plan else 0,
                _BACKFILL_PHASES[0].name,
                1 if reset_for_plan else 0,
                context_digest,
                context_json,
                str(plan["digest"]),
                plan_json,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                1 if reset_for_plan else 0,
                now,
                now,
                restart_delta,
                migration_id,
            ),
        )
        if int(result.rowcount or 0) != 1:
            raise CapabilityBackfillError("unable to initialize scoped backfill state")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return capability_v3_backfill_state(conn, migration_id=migration_id)


def _blocked_report(
    *,
    context: Mapping[str, Any],
    plan: Mapping[str, Any],
    batch_size: int,
    max_seconds: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "schema": BACKFILL_SCHEMA,
        "ok": False,
        "status": "blocked",
        "reason": reason,
        "migration_id": CAPABILITY_V3_BACKFILL_MIGRATION,
        "context_migration_id": str(context["context_migration_id"]),
        "context": dict(context),
        "plan": dict(plan),
        "runtime_scope": dict(context["runtime_scope"]),
        "capability_scope": str(context["capability_scope"]),
        "batch_size": batch_size,
        "max_seconds": max_seconds,
        "processed": 0,
        "mapped": 0,
        "skipped": 0,
        "completed": False,
        "full_migration_complete": False,
        "coverage": _coverage(),
    }


def _terminal_report(
    *,
    context: Mapping[str, Any],
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    batch_size: int,
    max_seconds: float,
) -> dict[str, Any]:
    return {
        "schema": BACKFILL_SCHEMA,
        "ok": True,
        "status": "completed",
        "reason": "backfill_already_completed",
        "migration_id": CAPABILITY_V3_BACKFILL_MIGRATION,
        "context_migration_id": str(context["context_migration_id"]),
        "context": dict(context),
        "plan": dict(plan),
        "runtime_scope": dict(context["runtime_scope"]),
        "capability_scope": str(context["capability_scope"]),
        "batch_size": batch_size,
        "max_seconds": max_seconds,
        "processed": 0,
        "mapped": 0,
        "skipped": 0,
        "skipped_by_reason": {},
        "cumulative_skipped_by_reason": _state_reason_counts(state),
        "completed": True,
        "full_migration_complete": _full_migration_complete(state, plan),
        "cursor": str(state.get("cursor") or ""),
        "source_total": int(state.get("source_total") or 0),
        "destination_total": int(state.get("destination_total") or 0),
        "source_digest": str(state.get("source_digest") or ""),
        "target_digest": str(state.get("target_digest") or ""),
        "phase_stats": _state_phase_stats(state),
        "duration_ms": 0,
        "result_digest": "",
        "results": [],
        "coverage": _coverage(),
        "state": dict(state),
    }


def _batch_report(
    *,
    context: Mapping[str, Any],
    plan: Mapping[str, Any],
    state: Mapping[str, Any],
    batch_size: int,
    max_seconds: float,
    executed_phase: _BackfillPhase,
    next_phase: _BackfillPhase | None,
    results: list[BackfillRowResult],
    phase_complete: bool,
    complete: bool,
    duration_ms: int,
    source_total: int,
    destination_total: int,
    destination_counts: Mapping[str, int],
    reason: str,
) -> dict[str, Any]:
    mapped = sum(1 for item in results if item.status == "mapped")
    skipped = sum(1 for item in results if item.status == "unmappable")
    ignored = sum(1 for item in results if item.status == "ignored")
    rows = [_result_payload(item) for item in results]
    return {
        "schema": BACKFILL_SCHEMA,
        "ok": True,
        "status": "completed" if complete else "running",
        "reason": reason,
        "migration_id": CAPABILITY_V3_BACKFILL_MIGRATION,
        "context_migration_id": str(context["context_migration_id"]),
        "context": dict(context),
        "plan": dict(plan),
        "runtime_scope": dict(context["runtime_scope"]),
        "capability_scope": str(context["capability_scope"]),
        "batch_size": batch_size,
        "max_seconds": max_seconds,
        "processed": len(results),
        "mapped": mapped,
        "skipped": skipped,
        "ignored": ignored,
        "skipped_by_reason": _reason_counts(results),
        "cumulative_skipped_by_reason": _state_reason_counts(state),
        "phase": executed_phase.name,
        "next_phase": next_phase.name if next_phase is not None else "completed",
        "phase_complete": phase_complete,
        "completed": complete,
        "full_migration_complete": _full_migration_complete(state, plan),
        "cursor": str(state.get("cursor") or ""),
        "source_total": source_total,
        "destination_total": destination_total,
        "destination_entity_counts": dict(destination_counts),
        "source_digest": str(state.get("source_digest") or ""),
        "target_digest": str(state.get("target_digest") or ""),
        "duration_ms": duration_ms,
        "result_digest": _fold_digest("", results),
        "results": rows,
        "result_samples": rows[:_MAX_RESULT_SAMPLES],
        "omitted_result_count": max(0, len(rows) - _MAX_RESULT_SAMPLES),
        "coverage": _coverage(),
        "phase_stats": _state_phase_stats(state),
        "state": dict(state),
    }


def _failure_report(
    *,
    status: str,
    reason: str,
    error: Exception,
    context: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    batch_size: int = 0,
    max_seconds: float = 0.0,
    results: list[BackfillRowResult] | None = None,
    duration_ms: int = 0,
) -> dict[str, Any]:
    context_dict = dict(context or {})
    row_results = list(results or [])
    return {
        "schema": BACKFILL_SCHEMA,
        "ok": False,
        "status": status,
        "reason": reason,
        "error": type(error).__name__,
        "detail": str(error)[:1_000],
        "migration_id": CAPABILITY_V3_BACKFILL_MIGRATION,
        "context_migration_id": str(context_dict.get("context_migration_id") or ""),
        "context": context_dict,
        "plan": dict(plan or {}),
        "runtime_scope": dict(context_dict.get("runtime_scope") or {}),
        "capability_scope": str(context_dict.get("capability_scope") or ""),
        "batch_size": batch_size,
        "max_seconds": max_seconds,
        "processed": len(row_results),
        "mapped": sum(1 for item in row_results if item.status == "mapped"),
        "skipped": sum(1 for item in row_results if item.status == "unmappable"),
        "skipped_by_reason": _reason_counts(row_results),
        "cumulative_skipped_by_reason": _state_reason_counts(state or {}),
        "completed": False,
        "full_migration_complete": False,
        "phase": str((state or {}).get("phase") or ""),
        "duration_ms": duration_ms,
        "source_digest": str((state or {}).get("source_digest") or ""),
        "target_digest": str((state or {}).get("target_digest") or ""),
        "results": [_result_payload(item) for item in row_results[:_MAX_RESULT_SAMPLES]],
        "omitted_result_count": max(0, len(row_results) - _MAX_RESULT_SAMPLES),
        "coverage": _coverage(),
        "phase_stats": _state_phase_stats(state or {}),
        "state": dict(state or {}),
    }


def _batch_summary(
    *,
    context: Mapping[str, Any],
    plan: Mapping[str, Any],
    phase: _BackfillPhase | None,
    next_phase: _BackfillPhase | None,
    results: list[BackfillRowResult],
    source_total: int,
    destination_total: int,
    destination_counts: Mapping[str, int],
    duration_ms: int,
    complete: bool,
    phase_complete: bool,
    partial_failure: bool = False,
) -> dict[str, Any]:
    return {
        "schema": BACKFILL_SCHEMA,
        "context_digest": str(context["context_digest"]),
        "plan_digest": str(plan.get("digest") or ""),
        "phase": phase.name if phase is not None else "",
        "next_phase": next_phase.name if next_phase is not None else "completed",
        "processed": len(results),
        "mapped": sum(1 for item in results if item.status == "mapped"),
        "skipped": sum(1 for item in results if item.status == "unmappable"),
        "ignored": sum(1 for item in results if item.status == "ignored"),
        "skipped_by_reason": _reason_counts(results),
        "source_total": source_total,
        "destination_total": destination_total,
        "destination_entity_counts": dict(destination_counts),
        "duration_ms": duration_ms,
        "phase_complete": phase_complete,
        "completed": complete,
        "partial_failure": partial_failure,
        "result_digest": _fold_digest("", results),
    }


def _coverage() -> dict[str, Any]:
    return {
        "mode": "dependency_ordered_capability_audit_entity_graph",
        "audit_source_schema": "capability.audit.v1",
        "completion_scope": "all current-plan audit entity phases plus explicit independently attested legacy observations",
        "full_historical_entity_graph_complete": "only_when_full_migration_complete_is_true",
        "mapped_entity_types": [
            "definition",
            "revision",
            "relation",
            "binding",
            "advertisement",
            "profile",
            "evaluation_spec",
            "evaluation_run",
            "observation",
            "knowledge_link",
            "snapshot",
            "assessment",
            "lifecycle_transition",
            "legacy_explicit_observation",
        ],
        "not_inferred_from_legacy": [
            "free_text",
            "legacy_scores",
            "package_versions",
            "machine_fingerprints",
        ],
        "policy": "only structurally verified capability.audit.v1 entities are replayed; legacy rows without complete independent observation attribution remain unmappable",
    }


def _reason_counts(results: list[BackfillRowResult]) -> dict[str, int]:
    counts = Counter(item.reason for item in results if item.status == "unmappable" and item.reason)
    if len(counts) <= _MAX_REASON_BUCKETS:
        return {key: int(value) for key, value in sorted(counts.items())}
    retained = counts.most_common(_MAX_REASON_BUCKETS - 1)
    retained_keys = {key for key, _ in retained}
    overflow = sum(value for key, value in counts.items() if key not in retained_keys)
    return {
        **{key: int(value) for key, value in sorted(retained)},
        "other_unmappable_reason": int(overflow),
    }


def _state_reason_counts(state: Mapping[str, Any]) -> dict[str, int]:
    raw = state.get("skipped_reasons_json")
    if isinstance(raw, Mapping):
        payload: object = raw
    else:
        try:
            payload = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    if not isinstance(payload, Mapping):
        return {}
    normalized: dict[str, int] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key:
            continue
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed >= 0:
            normalized[key] = parsed
    return _bounded_reason_counts(normalized)


def _merge_reason_counts(
    previous: Mapping[str, Any],
    current: Mapping[str, int],
) -> dict[str, int]:
    merged = Counter(_state_reason_counts(previous))
    merged.update({str(key): int(value) for key, value in current.items() if int(value) > 0})
    return _bounded_reason_counts(merged)


def _bounded_reason_counts(counts: Mapping[str, int]) -> dict[str, int]:
    normalized = {str(key): max(0, int(value)) for key, value in counts.items() if str(key)}
    if len(normalized) <= _MAX_REASON_BUCKETS:
        return {key: value for key, value in sorted(normalized.items())}
    ranked = sorted(normalized.items(), key=lambda item: (-item[1], item[0]))[: _MAX_REASON_BUCKETS - 1]
    retained_keys = {key for key, _ in ranked}
    overflow = sum(value for key, value in normalized.items() if key not in retained_keys)
    return {
        **{key: value for key, value in sorted(ranked)},
        "other_unmappable_reason": overflow,
    }


def _merge_phase_stats(
    previous: Mapping[str, Any],
    *,
    phase: _BackfillPhase,
    cursor: str,
    results: list[BackfillRowResult],
    source_digest: str,
    target_digest: str,
    completed: bool,
) -> dict[str, dict[str, Any]]:
    stats = _state_phase_stats(previous)
    current = dict(stats.get(phase.name, {}))
    current["status"] = "completed" if completed else "running"
    current["cursor"] = cursor
    current["rows_scanned"] = max(0, _safe_int(current.get("rows_scanned"))) + len(results)
    current["rows_written"] = max(0, _safe_int(current.get("rows_written"))) + sum(
        1 for item in results if item.status == "mapped"
    )
    current["rows_unmappable"] = max(0, _safe_int(current.get("rows_unmappable"))) + sum(
        1 for item in results if item.status == "unmappable"
    )
    current["rows_ignored"] = max(0, _safe_int(current.get("rows_ignored"))) + sum(
        1 for item in results if item.status == "ignored"
    )
    current["source_digest"] = source_digest
    current["target_digest"] = target_digest
    current["updated_at"] = _utc_now()
    stats[phase.name] = current
    return {
        phase_name: stats[phase_name]
        for phase_name in (phase_item.name for phase_item in _BACKFILL_PHASES)
        if phase_name in stats
    }


def _result_payload(item: BackfillRowResult) -> dict[str, Any]:
    return {
        "storage_key": item.storage_key,
        "status": item.status,
        "reason": item.reason,
        "observation_id": item.observation_id,
        "observation_digest": item.observation_digest,
        "entity_type": item.entity_type,
        "entity_id": item.entity_id,
        "entity_digest": item.entity_digest,
    }


def _source_count(conn: Any, *, scope: ScopeRef) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM records
        WHERE tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
        """,
        (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id),
    ).fetchone()
    return int(row[0] if row is not None else 0)


def _destination_count(conn: Any, *, scope: ScopeRef, capability_scope: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM capability_observations
        WHERE tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
          AND capability_scope = ?
        """,
        (
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            capability_scope,
        ),
    ).fetchone()
    return int(row[0] if row is not None else 0)


_DESTINATION_ENTITY_TABLES: tuple[tuple[str, str], ...] = (
    ("definition", "capability_definitions"),
    ("revision", "capability_revisions"),
    ("relation", "capability_relations"),
    ("binding", "capability_bindings"),
    ("advertisement", "adapter_capability_advertisements"),
    ("profile", "capability_profiles"),
    ("evaluation_spec", "evaluation_specs"),
    ("evaluation_run", "evaluation_runs"),
    ("observation", "capability_observations"),
    ("knowledge_link", "capability_knowledge_links"),
    ("snapshot", "capability_state_snapshots"),
    ("assessment", "l5_assessments_v3"),
    ("lifecycle_transition", "capability_entity_lifecycle_events"),
)


def _destination_entity_counts(
    conn: Any,
    *,
    scope: ScopeRef,
    capability_scope: str,
) -> dict[str, int]:
    """Expose bounded destination shape without treating counts as evidence."""

    values = (
        scope.tenant_id,
        scope.agent_id,
        scope.workspace_id,
        scope.user_id,
        capability_scope,
    )
    counts: dict[str, int] = {}
    for entity_type, table in _DESTINATION_ENTITY_TABLES:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM {table}
            WHERE tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?
              AND capability_scope = ?
            """,
            values,
        ).fetchone()
        counts[entity_type] = int(row[0] if row is not None else 0)
    return counts


def _duration_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1_000))


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_records(conn: Any, *, scope: ScopeRef, cursor: str, limit: int) -> list[Any]:
    return conn.execute(
        """
        SELECT storage_key, payload_json
        FROM records
        WHERE tenant_id = ?
          AND agent_id = ?
          AND workspace_id = ?
          AND user_id = ?
          AND storage_key > ?
        ORDER BY storage_key ASC
        LIMIT ?
        """,
        (
            scope.tenant_id,
            scope.agent_id,
            scope.workspace_id,
            scope.user_id,
            cursor,
            limit,
        ),
    ).fetchall()


def _backfill_row(
    row: Any,
    *,
    observations: CapabilityObservations,
    runtime_scope: ScopeRef,
    capability_scope: str,
) -> BackfillRowResult:
    storage_key = str(row["storage_key"])
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
        record = RecordEnvelope.from_dict(payload)
    except Exception:
        return BackfillRowResult(storage_key, "unmappable", "invalid_record_payload")
    if _scope_dict(record.scope) != _scope_dict(runtime_scope):
        return BackfillRowResult(storage_key, "unmappable", "record_scope_mismatch")
    observation, reason = _explicit_observation_from_record(record, capability_scope=capability_scope)
    if observation is None:
        return BackfillRowResult(storage_key, "unmappable", reason)
    try:
        result = observations.append(
            observation,
            runtime_scope=runtime_scope,
            request_key=f"capability-v3-backfill:{record.record_id}:{observation.observation_digest}",
        )
    except (CapabilityContractError, CapabilityStoreError, sqlite3.IntegrityError, ValueError) as exc:
        # An explicit historical observation whose immutable dependency chain
        # is absent is not a reason to stop later rows or fabricate the
        # missing definition/revision/binding.  Infrastructure/lock failures
        # deliberately escape this branch and leave the cursor unchanged.
        return BackfillRowResult(
            storage_key,
            "unmappable",
            f"destination_dependency_unavailable:{type(exc).__name__}",
        )
    receipt = result.observation
    if receipt is None:
        return BackfillRowResult(storage_key, "unmappable", "observation_boundary_unavailable")
    return BackfillRowResult(
        storage_key,
        "mapped",
        "explicit_attribution",
        observation_id=observation.observation_id,
        observation_digest=observation.observation_digest,
        entity_type="observation",
        entity_id=observation.observation_id,
        entity_digest=observation.observation_digest,
    )


def _explicit_observation_from_record(
    record: RecordEnvelope,
    *,
    capability_scope: str,
) -> tuple[CapabilityObservation | None, str]:
    """Translate only a complete, independently attested v3-shaped record."""

    sources = _record_sources(record)
    attribution = _first_mapping(sources, "capability_attribution")
    if not attribution:
        return None, "missing_explicit_capability_attribution"
    declared_scope = attribution.get("capability_scope")
    if declared_scope not in (None, ""):
        try:
            if normalize_opaque_id(declared_scope, field="capability_attribution.capability_scope") != capability_scope:
                return None, "capability_scope_mismatch"
        except CapabilityContractError:
            return None, "invalid_capability_scope"
    verifier = _first_mapping(sources, "verifier")
    required = (
        "capability_id",
        "capability_revision_id",
        "provider_binding_id",
        "idempotency_key",
        "observed_at",
        "evidence_refs",
        "environment_fingerprint",
        "provenance",
    )
    missing = [key for key in required if key not in attribution]
    if missing:
        return None, f"missing_attribution_fields:{','.join(missing)}"
    if verifier.get("independent") is not True:
        return None, "missing_independent_verifier"
    verifier_id = str(verifier.get("id") or "").strip()
    verifier_revision = str(verifier.get("revision") or "").strip()
    verifier_digest = str(verifier.get("contract_digest") or "").strip()
    if not verifier_id or not verifier_revision or not verifier_digest:
        return None, "incomplete_verifier_identity"
    evidence_refs = attribution.get("evidence_refs")
    environment = attribution.get("environment_fingerprint")
    provenance = attribution.get("provenance")
    if not isinstance(evidence_refs, list) or not evidence_refs:
        return None, "missing_evidence_refs"
    if not isinstance(environment, Mapping) or not environment:
        return None, "missing_environment_fingerprint"
    if not isinstance(provenance, Mapping) or not provenance:
        return None, "missing_provenance"
    verdict = _explicit_verdict(sources, verifier)
    if not verdict:
        return None, "missing_explicit_verdict"
    try:
        observed_at = require_timestamp(attribution.get("observed_at"), field="observed_at")
        digest = normalize_sha256(verifier_digest, field="verifier.contract_digest")
    except Exception:
        return None, "invalid_verifier_or_time"
    identity = _digest(
        {
            "record_id": record.record_id,
            "attribution": attribution,
            "verifier": verifier,
            "capability_scope": capability_scope,
        }
    )
    try:
        return (
            CapabilityObservation(
                observation_id=f"legacy-observation-{identity[:40]}",
                capability_id=str(attribution["capability_id"]),
                capability_revision_id=str(attribution["capability_revision_id"]),
                provider_binding_id=str(attribution["provider_binding_id"]),
                idempotency_key=f"legacy:{record.record_id}:{attribution['idempotency_key']}",
                verdict=verdict,
                source="legacy_explicit_backfill",
                executor_id=verifier_id,
                executor_contract_digest=digest,
                grader_id=verifier_id,
                grader_revision=verifier_revision,
                input_digest=_digest(attribution),
                output_digest=_digest(_first_mapping(sources, "outcome") or {"record_id": record.record_id}),
                evidence_digest=_digest({"evidence_refs": evidence_refs, "verifier": verifier}),
                evidence_refs=tuple(str(item) for item in evidence_refs),
                environment_fingerprint=dict(environment),
                provenance={
                    **dict(provenance),
                    "backfill_schema": BACKFILL_SCHEMA,
                    "legacy_record_id": record.record_id,
                },
                metrics={"backfilled": 1.0},
                error_taxonomy={} if verdict == "pass" else {"legacy": f"verdict_{verdict}"},
                observed_at=observed_at,
                scope=capability_scope,
                deployment_authority=(
                    dict(attribution["deployment_authority"])
                    if isinstance(attribution.get("deployment_authority"), Mapping)
                    else {}
                ),
            ),
            "",
        )
    except Exception:
        return None, "invalid_explicit_observation_contract"


def _record_sources(record: RecordEnvelope) -> list[Mapping[str, Any]]:
    content = record.content if isinstance(record.content, Mapping) else {}
    nested = content.get("payload") if isinstance(content.get("payload"), Mapping) else {}
    return [record.meta, content, nested, record.provenance]


def _first_mapping(sources: list[Mapping[str, Any]], key: str) -> dict[str, Any]:
    for source in sources:
        value = source.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _explicit_verdict(sources: list[Mapping[str, Any]], verifier: Mapping[str, Any]) -> str:
    for source in sources:
        value = str(source.get("verdict") or "").strip().lower()
        if value in {"pass", "fail", "blocked", "inconclusive"}:
            return value
    if isinstance(verifier.get("passed"), bool):
        return "pass" if verifier["passed"] else "fail"
    return ""


def _write_state(
    conn: Any,
    *,
    previous: Mapping[str, Any],
    migration_id: str,
    context: Mapping[str, Any],
    plan: Mapping[str, Any],
    phase_stats: Mapping[str, Mapping[str, Any]],
    cursor: str,
    scanned_delta: int,
    written_delta: int,
    skipped_delta: int,
    source_digest: str,
    target_digest: str,
    source_total: int,
    destination_total: int,
    duration_ms: int,
    batch_summary: Mapping[str, Any],
    status: str,
    phase: str,
    last_error: str,
    finished: bool,
) -> dict[str, Any]:
    observed_context = str(previous.get("context_digest") or "")
    expected_context = str(context["context_digest"])
    if observed_context and observed_context != expected_context:
        raise CapabilityBackfillError("backfill state context changed while a batch was running")
    if not _plan_matches(previous, plan):
        raise CapabilityBackfillError("backfill state plan changed while a batch was running")
    if phase != "completed" and phase not in _BACKFILL_PHASE_BY_NAME:
        raise CapabilityBackfillError(f"unknown checkpoint phase: {phase}")
    if status == "completed" and phase != "completed":
        raise CapabilityBackfillError("a completed backfill state must use the completed phase")
    if min(scanned_delta, written_delta, skipped_delta, source_total, destination_total, duration_ms) < 0:
        raise CapabilityBackfillError("backfill state counters must be non-negative")
    now = _utc_now()
    context_json = _canonical_json(
        {
            "schema": context["schema"],
            "runtime_scope": context["runtime_scope"],
            "capability_scope": context["capability_scope"],
        }
    )
    plan_json = _canonical_json({key: value for key, value in plan.items() if key != "digest"})
    phase_stats_json = _canonical_json(dict(phase_stats))
    batch_json = _canonical_json(dict(batch_summary))
    batch_digest = _digest(batch_summary)
    batch_reasons = batch_summary.get("skipped_by_reason")
    if not isinstance(batch_reasons, Mapping):
        raise CapabilityBackfillError("backfill batch summary lacks skipped reason counts")
    cumulative_reasons = _merge_reason_counts(
        previous,
        {str(key): int(value) for key, value in batch_reasons.items()},
    )
    cumulative_reasons_json = _canonical_json(cumulative_reasons)
    conn.execute("BEGIN IMMEDIATE")
    try:
        result = conn.execute(
            """
            UPDATE capability_v3_migration_state
            SET status = ?, phase = ?, context_digest = ?, context_json = ?,
                backfill_plan_digest = ?, backfill_plan_json = ?, phase_stats_json = ?,
                cursor = ?,
                rows_scanned = rows_scanned + ?,
                rows_written = rows_written + ?,
                rows_skipped = rows_skipped + ?,
                skipped_reasons_json = ?,
                batch_count = batch_count + 1,
                source_watermark = ?, source_digest = ?, target_digest = ?,
                source_total = ?, destination_total = ?, last_duration_ms = ?,
                last_batch_digest = ?, last_batch_json = ?,
                last_error = ?, started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END,
                updated_at = ?, finished_at = CASE WHEN ? THEN ? ELSE '' END
            WHERE migration_id = ?
            """,
            (
                status,
                phase,
                expected_context,
                context_json,
                str(plan["digest"]),
                plan_json,
                phase_stats_json,
                cursor,
                scanned_delta,
                written_delta,
                skipped_delta,
                cumulative_reasons_json,
                cursor,
                source_digest,
                target_digest,
                source_total,
                destination_total,
                duration_ms,
                batch_digest,
                batch_json,
                last_error[:1_000],
                now,
                now,
                1 if finished else 0,
                now,
                migration_id,
            ),
        )
        if int(result.rowcount or 0) != 1:
            raise CapabilityBackfillError("scoped backfill state disappeared before checkpoint")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return capability_v3_backfill_state(conn, migration_id=migration_id)


def _fold_digest(previous: str, results: list[BackfillRowResult], *, target: bool = False) -> str:
    entries = []
    for item in results:
        target_identity = ""
        if target and item.status == "mapped":
            target_identity = item.entity_digest or item.observation_digest
        source_identity = item.entity_id or item.observation_id
        entries.append(
            {
                "storage_key": item.storage_key,
                "status": item.status,
                "reason": item.reason,
                "entity_type": item.entity_type,
                "target": target_identity if target else source_identity,
            }
        )
    return _digest({"previous": previous, "entries": entries})


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _scope_dict(scope: ScopeRef) -> dict[str, str]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "BACKFILL_SCHEMA",
    "DUAL_WRITE_SCHEMA",
    "BACKFILL_PLAN_SCHEMA",
    "BACKFILL_CURSOR_SCHEMA",
    "BackfillRowResult",
    "CapabilityBackfillError",
    "capability_v3_backfill_context",
    "capability_v3_backfill_plan",
    "capability_v3_backfill_status",
    "inspect_capability_v3_dual_write",
    "run_capability_v3_backfill_batch",
]
