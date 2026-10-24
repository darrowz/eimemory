"""Bounded unpublished records for one reviewed paper candidate."""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.knowledge.evidence_gate import paper_source_evidence_reasons
from eimemory.storage.runtime_store import _stamp_canonical_hongtu_identity


MAX_PROMOTION_RECORDS = 512
MAX_PROMOTION_BYTES = 32 * 1024 * 1024
_KINDS = {"knowledge_candidate", "paper_source", "paper_extract", "claim_card",
          "entity_record", "relation_record", "knowledge_page", "replay_result"}


def _key(record_id: str, scope: ScopeRef, source_id: str) -> tuple[str, ...]:
    return (scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id, source_id, record_id)


class _PaperPromotionBatch:
    """Expose only the reads and buffered writes used by the paper pipeline.

    No unknown attribute is forwarded to the live store: an added mutation
    route must be reviewed before it can run during preparation. Artifact
    files remain content-addressed preparation, without a published record.
    """

    def __init__(self, store: Any, candidate: RecordEnvelope) -> None:
        if not callable(getattr(store, "mutate_records_atomically", None)):
            raise RuntimeError("paper_promotion_atomic_store_required")
        self._store = store
        self.root = store.root
        self._candidate = deepcopy(candidate)
        self._candidate_key = _key(candidate.record_id, candidate.scope, candidate.source_id)
        self._before: dict[tuple[str, ...], dict | None] = {}
        self._records: dict[tuple[str, ...], RecordEnvelope] = {}
        self._sizes: dict[tuple[str, tuple[str, ...]], int] = {}
        self._remember_before(self._candidate_key, candidate)

    def _reserve(self, category: str, key: tuple[str, ...], payload: dict | None) -> None:
        size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        slot = (category, key)
        if (len(set(self._before) | {key}) > MAX_PROMOTION_RECORDS
                or sum(self._sizes.values()) - self._sizes.get(slot, 0) + size > MAX_PROMOTION_BYTES):
            raise ValueError("paper_promotion_batch_too_large")
        self._sizes[slot] = size

    def _remember_before(self, key: tuple[str, ...], record: RecordEnvelope | None) -> None:
        payload = record.to_dict() if record is not None else None
        self._reserve("before", key, payload)
        self._before[key] = payload

    def append(self, record: RecordEnvelope) -> RecordEnvelope:
        if record.scope != self._candidate.scope or record.kind not in _KINDS:
            raise ValueError("paper_promotion_record_partition_or_kind_mismatch")
        key = _key(record.record_id, record.scope, record.source_id)
        if record.kind == "knowledge_candidate" and key != self._candidate_key:
            raise ValueError("paper_promotion_candidate_identity_mismatch")
        if key not in self._before:
            previous = self._store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
            if previous is not None and previous.kind != record.kind:
                raise ValueError("paper_promotion_record_kind_changed")
            self._remember_before(key, previous)
        prepared = deepcopy(record)
        _stamp_canonical_hongtu_identity(prepared)
        self._reserve("after", key, prepared.to_dict())
        self._records[key] = prepared
        return deepcopy(prepared)

    def rewrite(self, record: RecordEnvelope) -> RecordEnvelope:
        if self.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id) is None:
            raise ValueError("paper_promotion_rewrite_missing_record")
        return self.append(record)

    def upsert(self, record: RecordEnvelope, *, commit: bool = False) -> None:
        if commit:
            raise RuntimeError("paper_promotion_preparation_cannot_commit")
        self.append(record)

    def mutate_records_atomically(self, mutation):
        # Preserve a nested helper's transaction semantics inside this buffer.
        # The actual SQLite transaction is opened only by publish().
        saved = (dict(self._records), dict(self._before), dict(self._sizes))
        try:
            result, changed, edges = mutation(self)
            if edges or any(_key(item.record_id, item.scope, item.source_id) not in self._records
                            for item in changed):
                raise ValueError("paper_promotion_unsupported_staged_mutation")
            return result
        except BaseException:
            self._records, self._before, self._sizes = saved
            raise

    def get_by_exact_ref(self, record_id, *, scope, source_id, **kwargs):
        owner = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        key = _key(record_id, owner, source_id)
        if key in self._records:
            return deepcopy(self._records[key])
        if key == self._candidate_key:
            return deepcopy(self._candidate)
        return self._store.get_by_exact_ref(record_id, scope=owner, source_id=source_id, **kwargs)

    def get_by_id(self, record_id, scope=None, *, exact_scope=False):
        owner = None if scope is None else (scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope))
        if exact_scope and owner is None:
            raise ValueError("exact_scope_requires_scope")
        matches = [record for record in self._records.values() if record.record_id == record_id
                   and (owner is None or record.scope == owner)]
        if matches:
            return deepcopy(max(matches, key=lambda item: item.time.updated_at))
        if record_id == self._candidate.record_id and (owner is None or owner == self._candidate.scope):
            return deepcopy(self._candidate)
        return self._store.get_by_id(record_id, scope=owner, exact_scope=exact_scope)

    def get_by_idempotency_key(self, *, kinds, scope, idempotency_key):
        owner = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
        for record in self._records.values():
            if record.kind in kinds and record.scope == owner and record.meta.get("idempotency_key") == idempotency_key:
                return deepcopy(record)
        return self._store.get_by_idempotency_key(kinds=kinds, scope=owner, idempotency_key=idempotency_key)

    def list_records(self, *, kinds=None, **kwargs):
        # Release identity reads use other record kinds. A broad query that
        # could need overlay merging must never silently omit prepared writes.
        if not kinds or set(kinds) & _KINDS:
            raise RuntimeError("paper_promotion_staged_query_unsupported")
        return self._store.list_records(kinds=kinds, **kwargs)

    def latest_record_by_meta_value_exact_scope(self, *, kind, **kwargs):
        if kind in _KINDS:
            raise RuntimeError("paper_promotion_staged_query_unsupported")
        return self._store.latest_record_by_meta_value_exact_scope(kind=kind, **kwargs)

    def publish(self) -> None:
        candidate = self._candidate
        completion = self._records.get(self._candidate_key)
        if completion is None or completion.status != "promoted":
            raise ValueError("paper_promotion_candidate_completion_missing")

        def mutation(sqlite):
            current = sqlite.get_by_exact_ref(candidate.record_id, scope=candidate.scope, source_id=candidate.source_id)
            if current is None or current.to_dict() != self._before[self._candidate_key]:
                raise ValueError("paper_promotion_candidate_changed_retry_required")
            for key, record in self._records.items():
                previous = sqlite.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
                if (previous.to_dict() if previous is not None else None) != self._before[key]:
                    raise ValueError("paper_promotion_record_changed_retry_required")
                idempotency_key = str(record.meta.get("idempotency_key") or "")
                if idempotency_key:
                    existing = sqlite.get_by_idempotency_key(kinds=[record.kind], scope=record.scope,
                                                            idempotency_key=idempotency_key)
                    if existing is not None and (existing.record_id != record.record_id
                                                or existing.scope != record.scope
                                                or existing.source_id != record.source_id):
                        raise ValueError("paper_promotion_idempotency_changed_retry_required")
            records = list(self._records.values())
            if any(paper_source_evidence_reasons(records, store=sqlite)):
                raise ValueError("paper_promotion_source_not_eligible")
            for record in records:
                sqlite.upsert(record, commit=False)
            return None, records, []

        self._store.mutate_records_atomically(mutation)
