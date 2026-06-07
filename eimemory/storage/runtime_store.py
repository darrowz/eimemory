from __future__ import annotations

from pathlib import Path
from hashlib import sha256

from eimemory.adapters.openclaw.qmd_export import export_record_markdown
from eimemory.metadata import business_metadata
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
        return self.sqlite.record_event(payload, scope=scope_ref)

    def record_outcome(self, event_id: str, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.record_outcome(event_id, payload, scope=scope_ref)

    def upsert_intent_pattern(self, payload: dict, *, scope: ScopeRef | dict | None = None) -> dict:
        scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        return self.sqlite.upsert_intent_pattern(payload, scope=scope_ref)

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
    ) -> list[RecordEnvelope]:
        scope_ref = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
        return self.sqlite.list_records(kinds=kinds, scope=scope_ref, status=status, limit=limit, offset=offset)

    def close(self) -> None:
        self.sqlite.close()

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
