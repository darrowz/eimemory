from __future__ import annotations

import os
from pathlib import Path
from hashlib import sha256
from threading import RLock
import tempfile

from eimemory.adapters.openclaw.qmd_export import export_record_markdown
from eimemory.metadata import business_metadata
from eimemory.models.memory_edges import MemoryEdge
from eimemory.storage.jsonl import JsonlLog, JsonlScanEntry, JsonlScanError
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.models.records import RecordEnvelope, ScopeRef


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
                self.sqlite.upsert(record, commit=False)
                export = self.sqlite.enqueue_export(
                    stream="records",
                    payload=record.to_dict(),
                    commit=False,
                )
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(export["operation_id"])
            export_record_markdown(self.root, record)
            return record

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
                export = self.sqlite.enqueue_export(
                    stream="records",
                    payload=record.to_dict(),
                    commit=False,
                )
                self.sqlite.conn.commit()
            except Exception:
                self.sqlite.conn.rollback()
                raise
            self._flush_committed_exports(export["operation_id"])
            export_record_markdown(self.root, record)
            return record

    def search(
        self,
        *,
        query: str,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        limit: int = 10,
    ) -> list[RecordEnvelope]:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.search(query=query, kinds=kinds, scope=scope_ref, limit=limit)

    def search_with_diagnostics(
        self,
        *,
        query: str,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        limit: int = 10,
        recall_filters: dict | None = None,
    ) -> tuple[list[RecordEnvelope], dict]:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.search_with_diagnostics(
                query=query,
                kinds=kinds,
                scope=scope_ref,
                limit=limit,
                recall_filters=recall_filters,
            )

    def get_active_policy(self, *, task_type: str, scope: ScopeRef | dict | None = None) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.get_active_policy(task_type=task_type, scope=scope_ref)

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
    ) -> dict:
        with self._lock:
            scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            return self.sqlite.search_policy(user_phrase, scope=scope_ref, context=context, limit=limit)

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
            )

    def count_records(
        self,
        *,
        kinds: list[str] | None = None,
        scope: ScopeRef | dict | None = None,
        status: str | None = None,
    ) -> int:
        with self._lock:
            scope_ref = None if scope is None else (
                scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
            )
            return self.sqlite.count_records(
                kinds=kinds,
                scope=scope_ref,
                status=status,
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

    def maintain_storage(self, *, outbox_keep: int = 10_000) -> dict:
        with self._lock:
            flush = self.flush_exports()
            maintenance = self.sqlite.maintain(outbox_keep=outbox_keep)
            return {
                **maintenance,
                "ok": flush["remaining"] == 0 and bool(maintenance.get("ok")),
                "flush": flush,
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

    def _flush_committed_exports(self, *operation_ids: str) -> dict:
        recent = self.sqlite.newest_pending_export_ids(limit=100)
        requested = list(dict.fromkeys([*operation_ids, *recent]))
        return self.flush_exports(operation_ids=requested)

    def _auxiliary_log(self, log_name: str) -> JsonlLog:
        clean_name = str(log_name or "").strip()
        if clean_name not in self._auxiliary_logs:
            self._auxiliary_logs[clean_name] = JsonlLog(
                self.auxiliary_log_dir / f"{clean_name}.jsonl"
            )
        return self._auxiliary_logs[clean_name]

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

    def _existing_reflection_duplicate(self, record: RecordEnvelope) -> RecordEnvelope | None:
        if record.kind != "reflection":
            return None
        fingerprint = _reflection_fingerprint(record)
        if not fingerprint:
            return None
        report_type = str(business_metadata(record.meta).get("report_type") or record.provenance.get("report_type") or "")
        if report_type == "outcome_trace":
            return None
        for existing in self.list_records(kinds=["reflection"], scope=record.scope, limit=200):
            if str(existing.source or "") != str(record.source or ""):
                continue
            existing_report_type = str(
                business_metadata(existing.meta).get("report_type") or existing.provenance.get("report_type") or ""
            )
            if existing_report_type != report_type:
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
