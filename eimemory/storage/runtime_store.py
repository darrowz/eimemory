from __future__ import annotations

import json
import os
from pathlib import Path
from hashlib import sha256
import re
from threading import RLock
import tempfile
from collections.abc import Callable
from typing import TypeVar

from eimemory.adapters.openclaw.qmd_export import export_record_markdown
from eimemory.metadata import business_metadata
from eimemory.models.memory_edges import MemoryEdge
from eimemory.storage.jsonl import JsonlLog, JsonlScanEntry, JsonlScanError
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.models.records import RecordEnvelope, ScopeRef


AUXILIARY_JSONL_STREAMS = (
    "events",
    "event_outcomes",
    "intent_patterns",
    "memory_edges",
    "policy_rollout_ledger",
    "replay_manifests",
)

T = TypeVar("T")


class RuntimeStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = JsonlLog(self.root / "records.jsonl")
        self.auxiliary_log_dir = self.root / "state"
        self._auxiliary_logs: dict[str, JsonlLog] = {}
        self._lock = RLock()
        self.sqlite = SqliteRecordStore(self.root / "state" / "eimemory.sqlite", auxiliary_log_dir=self.auxiliary_log_dir)

    def append(self, record: RecordEnvelope) -> RecordEnvelope:
        with self._lock:
            existing = self._existing_reflection_duplicate(record)
            if existing is not None:
                return existing
            try:
                if _deterministic_insert_once(record):
                    self.sqlite.conn.execute("BEGIN IMMEDIATE")
                    existing = self.sqlite.get_by_id(record.record_id, scope=record.scope)
                    if existing is not None:
                        self.sqlite.conn.commit()
                        return existing
                self.sqlite.upsert(record, commit=False)
                exports = self._enqueue_record_exports(record)
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(*(item["operation_id"] for item in exports))
            export_record_markdown(self.root, record)
            return record

    def mutate_records_atomically(
        self,
        mutation: Callable[
            [SqliteRecordStore],
            tuple[T, list[RecordEnvelope], list[MemoryEdge]],
        ],
    ) -> T:
        """Run an authoritative record mutation in one SQLite write transaction.

        The callback must use ``commit=False`` for record writes and return
        every changed record plus every graph edge.  Record changes, edges,
        and their durable JSONL outbox entries commit together; outbox flush
        and Markdown projection happen only after that commit.
        """

        with self._lock:
            operation_ids: list[str] = []
            changed_records: list[RecordEnvelope] = []
            try:
                self.sqlite.conn.execute("BEGIN IMMEDIATE")
                result, changed_records, changed_edges = mutation(self.sqlite)
                self.sqlite.upsert_memory_edges(changed_edges, commit=False)
                for record in changed_records:
                    operation_ids.extend(
                        export["operation_id"]
                        for export in self._enqueue_record_exports(record)
                    )
                for edge in changed_edges:
                    export = self.sqlite.enqueue_export(
                        stream="memory_edges",
                        payload=self._auxiliary_entry(
                            "memory_edges",
                            edge.to_dict(),
                            scope=edge.scope,
                        ),
                        commit=False,
                    )
                    operation_ids.append(export["operation_id"])
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(*operation_ids)
            for record in changed_records:
                export_record_markdown(self.root, record)
            return result

    def append_proactive_turn(
        self,
        payload: dict,
        *,
        max_session_turns: int = 4,
        max_global_turns: int = 512,
    ) -> list[dict]:
        with self._lock:
            try:
                self.sqlite.conn.execute("BEGIN IMMEDIATE")
                result = self.sqlite.append_proactive_turn(
                    payload,
                    max_session_turns=max_session_turns,
                    max_global_turns=max_global_turns,
                    commit=False,
                )
                self.sqlite.conn.commit()
                return result
            except Exception:
                self.sqlite.conn.rollback()
                raise

    def load_proactive_turns(self, payload: dict, *, limit: int = 4) -> list[dict]:
        with self._lock:
            return self.sqlite.load_proactive_turns(payload, limit=limit)

    def record_proactive_decision(
        self,
        payload: dict,
        items: list[dict],
        feedback_records: list[RecordEnvelope],
        *,
        max_global_decisions: int = 512,
    ) -> tuple[dict, bool]:
        """Persist a decision, its items, and volunteered audit records atomically."""

        with self._lock:
            operation_ids: list[str] = []
            written_records: list[RecordEnvelope] = []
            try:
                self.sqlite.conn.execute("BEGIN IMMEDIATE")
                decision, idempotent = self.sqlite.insert_proactive_decision(
                    payload, items, max_global_decisions=max_global_decisions, commit=False
                )
                if not idempotent:
                    for record in feedback_records:
                        existing = self.sqlite.get_by_idempotency_key(
                            kinds=[record.kind], scope=record.scope,
                            idempotency_key=str(record.meta.get("idempotency_key") or ""),
                        )
                        if existing is not None:
                            continue
                        self.sqlite.upsert(record, commit=False)
                        operation_ids.extend(
                            export["operation_id"] for export in self._enqueue_record_exports(record)
                        )
                        written_records.append(record)
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(*operation_ids)
            for record in written_records:
                export_record_markdown(self.root, record)
            return decision, idempotent

    def load_proactive_decision(self, decision_id: str) -> dict | None:
        with self._lock:
            return self.sqlite.load_proactive_decision(decision_id)

    def find_proactive_decision(self, payload: dict) -> dict | None:
        with self._lock:
            return self.sqlite.find_proactive_decision(payload)

    def list_stale_proactive_decisions(
        self,
        payload: dict,
        *,
        before_created_at: str,
        before_injected_updated_at: str,
        limit: int = 64,
    ) -> list[dict]:
        with self._lock:
            return self.sqlite.list_stale_proactive_decisions(
                payload,
                before_created_at=before_created_at,
                before_injected_updated_at=before_injected_updated_at,
                limit=limit,
            )

    def proactive_session_refs(self, payload: dict, *, limit: int = 512) -> set[tuple[str, str]]:
        with self._lock:
            return self.sqlite.proactive_session_refs(payload, limit=limit)

    def transition_proactive_decision(
        self,
        decision_id: str,
        targets: dict[str, str],
        feedback_records: dict[tuple[str, str], RecordEnvelope],
        *,
        expected: dict | None = None,
        stale_lease_guard: dict[str, str] | None = None,
    ) -> list[dict] | None:
        """CAS decision items and append their usage feedback in one transaction."""

        with self._lock:
            operation_ids: list[str] = []
            written_records: list[RecordEnvelope] = []
            try:
                self.sqlite.conn.execute("BEGIN IMMEDIATE")
                changed = self.sqlite.transition_proactive_items(
                    decision_id,
                    targets,
                    expected=expected,
                    stale_lease_guard=stale_lease_guard,
                    commit=False,
                )
                if changed is None:
                    self.sqlite.conn.commit()
                    return None
                for item in changed:
                    key = (str(item["citation"]), str(item["state"]))
                    record = feedback_records.get(key)
                    if record is None:
                        raise ValueError("missing proactive feedback for an accepted transition")
                    existing = self.sqlite.get_by_idempotency_key(
                        kinds=[record.kind], scope=record.scope,
                        idempotency_key=str(record.meta.get("idempotency_key") or ""),
                    )
                    if existing is None:
                        self.sqlite.upsert(record, commit=False)
                        operation_ids.extend(
                            export["operation_id"] for export in self._enqueue_record_exports(record)
                        )
                        written_records.append(record)
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(*operation_ids)
            for record in written_records:
                export_record_markdown(self.root, record)
            return changed

    def record_proactive_outcome(
        self, decision_id: str, outcome: dict, *, expected: dict | None = None
    ) -> bool:
        with self._lock:
            try:
                self.sqlite.conn.execute("BEGIN IMMEDIATE")
                created = self.sqlite.update_proactive_outcome(
                    decision_id, outcome, expected=expected, commit=False
                )
                self.sqlite.conn.commit()
                return created
            except Exception:
                self.sqlite.conn.rollback()
                raise

    def list_proactive_outcomes(self, payload: dict, *, limit: int = 500) -> list[dict]:
        with self._lock:
            return self.sqlite.list_proactive_outcomes(payload, limit=limit)

    def append_proactive_bypass(self, payload: dict, *, max_entries: int = 64) -> None:
        with self._lock:
            try:
                self.sqlite.conn.execute("BEGIN IMMEDIATE")
                self.sqlite.append_proactive_bypass(payload, max_entries=max_entries, commit=False)
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise

    def list_proactive_bypasses(self, *, limit: int = 64) -> list[dict[str, str]]:
        with self._lock:
            return self.sqlite.list_proactive_bypasses(limit=limit)

    def rewrite(self, record: RecordEnvelope, *, previous_scope: ScopeRef | dict | None = None) -> RecordEnvelope:
        with self._lock:
            previous_scope_ref = (
                previous_scope
                if isinstance(previous_scope, ScopeRef)
                else (None if previous_scope is None else ScopeRef.from_dict(previous_scope))
            )
            try:
                self.sqlite.rewrite(
                    record,
                    previous_scope=previous_scope_ref,
                    commit=False,
                )
                exports = self._enqueue_record_exports(record)
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(*(item["operation_id"] for item in exports))
            export_record_markdown(self.root, record)
            return record

    def search(
        self,
        *,
        query: str,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        limit: int = 10,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.search(
                query=query, kinds=kinds, scope=scope_ref, limit=limit, source_ids=source_ids
            )

    def search_with_diagnostics(
        self,
        *,
        query: str,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        limit: int = 10,
        recall_filters: dict | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> tuple[list[RecordEnvelope], dict]:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.search_with_diagnostics(
                query=query,
                kinds=kinds,
                scope=scope_ref,
                limit=limit,
                recall_filters=recall_filters,
                source_ids=source_ids,
            )

    def get_active_policy(
        self,
        *,
        task_type: str,
        scope: ScopeRef | dict | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.get_active_policy(
                task_type=task_type,
                scope=scope_ref,
                source_ids=source_ids,
            )

    def record_event(self, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            try:
                result = self.sqlite.record_event(payload, scope=scope_ref, commit=False)
                export = self.sqlite.enqueue_export(
                    stream="events",
                    payload=self._auxiliary_entry("events", result, scope=scope_ref),
                    commit=False,
                )
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(export["operation_id"])
            return result

    def record_outcome(self, event_id: str, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            try:
                result = self.sqlite.record_outcome(
                    event_id,
                    payload,
                    scope=scope_ref,
                    commit=False,
                )
                export = self.sqlite.enqueue_export(
                    stream="event_outcomes",
                    payload=self._auxiliary_entry(
                        "event_outcomes", result, scope=scope_ref
                    ),
                    commit=False,
                )
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(export["operation_id"])
            return result

    def record_terminal_bundle(
        self,
        *,
        verified_receipts: list[dict],
        channel: str,
        session_id: str,
        run_id: str,
        trace_id: str,
        event_payload: dict,
        outcome_payload: dict,
        trace_record: RecordEnvelope,
        scope: ScopeRef | dict | None = None,
    ) -> dict:
        """Commit receipt consumption and all terminal persistence in one transaction."""
        from eimemory.events import ensure_outcome_payload

        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            expected_receipts = list(verified_receipts)
            clean_receipt_ids = list(
                dict.fromkeys(
                    str(item.get("receipt_id") or "").strip()
                    for item in expected_receipts
                    if str(item.get("receipt_id") or "").strip()
                )
            )
            operation_ids: list[str] = []
            try:
                self.sqlite.conn.execute("BEGIN IMMEDIATE")
                canonical_outcome = ensure_outcome_payload(
                    str(event_payload.get("id") or ""),
                    outcome_payload,
                )
                recorded_event = self._existing_terminal_event(
                    str(event_payload.get("id") or ""), scope=scope_ref
                )
                recorded_outcome = self._existing_terminal_outcome(
                    str(canonical_outcome["id"]), scope=scope_ref
                )
                stored_trace = self.sqlite.get_by_id(trace_record.record_id, scope=scope_ref)
                existing_parts = (
                    recorded_event is not None,
                    recorded_outcome is not None,
                    stored_trace is not None,
                )
                if any(existing_parts):
                    if not all(existing_parts):
                        raise ValueError("terminal retry conflict: incomplete persisted bundle")
                    expected_digest = str(event_payload.get("terminal_contract_digest") or "")
                    trace_payload = (
                        stored_trace.content.get("payload")
                        if isinstance(stored_trace.content, dict)
                        else None
                    )
                    observed_digests = {
                        str(recorded_event.get("terminal_contract_digest") or ""),
                        str(recorded_outcome.get("terminal_contract_digest") or ""),
                        str(trace_payload.get("terminal_contract_digest") or "")
                        if isinstance(trace_payload, dict)
                        else "",
                    }
                    if not expected_digest or observed_digests != {expected_digest}:
                        raise ValueError("terminal retry conflict: payload or receipt set changed")

                claimable = self.sqlite.load_claimable_adapter_tool_receipts(
                    channel=channel,
                    session_id=session_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    scope=scope_ref,
                )
                claimable_ids = [str(item.get("receipt_id") or "") for item in claimable]
                if sorted(claimable_ids) != sorted(clean_receipt_ids):
                    raise ValueError("terminal receipt set changed before atomic consumption")
                if sorted(claimable, key=lambda item: str(item.get("receipt_id") or "")) != sorted(
                    expected_receipts,
                    key=lambda item: str(item.get("receipt_id") or ""),
                ):
                    raise ValueError("terminal receipt payload changed before atomic consumption")
                consumed = self.sqlite.consume_adapter_tool_receipts(
                    clean_receipt_ids,
                    channel=channel,
                    session_id=session_id,
                    run_id=run_id,
                    trace_id=trace_id,
                    scope=scope_ref,
                    commit=False,
                )
                consumed_ids = [str(item.get("receipt_id") or "") for item in consumed]
                if sorted(consumed_ids) != sorted(clean_receipt_ids):
                    raise ValueError("terminal receipt set changed before atomic consumption")
                if sorted(consumed, key=lambda item: str(item.get("receipt_id") or "")) != sorted(
                    expected_receipts,
                    key=lambda item: str(item.get("receipt_id") or ""),
                ):
                    raise ValueError("terminal receipt payload changed before atomic consumption")

                if recorded_event is None:
                    recorded_event = self.sqlite.record_event(
                        event_payload,
                        scope=scope_ref,
                        commit=False,
                    )
                event_export = self.sqlite.enqueue_export(
                    stream="events",
                    payload=self._auxiliary_entry("events", recorded_event, scope=scope_ref),
                    commit=False,
                )
                operation_ids.append(event_export["operation_id"])

                if recorded_outcome is None:
                    recorded_outcome = self.sqlite.record_outcome(
                        recorded_event["id"],
                        canonical_outcome,
                        scope=scope_ref,
                        commit=False,
                        apply_rollbacks=False,
                    )
                outcome_export = self.sqlite.enqueue_export(
                    stream="event_outcomes",
                    payload=self._auxiliary_entry(
                        "event_outcomes", recorded_outcome, scope=scope_ref
                    ),
                    commit=False,
                )
                operation_ids.append(outcome_export["operation_id"])

                if stored_trace is None:
                    self.sqlite.upsert(trace_record, commit=False)
                    stored_trace = trace_record
                trace_exports = self._enqueue_record_exports(stored_trace)
                operation_ids.extend(item["operation_id"] for item in trace_exports)
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(*operation_ids)
            export_record_markdown(self.root, stored_trace)
            return {
                "event": recorded_event,
                "outcome": recorded_outcome,
                "outcome_trace": {
                    "ok": True,
                    "record_id": stored_trace.record_id,
                    "kind": stored_trace.kind,
                    "idempotent": stored_trace is not trace_record,
                },
            }

    def upsert_intent_pattern(self, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            try:
                result = self.sqlite.upsert_intent_pattern(
                    payload,
                    scope=scope_ref,
                    commit=False,
                )
                export = self.sqlite.enqueue_export(
                    stream="intent_patterns",
                    payload=self._auxiliary_entry(
                        "intent_patterns", result, scope=scope_ref
                    ),
                    commit=False,
                )
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(export["operation_id"])
            return result

    def search_policy(
        self,
        user_phrase: str,
        *,
        scope: ScopeRef | dict | None = None,
        context: dict | None = None,
        limit: int = 5,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.search_policy(
                user_phrase,
                scope=scope_ref,
                context=context,
                limit=limit,
                source_ids=source_ids,
            )

    def get_policy_rollout_ledger(
        self,
        *,
        scope: ScopeRef | dict | None = None,
        action: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.get_policy_rollout_ledger(scope=scope_ref, action=action, limit=limit)

    def rollback_intent_pattern(
        self,
        pattern_id: str,
        *,
        scope: ScopeRef | dict | None = None,
        reason: str = "",
        auto: bool = False,
    ) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.rollback_intent_pattern(pattern_id, scope=scope_ref, reason=reason, auto=auto)

    def get_by_id(self, record_id: str, scope: ScopeRef | dict | None = None) -> RecordEnvelope | None:
        with self._lock:
            scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
            return self.sqlite.get_by_id(record_id, scope=scope_ref)

    def get_by_exact_ref(
        self,
        record_id: str,
        *,
        scope: ScopeRef | dict,
        source_id: str,
    ) -> RecordEnvelope | None:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.get_by_exact_ref(record_id, scope=scope_ref, source_id=source_id)

    def list_by_record_id_exact_scope(
        self,
        record_id: str,
        *,
        scope: ScopeRef | dict,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.list_by_record_id_exact_scope(
                record_id,
                scope=scope_ref,
                source_ids=source_ids,
            )

    def get_by_idempotency_key(
        self,
        *,
        kinds: list[str],
        scope: ScopeRef | dict | None,
        idempotency_key: str,
    ) -> RecordEnvelope | None:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.get_by_idempotency_key(kinds=kinds, scope=scope_ref, idempotency_key=idempotency_key)

    def get_many_by_ids(self, record_ids: list[str], scope: ScopeRef | dict | None = None) -> list[RecordEnvelope]:
        with self._lock:
            resolved: list[RecordEnvelope] = []
            seen: set[str] = set()
            for record_id in record_ids:
                if record_id in seen:
                    continue
                seen.add(record_id)
                record = self.get_by_id(record_id, scope=scope)
                if record is not None:
                    resolved.append(record)
            return resolved

    def list_records(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        since: str | None = None,
        until: str | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope]:
        with self._lock:
            scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
            return self.sqlite.list_records(
                kinds=kinds,
                scope=scope_ref,
                status=status,
                limit=limit,
                offset=offset,
                since=since,
                until=until,
                source_ids=source_ids,
            )

    def count_records(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        status: str | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> int:
        with self._lock:
            scope_ref = None if scope is None else (
                scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            )
            return self.sqlite.count_records(
                kinds=kinds,
                scope=scope_ref,
                status=status,
                source_ids=source_ids,
            )

    def count_records_exact_scope(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict,
        status: str | None = None,
        statuses: list[str] | set[str] | tuple[str, ...] | None = None,
        since: str | None = None,
        until: str | None = None,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> int:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.count_records_exact_scope(
                kinds=kinds,
                scope=scope_ref,
                status=status,
                statuses=statuses,
                since=since,
                until=until,
                source_ids=source_ids,
            )

    def list_capability_scores_compact(
        self,
        *,
        scope: ScopeRef | dict,
        limit: int = 500,
        since: str | None = None,
        until: str | None = None,
    ) -> list[RecordEnvelope]:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.list_capability_scores_compact(
                scope=scope_ref,
                limit=limit,
                since=since,
                until=until,
            )

    def count_records_by_meta_value(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        meta_key: str,
        meta_value: object,
        status: str | None = None,
    ) -> int | None:
        with self._lock:
            scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
            return self.sqlite.count_records_by_meta_value(
                kinds=kinds,
                scope=scope_ref,
                meta_key=meta_key,
                meta_value=meta_value,
                status=status,
            )

    def list_records_by_meta_value(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        meta_key: str,
        meta_value: object,
        status: str | None = None,
        limit: int = 100,
        source_ids: list[str] | tuple[str, ...] | None = None,
    ) -> list[RecordEnvelope] | None:
        with self._lock:
            scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
            return self.sqlite.list_records_by_meta_value(
                kinds=kinds,
                scope=scope_ref,
                meta_key=meta_key,
                meta_value=meta_value,
                status=status,
                limit=limit,
                source_ids=source_ids,
            )

    def latest_record_by_meta_value_exact_scope(
        self,
        *,
        kind: str,
        source: str,
        status: str,
        scope: ScopeRef | dict,
        meta_key: str,
        meta_value: object,
    ) -> RecordEnvelope | None:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.latest_record_by_meta_value_exact_scope(
                kind=kind,
                source=source,
                status=status,
                scope=scope_ref,
                meta_key=meta_key,
                meta_value=meta_value,
            )

    def upsert_memory_edge(self, edge: MemoryEdge) -> MemoryEdge:
        with self._lock:
            try:
                result = self.sqlite.upsert_memory_edge(edge, commit=False)
                export = self.sqlite.enqueue_export(
                    stream="memory_edges",
                    payload=self._auxiliary_entry(
                        "memory_edges", result.to_dict(), scope=result.scope
                    ),
                    commit=False,
                )
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(export["operation_id"])
            return result

    def upsert_memory_edges(self, edges: list[MemoryEdge]) -> list[MemoryEdge]:
        with self._lock:
            try:
                results = self.sqlite.upsert_memory_edges(edges, commit=False)
                operation_ids = []
                for edge in results:
                    export = self.sqlite.enqueue_export(
                        stream="memory_edges",
                        payload=self._auxiliary_entry(
                            "memory_edges", edge.to_dict(), scope=edge.scope
                        ),
                        commit=False,
                    )
                    operation_ids.append(export["operation_id"])
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(*operation_ids)
            return results

    def list_memory_edges(
        self,
        *,
        scope: ScopeRef | dict | None = None,
        edge_types: list[str] | None = None,
        record_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[MemoryEdge]:
        with self._lock:
            scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
            return self.sqlite.list_memory_edges(
                scope=scope_ref,
                edge_types=edge_types,
                record_ids=record_ids,
                limit=limit,
            )

    def close(self) -> None:
        with self._lock:
            self.sqlite.close()

    def rebuild_sqlite_from_jsonl(self, *, replace: bool = False) -> dict:
        with self._lock:
            try:
                flush_report = self.flush_exports()
            except Exception as exc:
                return {
                    "ok": False,
                    "replace": bool(replace),
                    "replaced": False,
                    "errors": [{"line": 0, "offset": 0, "error": str(exc)}],
                }
            if flush_report["remaining"]:
                return {
                    "ok": False,
                    "replace": bool(replace),
                    "replaced": False,
                    "errors": [
                        {
                            "line": 0,
                            "offset": 0,
                            "error": "pending JSONL exports must be flushed before rebuild",
                        }
                    ],
                }
            if not replace:
                try:
                    counts = self._replay_jsonl_into(self.sqlite)
                except JsonlScanError as exc:
                    self.sqlite.conn.rollback()
                    return {
                        "ok": False,
                        "replace": False,
                        "replaced": False,
                        "errors": [exc.report],
                    }
                except Exception as exc:
                    self.sqlite.conn.rollback()
                    return {
                        "ok": False,
                        "replace": False,
                        "replaced": False,
                        "errors": [{"line": 0, "offset": 0, "error": str(exc)}],
                    }
                return {
                    "ok": True,
                    "root": str(self.root),
                    "replace": False,
                    "replaced": False,
                    "replayed": counts,
                }

            live_path = self.sqlite.path
            descriptor, temp_name = tempfile.mkstemp(
                prefix=f".{live_path.name}.rebuild-",
                suffix=".tmp",
                dir=live_path.parent,
            )

            os.close(descriptor)
            temporary_path = Path(temp_name)
            replacement: SqliteRecordStore | None = None
            try:
                replacement = SqliteRecordStore(
                    temporary_path,
                    auxiliary_log_dir=self.auxiliary_log_dir,
                )
                counts = self._replay_jsonl_into(replacement)
                replacement.conn.execute("DROP TABLE IF EXISTS temp.rebuild_seen_operations")
                replacement.conn.execute("DROP TABLE IF EXISTS temp.rebuild_expected")
                replacement.conn.commit()
                replacement.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                replacement.conn.execute("PRAGMA journal_mode=DELETE")
                replacement.close()
                replacement = None
                _fsync_file(temporary_path)

                self.sqlite.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self.sqlite.close()
                for sidecar in (
                    Path(str(live_path) + "-wal"),
                    Path(str(live_path) + "-shm"),
                ):
                    sidecar.unlink(missing_ok=True)
                try:
                    os.replace(temporary_path, live_path)
                    _fsync_directory(live_path.parent)
                finally:
                    self.sqlite = SqliteRecordStore(
                        live_path,
                        auxiliary_log_dir=self.auxiliary_log_dir,
                    )
                return {
                    "ok": True,
                    "root": str(self.root),
                    "replace": True,
                    "replaced": True,
                    "replayed": counts,
                }
            except JsonlScanError as exc:
                if replacement is not None:
                    replacement.close()
                _remove_sqlite_path(temporary_path)
                return {
                    "ok": False,
                    "replace": True,
                    "replaced": False,
                    "errors": [exc.report],
                }
            except Exception as exc:
                if replacement is not None:
                    replacement.close()
                _remove_sqlite_path(temporary_path)
                return {
                    "ok": False,
                    "replace": True,
                    "replaced": False,
                    "errors": [{"line": 0, "offset": 0, "error": str(exc)}],
                }

    def _replay_jsonl_into(self, target: SqliteRecordStore) -> dict[str, int]:
        counts = {
            "records": 0,
            "events": 0,
            "event_outcomes": 0,
            "intent_patterns": 0,
            "policy_rollout_ledger": 0,
            "memory_edges": 0,
        }
        previous_suppression = bool(target.suppress_auxiliary_logging)
        target.suppress_auxiliary_logging = True
        target.conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS rebuild_seen_operations ("
            "operation_id TEXT PRIMARY KEY, payload_digest TEXT NOT NULL)"
        )
        target.conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS rebuild_expected ("
            "table_name TEXT NOT NULL, item_key TEXT NOT NULL, "
            "PRIMARY KEY(table_name, item_key))"
        )
        try:
            for scanned in self.log.scan_strict():
                if not _accept_rebuild_operation(target, scanned):
                    continue
                record = RecordEnvelope.from_dict(scanned.payload)
                target.upsert(record, commit=False)
                target.conn.execute(
                    "INSERT OR IGNORE INTO temp.rebuild_expected VALUES ('records', ?)",
                    (target._storage_key(record),),
                )
                counts["records"] += 1
            for stream in (
                "events",
                "event_outcomes",
                "intent_patterns",
                "policy_rollout_ledger",
                "memory_edges",
            ):
                for scanned in self._auxiliary_log(stream).scan_strict():
                    if not _accept_rebuild_operation(target, scanned):
                        continue
                    entry = _auxiliary_entry_from_payload(scanned.payload)
                    payload = entry["payload"]
                    scope = entry["scope"]
                    item_key = ""
                    if stream == "events":
                        result = target.record_event(payload, scope=scope, commit=False)
                        item_key = str(result.get("id") or "")
                    elif stream == "event_outcomes":
                        event_id = str(payload.get("event_id") or "")
                        if not event_id:
                            raise ValueError("event outcome rebuild row lacks event_id")
                        result = target.record_outcome(
                            event_id,
                            payload,
                            scope=scope,
                            commit=False,
                            apply_rollbacks=False,
                        )
                        item_key = str(result.get("id") or "")
                    elif stream == "intent_patterns":
                        result = target.upsert_intent_pattern(
                            payload,
                            scope=scope,
                            commit=False,
                        )
                        item_key = str(result.get("id") or "")
                    elif stream == "policy_rollout_ledger":
                        result = target.upsert_policy_rollout_ledger_payload(
                            payload,
                            commit=False,
                        )
                        item_key = str(result.get("id") or "")
                    else:
                        edge = MemoryEdge.from_dict(payload)
                        target.upsert_memory_edge(edge, commit=False)
                        item_key = edge.edge_id
                    if not item_key:
                        raise ValueError(f"{stream} rebuild row lacks stable id")
                    target.conn.execute(
                        "INSERT OR IGNORE INTO temp.rebuild_expected VALUES (?, ?)",
                        (stream, item_key),
                    )
                    counts[stream] += 1
            _validate_rebuild_counts(target)
            target.conn.commit()
            return counts
        except Exception:
            target.conn.rollback()
            raise
        finally:
            target.suppress_auxiliary_logging = previous_suppression

    def flush_exports(
        self,
        *,
        limit: int = 1_000,
        operation_ids: list[str] | None = None,
    ) -> dict:
        with self._lock:
            pending = self.sqlite.pending_exports(
                limit=limit,
                operation_ids=operation_ids,
            )
            exported = 0
            for item in pending:
                stream = str(item["stream"])
                log = self.log if stream == "records" else self._auxiliary_log(stream)
                log.append_payload(
                    item["payload"],
                    operation_id=item["operation_id"],
                    expected_digest=item["payload_digest"],
                )
                self.sqlite.mark_exported(item["operation_id"])
                exported += 1
            remaining = int(
                self.sqlite.conn.execute(
                    "SELECT COUNT(*) FROM export_outbox WHERE state = 'pending'"
                ).fetchone()[0]
            )
            return {
                "ok": remaining == 0 or bool(operation_ids),
                "exported": exported,
                "remaining": remaining,
            }

    def maintain_storage(
        self,
        *,
        outbox_keep: int = 10_000,
        force_jsonl_cleanup: bool = False,
    ) -> dict:
        with self._lock:
            flush = self.flush_exports()
            jsonl_segments = self.log.resegment_oversized(
                force_cleanup=force_jsonl_cleanup
            )
            auxiliary_jsonl_segments = {
                name: self._auxiliary_log(name).resegment_oversized(
                    force_cleanup=force_jsonl_cleanup
                )
                for name in self._auxiliary_log_names_for_maintenance()
            }
            maintenance = self.sqlite.maintain(outbox_keep=outbox_keep)
            return {
                **maintenance,
                "ok": (
                    flush["remaining"] == 0
                    and bool(jsonl_segments.get("ok"))
                    and all(
                        bool(report.get("ok"))
                        for report in auxiliary_jsonl_segments.values()
                    )
                    and bool(maintenance.get("ok"))
                ),
                "flush": flush,
                "jsonl_segments": jsonl_segments,
                "auxiliary_jsonl_segments": auxiliary_jsonl_segments,
            }

    def allocate_manifest_sequences(
        self,
        *,
        scope: ScopeRef | dict,
        capabilities: list[str],
        floor_by_capability: dict[str, int] | None = None,
    ) -> dict[str, int]:
        with self._lock:
            if scope is None:
                raise ValueError("manifest sequence allocation requires an explicit scope")
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.allocate_replay_manifest_sequences(
                scope=scope_ref,
                capabilities=capabilities,
                floor_by_capability=floor_by_capability,
            )

    def replay_manifest_sequence_state(
        self,
        *,
        scope: ScopeRef | dict,
        capabilities: list[str] | set[str],
    ) -> dict[str, dict[str, object]]:
        """Read replay high-water evidence from the bounded indexed projection."""

        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.replay_manifest_sequence_state(
                scope=scope_ref,
                capabilities=capabilities,
            )

    def _enqueue_record_exports(self, record: RecordEnvelope) -> list[dict]:
        exports = [
            self.sqlite.enqueue_export(
                stream="records",
                payload=record.to_dict(),
                commit=False,
            )
        ]
        manifest_projection = _replay_manifest_projection(record)
        if manifest_projection is not None:
            exports.append(
                self.sqlite.enqueue_export(
                    stream="replay_manifests",
                    payload=self._auxiliary_entry(
                        "replay_manifests",
                        manifest_projection,
                        scope=record.scope,
                    ),
                    commit=False,
                )
            )
        return exports

    def _flush_committed_exports(self, *operation_ids: str) -> dict:
        recent = self.sqlite.newest_pending_export_ids(limit=100)
        requested = list(dict.fromkeys([*operation_ids, *recent]))
        return self.flush_exports(operation_ids=requested)

    def _auxiliary_log(self, log_name: str) -> JsonlLog:
        clean_name = str(log_name or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", clean_name):
            raise ValueError("invalid auxiliary JSONL stream name")
        if clean_name not in self._auxiliary_logs:
            self._auxiliary_logs[clean_name] = JsonlLog(
                self.auxiliary_log_dir / f"{clean_name}.jsonl"
            )
        return self._auxiliary_logs[clean_name]

    def _auxiliary_log_names_for_maintenance(self) -> list[str]:
        names = set(self._auxiliary_logs)
        names.update(AUXILIARY_JSONL_STREAMS)
        return sorted(
            name
            for name in names
            if self._has_auxiliary_log_artifacts(name)
        )

    def _has_auxiliary_log_artifacts(self, name: str) -> bool:
        path = self.auxiliary_log_dir / f"{name}.jsonl"
        if path.exists():
            return True
        if path.with_name(f"{name}.segments.json").exists():
            return True
        if path.with_name(f"{name}.segments.backup.json").exists():
            return True
        patterns = (
            f"{name}.[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].jsonl",
            f"{name}.segment-*.jsonl",
            f".{name}.segments-*",
        )
        return any(
            next(self.auxiliary_log_dir.glob(pattern), None) is not None
            for pattern in patterns
        )

    def _auxiliary_entry(
        self,
        log_name: str,
        payload: dict,
        *,
        scope: ScopeRef,
    ) -> dict:
        return {
            "log_type": str(log_name),
            "scope": _scope_to_dict(scope),
            "payload": dict(payload or {}),
        }

    def _existing_terminal_event(self, event_id: str, *, scope: ScopeRef) -> dict | None:
        row = self.sqlite.conn.execute(
            """SELECT payload_json FROM events
               WHERE id = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?""",
            (
                event_id,
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
            ),
        ).fetchone()
        return json.loads(str(row["payload_json"])) if row is not None else None

    def _existing_terminal_outcome(self, outcome_id: str, *, scope: ScopeRef) -> dict | None:
        row = self.sqlite.conn.execute(
            """SELECT payload_json FROM event_outcomes
               WHERE id = ? AND tenant_id = ? AND agent_id = ? AND workspace_id = ? AND user_id = ?""",
            (
                outcome_id,
                scope.tenant_id,
                scope.agent_id,
                scope.workspace_id,
                scope.user_id,
            ),
        ).fetchone()
        return json.loads(str(row["payload_json"])) if row is not None else None

    def _existing_reflection_duplicate(self, record: RecordEnvelope) -> RecordEnvelope | None:
        if record.kind != "reflection":
            return None
        fingerprint = _reflection_fingerprint(record)
        if not fingerprint:
            return None
        report_type = str(business_metadata(record.meta).get("report_type") or record.provenance.get("report_type") or "")
        if report_type == "outcome_trace":
            return None
        release_identity = _record_release_identity(record)
        for existing in self.list_records(kinds=["reflection"], scope=record.scope, limit=200):
            if str(existing.source or "") != str(record.source or ""):
                continue
            if existing.source_id != record.source_id:
                continue
            existing_report_type = str(
                business_metadata(existing.meta).get("report_type") or existing.provenance.get("report_type") or ""
            )
            if existing_report_type != report_type:
                continue
            existing_release_identity = _record_release_identity(existing)
            if (release_identity or existing_release_identity) and existing_release_identity != release_identity:
                continue
            if _reflection_fingerprint(existing) == fingerprint:
                return existing
        return None


def _reflection_fingerprint(record: RecordEnvelope) -> str:
    content = record.content if isinstance(record.content, dict) else {}
    text = "\n".join(
        str(value or "").strip()
        for value in (
            record.title,
            record.summary,
            record.detail,
            content.get("text"),
            content.get("summary"),
            content.get("report"),
        )
        if str(value or "").strip()
    ).lower()
    return sha256(text.encode("utf-8")).hexdigest()[:24] if text else ""


def _deterministic_insert_once(record: RecordEnvelope) -> bool:
    if record.source == "eimemory.experience.outcome_trace":
        payload = record.content.get("payload") if isinstance(record.content, dict) else None
        if isinstance(payload, dict):
            terminal_source = str(payload.get("source") or "").strip()
            idempotency_key = str(payload.get("idempotency_key") or "").strip()
            if terminal_source in {
                "openclaw.agent_end",
                "openclaw.task_end",
                "codex.stop",
                "hermes.task_end",
            } and idempotency_key.startswith(f"{terminal_source}:"):
                return True
    return (
        record.kind == "memory"
        and str(record.source or "").endswith(".memory")
        and record.meta.get("authoritative") is True
        and str(record.meta.get("idempotency_key") or "").startswith("adapter.")
    )


def _record_release_identity(record: RecordEnvelope) -> tuple[str, str, str, str] | None:
    meta = business_metadata(record.meta)
    content = record.content if isinstance(record.content, dict) else {}
    identity = tuple(
        str(meta.get(key) or content.get(key) or "").strip()
        for key in (
            "release_commit",
            "release_version",
            "deployment_receipt_id",
            "release_session_id",
        )
    )
    return identity if all(identity) else None


def _replay_manifest_projection(record: RecordEnvelope) -> dict | None:
    content = record.content if isinstance(record.content, dict) else {}
    meta = record.meta if isinstance(record.meta, dict) else {}
    if (
        record.kind != "replay_result"
        or record.source != "eimemory.capability_replay"
        or str(meta.get("report_type") or content.get("report_type") or "")
        != "capability_replay_manifest"
    ):
        return None
    sequences = content.get("sequence_by_capability")
    if not isinstance(sequences, dict):
        sequences = {}
    normalized_sequences: dict[str, int] = {}
    for raw_capability, raw_sequence in sequences.items():
        capability = str(raw_capability or "").strip()
        try:
            sequence = int(raw_sequence)
        except (TypeError, ValueError):
            continue
        if capability and sequence > 0:
            normalized_sequences[capability] = sequence
    return {
        "record_id": record.record_id,
        "report_type": "capability_replay_manifest",
        "schema_version": str(content.get("schema_version") or meta.get("schema_version") or ""),
        "source": record.source,
        "status": record.status,
        "execution_id": str(content.get("execution_id") or ""),
        "manifest_digest": str(content.get("manifest_digest") or ""),
        "sequence_by_capability": normalized_sequences,
    }


def remove_sqlite_files(root: str | Path) -> list[str]:
    sqlite_path = Path(root) / "state" / "eimemory.sqlite"
    removed: list[str] = []
    for path in (sqlite_path, Path(str(sqlite_path) + "-wal"), Path(str(sqlite_path) + "-shm")):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    return removed


def _scope_to_dict(scope: ScopeRef) -> dict[str, str]:
    return {
        "tenant_id": scope.tenant_id,
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "user_id": scope.user_id,
    }


def _auxiliary_entry_from_payload(entry: dict) -> dict:
    if not isinstance(entry, dict):
        raise ValueError("auxiliary JSONL row payload must be an object")
    payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
    scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else payload.get("scope", {})
    if not isinstance(payload, dict):
        raise ValueError("auxiliary JSONL row payload must be an object")
    return {
        "payload": payload,
        "scope": ScopeRef.from_dict(scope if isinstance(scope, dict) else {}),
    }


def _accept_rebuild_operation(
    target: SqliteRecordStore,
    scanned: JsonlScanEntry,
) -> bool:
    if not scanned.operation_id:
        return True
    existing = target.conn.execute(
        "SELECT payload_digest FROM temp.rebuild_seen_operations WHERE operation_id = ?",
        (scanned.operation_id,),
    ).fetchone()
    if existing is not None:
        if str(existing["payload_digest"]) != scanned.payload_digest:
            raise ValueError("conflicting JSONL payloads share one operation id")
        return False
    target.conn.execute(
        "INSERT INTO temp.rebuild_seen_operations(operation_id, payload_digest) VALUES (?, ?)",
        (scanned.operation_id, scanned.payload_digest),
    )
    return True


def _validate_rebuild_counts(target: SqliteRecordStore) -> None:
    missing_rows = target.conn.execute(
        """
        SELECT expected.table_name, COUNT(*) AS missing_count
        FROM temp.rebuild_expected expected
        WHERE NOT EXISTS (
          SELECT 1
          FROM (
            SELECT 'records' AS table_name, storage_key AS item_key FROM records
            UNION ALL SELECT 'events', CAST(id AS TEXT) FROM events
            UNION ALL SELECT 'event_outcomes', CAST(id AS TEXT) FROM event_outcomes
            UNION ALL SELECT 'intent_patterns', CAST(id AS TEXT) FROM intent_patterns
            UNION ALL SELECT 'policy_rollout_ledger', CAST(id AS TEXT) FROM policy_rollout_ledger
            UNION ALL SELECT 'memory_edges', edge_id FROM memory_edges
          ) actual
          WHERE actual.table_name = expected.table_name
            AND actual.item_key = expected.item_key
        )
        GROUP BY expected.table_name
        ORDER BY expected.table_name
        """
    ).fetchall()
    if missing_rows:
        stream = str(missing_rows[0]["table_name"])
        missing = int(missing_rows[0]["missing_count"])
        raise ValueError(f"rebuild validation failed for {stream}: {missing} rows missing")


def _remove_sqlite_path(path: Path) -> None:
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
    ):
        candidate.unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR if os.name == "nt" else os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
