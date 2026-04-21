from __future__ import annotations

from pathlib import Path

from eimemory.adapters.openclaw.qmd_export import export_record_markdown
from eimemory.storage.jsonl import JsonlLog
from eimemory.storage.sqlite_store import SqliteRecordStore
from eimemory.models.records import RecordEnvelope, ScopeRef


class RuntimeStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = JsonlLog(self.root / "records.jsonl")
        self.sqlite = SqliteRecordStore(self.root / "state" / "eimemory.sqlite")

    def append(self, record: RecordEnvelope) -> RecordEnvelope:
        self.log.append(record)
        self.sqlite.upsert(record)
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
    ) -> tuple[list[RecordEnvelope], dict]:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.search_with_diagnostics(query=query, kinds=kinds, scope=scope_ref, limit=limit)

    def get_active_policy(self, *, task_type: str, scope: ScopeRef | dict | None = None) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.get_active_policy(task_type=task_type, scope=scope_ref)

    def get_by_id(self, record_id: str) -> RecordEnvelope | None:
        return self.sqlite.get_by_id(record_id)

    def get_many_by_ids(self, record_ids: list[str]) -> list[RecordEnvelope]:
        resolved: list[RecordEnvelope] = []
        seen: set[str] = set()
        for record_id in record_ids:
            if record_id in seen:
                continue
            seen.add(record_id)
            record = self.get_by_id(record_id)
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
    ) -> list[RecordEnvelope]:
        scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
        return self.sqlite.list_records(kinds=kinds, scope=scope_ref, status=status, limit=limit, offset=offset)

    def close(self) -> None:
        self.sqlite.close()
