from __future__ import annotations

import json
from pathlib import Path
from hashlib import sha256

from eimemory.adapters.openclaw.qmd_export import export_record_markdown
from eimemory.metadata import business_metadata
from eimemory.models.memory_edges import MemoryEdge
from eimemory.storage.jsonl import JsonlLog
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.models.records import RecordEnvelope, ScopeRef


class RuntimeStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = JsonlLog(self.root / "records.jsonl")
        self.auxiliary_log_dir = self.root / "state"
        self.sqlite = SqliteRecordStore(self.root / "state" / "eimemory.sqlite", auxiliary_log_dir=self.auxiliary_log_dir)

    def append(self, record: RecordEnvelope) -> RecordEnvelope:
        existing = self._existing_reflection_duplicate(record)
        if existing is not None:
            return existing
        self.log.append(record)
        self.sqlite.upsert(record)
        export_record_markdown(self.root, record)
        return record

    def rewrite(self, record: RecordEnvelope, *, previous_scope: ScopeRef | dict | None = None) -> RecordEnvelope:
        previous_scope_ref = (
            previous_scope
            if isinstance(previous_scope, ScopeRef)
            else (None if previous_scope is None else ScopeRef.from_dict(previous_scope))
        )
        self.log.append(record)
        self.sqlite.rewrite(record, previous_scope=previous_scope_ref)
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
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.search_with_diagnostics(
            query=query,
            kinds=kinds,
            scope=scope_ref,
            limit=limit,
            recall_filters=recall_filters,
        )

    def get_active_policy(self, *, task_type: str, scope: ScopeRef | dict | None = None) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.get_active_policy(task_type=task_type, scope=scope_ref)

    def record_event(self, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        result = self.sqlite.record_event(payload, scope=scope_ref)
        self._append_auxiliary_log("events", result, scope=scope_ref)
        return result

    def record_outcome(self, event_id: str, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        result = self.sqlite.record_outcome(event_id, payload, scope=scope_ref)
        self._append_auxiliary_log("event_outcomes", result, scope=scope_ref)
        return result

    def upsert_intent_pattern(self, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        result = self.sqlite.upsert_intent_pattern(payload, scope=scope_ref)
        self._append_auxiliary_log("intent_patterns", result, scope=scope_ref)
        return result

    def search_policy(
        self,
        user_phrase: str,
        *,
        scope: ScopeRef | dict | None = None,
        context: dict | None = None,
        limit: int = 5,
    ) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.search_policy(user_phrase, scope=scope_ref, context=context, limit=limit)

    def get_policy_rollout_ledger(
        self,
        *,
        scope: ScopeRef | dict | None = None,
        action: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
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
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.rollback_intent_pattern(pattern_id, scope=scope_ref, reason=reason, auto=auto)

    def get_by_id(self, record_id: str, scope: ScopeRef | dict | None = None) -> RecordEnvelope | None:
        scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
        return self.sqlite.get_by_id(record_id, scope=scope_ref)

    def get_by_idempotency_key(
        self,
        *,
        kinds: list[str],
        scope: ScopeRef | dict | None,
        idempotency_key: str,
    ) -> RecordEnvelope | None:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.get_by_idempotency_key(kinds=kinds, scope=scope_ref, idempotency_key=idempotency_key)

    def get_many_by_ids(self, record_ids: list[str], scope: ScopeRef | dict | None = None) -> list[RecordEnvelope]:
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

    def upsert_memory_edge(self, edge: MemoryEdge) -> MemoryEdge:
        result = self.sqlite.upsert_memory_edge(edge)
        self._append_auxiliary_log("memory_edges", result.to_dict(), scope=result.scope)
        return result

    def upsert_memory_edges(self, edges: list[MemoryEdge]) -> list[MemoryEdge]:
        results = self.sqlite.upsert_memory_edges(edges)
        for edge in results:
            self._append_auxiliary_log("memory_edges", edge.to_dict(), scope=edge.scope)
        return results

    def list_memory_edges(
        self,
        *,
        scope: ScopeRef | dict | None = None,
        edge_types: list[str] | None = None,
        record_ids: list[str] | None = None,
        limit: int = 100,
    ) -> list[MemoryEdge]:
        scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
        return self.sqlite.list_memory_edges(
            scope=scope_ref,
            edge_types=edge_types,
            record_ids=record_ids,
            limit=limit,
        )

    def close(self) -> None:
        self.sqlite.close()

    def rebuild_sqlite_from_jsonl(self, *, replace: bool = False) -> dict:
        if replace:
            self.close()
            remove_sqlite_files(self.root)
            self.sqlite = SqliteRecordStore(self.root / "state" / "eimemory.sqlite", auxiliary_log_dir=self.auxiliary_log_dir)
        counts = {
            "records": 0,
            "events": 0,
            "event_outcomes": 0,
            "intent_patterns": 0,
            "policy_rollout_ledger": 0,
            "memory_edges": 0,
        }
        previous_suppression = bool(getattr(self.sqlite, "suppress_auxiliary_logging", False))
        self.sqlite.suppress_auxiliary_logging = True
        try:
            for payload in _iter_jsonl_payloads(self.log.path):
                self.sqlite.upsert(RecordEnvelope.from_dict(payload))
                counts["records"] += 1
            for entry in _iter_auxiliary_entries(self.auxiliary_log_dir / "events.jsonl"):
                self.sqlite.record_event(entry["payload"], scope=entry["scope"], commit=False)
                counts["events"] += 1
            for entry in _iter_auxiliary_entries(self.auxiliary_log_dir / "event_outcomes.jsonl"):
                event_id = str(entry["payload"].get("event_id") or "")
                if event_id:
                    self.sqlite.record_outcome(
                        event_id,
                        entry["payload"],
                        scope=entry["scope"],
                        commit=False,
                        apply_rollbacks=False,
                    )
                    counts["event_outcomes"] += 1
            for entry in _iter_auxiliary_entries(self.auxiliary_log_dir / "intent_patterns.jsonl"):
                self.sqlite.upsert_intent_pattern(entry["payload"], scope=entry["scope"], commit=False)
                counts["intent_patterns"] += 1
            for entry in _iter_auxiliary_entries(self.auxiliary_log_dir / "policy_rollout_ledger.jsonl"):
                self.sqlite.upsert_policy_rollout_ledger_payload(entry["payload"], commit=False)
                counts["policy_rollout_ledger"] += 1
            self.sqlite.conn.commit()
            edge_batch: list[MemoryEdge] = []
            for entry in _iter_auxiliary_entries(self.auxiliary_log_dir / "memory_edges.jsonl"):
                edge_batch.append(MemoryEdge.from_dict(entry["payload"]))
            if edge_batch:
                self.sqlite.upsert_memory_edges(edge_batch)
                counts["memory_edges"] = len(edge_batch)
        finally:
            self.sqlite.suppress_auxiliary_logging = previous_suppression
        return {"ok": True, "root": str(self.root), "replace": bool(replace), "replayed": counts}

    def _append_auxiliary_log(self, log_name: str, payload: dict, *, scope: ScopeRef) -> None:
        if bool(getattr(self.sqlite, "suppress_auxiliary_logging", False)):
            return
        path = self.auxiliary_log_dir / f"{log_name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "log_type": str(log_name),
            "scope": _scope_to_dict(scope),
            "payload": dict(payload or {}),
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

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


def _iter_jsonl_payloads(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _iter_auxiliary_entries(path: Path):
    for entry in _iter_jsonl_payloads(path):
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
        scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else payload.get("scope", {})
        if isinstance(payload, dict):
            yield {"payload": payload, "scope": ScopeRef.from_dict(scope if isinstance(scope, dict) else {})}
