"""Current consumption of stored evidence and its native source bindings.

History remains in storage. Answer, recall and brief consumers must not reuse
that history after an authoritative input is withdrawn or replaced. All reads
in a call share one exact-owner snapshot, cache, deadline and lookup budget.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
import json
import re
import sqlite3
from time import perf_counter
from types import SimpleNamespace
from typing import Any

from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


MAX_CURRENT_EVIDENCE_LOOKUPS = 4096
MAX_SOURCE_DEPTH = 32
_SCOPE_FIELDS = ("tenant_id", "agent_id", "workspace_id", "user_id")
_REF_FIELDS = {"record_id", "kind", "scope", "source_id", "version_digest"}
WITHDRAWN_STATUSES = frozenset({
    "removed", "superseded", "rejected", "quarantined", "deprecated",
    "expired", "refuted", "inactive", "conflicted", "archived",
})
_READ_ERRORS = (OSError, ValueError, TypeError, RuntimeError, sqlite3.Error)


def inactive_evidence_reason(payload: Mapping[str, Any]) -> str:
    meta = payload.get("meta")
    meta = business_metadata(meta if isinstance(meta, Mapping) else {})
    if (str(payload.get("status") or "").strip().lower() in WITHDRAWN_STATUSES
            or str(meta.get("mutation_state") or "").strip().lower() in WITHDRAWN_STATUSES
            or str(meta.get("superseded_by") or "").strip()):
        return "inactive_status"
    return ""


def current_evidence_reasons(records: list[Any], *, store=None, deadline_at: float = 0.0) -> list[str]:
    payloads = [_payload(record) for record in records]
    if not records or store is None:
        # Pure builders still accept caller-owned, unpersisted records. Only a
        # Runtime supplies the store needed to certify current source versions.
        return [inactive_evidence_reason(payload) for payload in payloads]

    def read(reader):
        gate = _CurrentEvidence(reader, deadline_at=deadline_at)
        return [gate.check_input(payload) for payload in payloads]

    try:
        consistent = getattr(store, "read_consistent", None)
        if deadline_at:
            from eimemory.storage.recall_deadline import recall_read_scope

            with recall_read_scope(store, {"_recall_collection_deadline_monotonic": deadline_at}):
                return consistent(read) if callable(consistent) else read(store)
        return consistent(read) if callable(consistent) else read(store)
    except _READ_ERRORS:
        return [inactive_evidence_reason(payload) or "evidence_unavailable" for payload in payloads]


class _CurrentEvidence:
    def __init__(self, reader, *, deadline_at):
        self.reader = reader
        self.deadline_at = deadline_at
        self.cache: dict[tuple, RecordEnvelope | None] = {}
        self.attempted: set[tuple] = set()
        self.failed: set[tuple] = set()
        self.checked: dict[tuple, str] = {}
        self.visiting: set[tuple] = set()
        # Reuse the paper contract without opening another transaction or
        # multiplying its read budget for each recursive source.
        self.paper_reader = SimpleNamespace(get_by_exact_ref=self.lookup)

    def lookup(self, record_id, *, scope, source_id, preserve_unknown_occurred_at=False, **_options):
        self._deadline()
        # Ordinary envelope/version checks must use the same hydration as the
        # producer. L1 explicitly requests the narrower source-clock view.
        # Distinguish the views in the shared cache and charge both reads to
        # the same finite budget; neither may authorize the other's result.
        strict_clock = preserve_unknown_occurred_at is True
        key = (*asdict(scope).values(), source_id, record_id, strict_clock)
        if key in self.failed:
            raise RuntimeError("current_evidence_unavailable")
        if key not in self.cache:
            if len(self.attempted) >= MAX_CURRENT_EVIDENCE_LOOKUPS:
                raise ValueError("current_evidence_lookup_limit")
            self.attempted.add(key)
            try:
                self.cache[key] = self.reader.get_by_exact_ref(
                    record_id, scope=scope, source_id=source_id,
                    preserve_unknown_occurred_at=strict_clock, fail_on_unavailable=True,
                )
            except _READ_ERRORS:
                self.failed.add(key)
                raise
        return self.cache[key]

    def _deadline(self):
        if self.deadline_at and perf_counter() >= self.deadline_at:
            raise ValueError("current_evidence_deadline")

    def check_input(self, payload):
        if reason := inactive_evidence_reason(payload):
            return reason
        try:
            owner = _owner(payload.get("scope"))
            record_id, source_id = payload.get("record_id"), payload.get("source_id")
            if owner is None or not _identifier(record_id) or not _identifier(source_id):
                return "evidence_identity_invalid"
            current = self.lookup(record_id, scope=owner, source_id=source_id)
            if current is None:
                return "evidence_missing"
            if current.kind != payload.get("kind"):
                return "evidence_identity_invalid"
            if reason := inactive_evidence_reason(current.to_dict()):
                return reason
            # No guessed defaults or mutable object receipt: compare the actual
            # full envelope consumed by the caller with its current hydration.
            if _digest(payload) != _digest(current.to_dict()):
                return "evidence_changed"
            return self.check_current(current, depth=0)
        except _READ_ERRORS:
            return "evidence_unavailable"

    def check_current(self, record, *, depth):
        self._deadline()
        key = (*asdict(record.scope).values(), record.source_id, record.record_id)
        if key in self.visiting or depth > MAX_SOURCE_DEPTH:
            return "source_lineage_invalid"
        if key in self.checked:
            return self.checked[key]
        self.visiting.add(key)
        try:
            reason = self._check_current(record, depth=depth)
            self._deadline()
            self.checked[key] = reason
            return reason
        finally:
            self.visiting.remove(key)

    def _check_current(self, record, *, depth):
        payload = record.to_dict()
        if inactive_evidence_reason(payload):
            return "source_evidence_inactive"
        from eimemory.knowledge.evidence_gate import paper_source_evidence_reasons

        paper_reason = paper_source_evidence_reasons([record], store=self.paper_reader)[0]
        if paper_reason:
            return paper_reason
        meta = business_metadata(record.meta)
        if record.kind == "reflection" and record.source == "eimemory.daily_brief":
            return self._brief(record, depth=depth)
        binding = meta.get("source_event_binding")
        if record.kind == "memory" and (meta.get("capture_origin") == "l1_extract"
                or isinstance(binding, Mapping) and binding.get("schema") == "l1_source_event.v1"):
            return self._l1(record, depth=depth)
        context = record.provenance.get("project_context")
        if record.kind == "memory" and (meta.get("capture_origin") == "same_turn_context"
                or isinstance(context, Mapping) and context.get("schema") == "same_turn_release_context.v1"):
            return self._turn_context(record, depth=depth)
        containers = (meta, record.content, record.provenance)
        projection = any(container.get("projection_type") == "operational_knowledge" for container in containers)
        declared = any(key in container for container in containers
                       for key in ("source_record_ref", "supporting_claim_refs", "projection_lineage_schema"))
        if record.kind == "memory" and (record.source == "eimemory.knowledge.projectors" or projection and declared):
            return self._projection(record, depth=depth)
        if record.kind == "knowledge_page" and any(
                key in record.content for key in ("supporting_claim_refs", "supporting_claim_refs_schema")):
            if record.content.get("supporting_claim_refs_schema") != "knowledge.projection_support.v1":
                return "source_lineage_invalid"
            refs = record.content.get("supporting_claim_refs")
            if not isinstance(refs, list) or not refs:
                return "source_lineage_invalid"
            ids = record.content.get("supporting_claim_ids")
            if (not isinstance(ids, (list, tuple)) or any(not _identifier(item) for item in ids)
                    or len(set(ids)) != len(ids) or len(ids) != len(refs)
                    or set(ids) != {ref.get("record_id") for ref in refs if isinstance(ref, dict)}):
                return "source_lineage_invalid"
            return self._refs(record, refs, depth=depth, kinds={"claim_card"})
        return ""

    def _refs(self, record, refs, *, depth, kinds=None, allowed_scopes=None, allow_sources=False):
        if not isinstance(refs, list) or len(refs) > MAX_CURRENT_EVIDENCE_LOOKUPS:
            return "source_lineage_invalid"
        seen = set()
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != _REF_FIELDS
                    or not _identifier(ref.get("record_id")) or not _identifier(ref.get("source_id"))
                    or not isinstance(ref.get("version_digest"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", ref["version_digest"]) is None
                    or kinds is not None and ref.get("kind") not in kinds):
                return "source_lineage_invalid"
            owner = _owner(ref.get("scope"))
            scopes = allowed_scopes if allowed_scopes is not None else {tuple(asdict(record.scope).values())}
            if (owner is None or tuple(asdict(owner).values()) not in scopes
                    or not allow_sources and ref["source_id"] != record.source_id):
                return "source_lineage_invalid"
            key = (*asdict(owner).values(), ref["source_id"], ref["record_id"])
            if key in seen:
                return "source_lineage_invalid"
            seen.add(key)
            parent = self.lookup(ref["record_id"], scope=owner, source_id=ref["source_id"])
            if parent is None:
                return "source_evidence_missing"
            if inactive_evidence_reason(parent.to_dict()):
                return "source_evidence_inactive"
            if versioned_record_ref(parent) != ref:
                return "source_evidence_changed"
            if reason := self.check_current(parent, depth=depth + 1):
                return reason
        return ""

    def _brief(self, record, *, depth):
        content = record.content
        schema = content.get("source_lineage_schema")
        if schema is None and "source_record_refs" not in content:
            return "source_lineage_missing"
        refs, brief, delivery = (content.get(key) for key in ("source_record_refs", "brief", "delivery"))
        if (schema != "daily_brief_sources.v1" or record.provenance.get("source_lineage_schema") != schema
                or not isinstance(brief, Mapping) or not isinstance(delivery, Mapping)
                or brief.get("source_lineage_schema") != schema or brief.get("source_record_refs") != refs):
            return "source_lineage_invalid"
        outbox = delivery.get("outbox")
        body = outbox.get("body") if isinstance(outbox, Mapping) else None
        if not isinstance(body, Mapping) or body != brief:
            return "source_lineage_invalid"
        if not isinstance(refs, list) or len(refs) > 2500:
            return "source_lineage_invalid"
        body_ids = set()
        for section in ("new_memories", "decisions", "followups", "research_digest", "news_digest"):
            items = brief.get(section)
            if section.endswith("_digest") and isinstance(items, Mapping):
                items = items.get("items")
            if not isinstance(items, list) or any(not isinstance(item, Mapping) for item in items):
                return "source_lineage_invalid"
            body_ids.update(item.get("record_id") for item in items)
        if body_ids != {ref.get("record_id") for ref in refs if isinstance(ref, dict)}:
            return "source_lineage_invalid"
        # Match the producer's existing list_records visibility: the requested
        # owner, its explicit legacy query aliases and inherited user='' rows.
        # Hydration still uses each source's exact four-field owner and source.
        from eimemory.identity import hongtu_query_scopes

        scopes = set()
        for scope in hongtu_query_scopes(record.scope):
            owner = tuple(asdict(scope).values())
            scopes.add(owner)
            scopes.add((*owner[:3], ""))
        return self._refs(record, refs, depth=depth, allowed_scopes=scopes, allow_sources=True)

    def _l1(self, record, *, depth):
        from eimemory.contracts.l1_source_order import L1SourceOrderBlocked, validate_l1_source_event

        binding = business_metadata(record.meta).get("source_event_binding")
        if binding is None:
            # Legacy L1 atoms carry only an exact-owner derived_from link.
            ids = {link.target_id for link in record.links
                   if link.relation == "derived_from" and link.target_kind in {"memory", "record"}}
            if len(ids) != 1:
                return "source_lineage_missing"
            return self._parent(record, next(iter(ids)), kind="memory", depth=depth)
        try:
            parent = validate_l1_source_event(record, binding, get_exact=self.lookup)
        except L1SourceOrderBlocked as exc:
            return exc.code
        return self.check_current(parent, depth=depth + 1)

    def _parent(self, record, record_id, *, kind, depth):
        if not _identifier(record_id):
            return "source_lineage_invalid"
        parent = self.lookup(record_id, scope=record.scope, source_id=record.source_id)
        if parent is None:
            return "source_evidence_missing"
        if parent.kind != kind:
            return "source_lineage_invalid"
        if parent.status != "active":
            return "source_evidence_inactive"
        return self.check_current(parent, depth=depth + 1)

    def _turn_context(self, record, *, depth):
        context = record.provenance.get("project_context")
        if not isinstance(context, Mapping) or context.get("schema") != "same_turn_release_context.v1":
            return "source_lineage_invalid"
        parent_id, support = context.get("parent_record_id"), context.get("supporting_record_ids")
        if (not _identifier(parent_id) or not isinstance(support, list) or not 1 <= len(support) <= 64
                or any(not _identifier(value) for value in support) or len(set(support)) != len(support)
                or {link.target_id for link in record.links if link.relation == "derived_from"
                    and link.target_kind == "memory"} != {parent_id}
                or {link.target_id for link in record.links if link.relation == "supported_by"
                    and link.target_kind == "raw_chunk"} != set(support)):
            return "source_lineage_invalid"
        if reason := self._parent(record, parent_id, kind="memory", depth=depth):
            return reason
        parent = self.lookup(parent_id, scope=record.scope, source_id=record.source_id)
        if context.get("source_event_id") != business_metadata(parent.meta).get("source_event_id"):
            return "source_lineage_invalid"
        text = record.content.get("text")
        parent_text = parent.content.get("text")
        assistant = text.partition("\n")[2] if isinstance(text, str) else ""
        if (not assistant or not isinstance(parent_text, str) or assistant not in parent_text
                or sha256(assistant.encode()).hexdigest() != context.get("assistant_content_sha256")):
            return "source_evidence_changed"
        for record_id in support:
            if reason := self._parent(record, record_id, kind="raw_chunk", depth=depth):
                return reason
            raw = self.lookup(record_id, scope=record.scope, source_id=record.source_id)
            provenance = raw.provenance
            text = raw.content.get("text")
            if (provenance.get("schema") != context["schema"] or not isinstance(text, str)
                    or provenance.get("content_sha256") != sha256(text.encode()).hexdigest()
                    or any(provenance.get(key) != context.get(key) for key in (
                        "session_id", "source_event_id", "turn_start_user_message_id", "bound_assistant_message_id"))
                    or provenance.get("source_message_id") not in context.get("source_message_ids", [])):
                return "source_evidence_changed"
        return ""

    def _projection(self, record, *, depth):
        meta = business_metadata(record.meta)
        keys = {"source_record_ref", "supporting_claim_refs", "projection_lineage_schema"}
        containers = (meta, record.provenance, record.content)
        for container in containers:
            if any(key in container and container[key] != meta.get(key)
                   for key in ("source_record_id", "source_record_kind")):
                return "source_lineage_invalid"
        if not any(key in container for container in containers for key in keys):
            # The historical default partition format declared a parent ID.
            if record.source_id != "default":
                return "source_lineage_missing"
            return self._parent(record, meta.get("source_record_id"),
                                kind=meta.get("source_record_kind"), depth=depth)
        if (meta.get("projection_lineage_schema") != "knowledge.projection_lineage.v1"
                or any(meta.get(key) != record.provenance.get(key) for key in keys)
                or any(key in record.content and record.content[key] != meta.get(key) for key in keys)):
            return "source_lineage_invalid"
        parent_ref = meta.get("source_record_ref")
        if (not isinstance(parent_ref, Mapping)
                or parent_ref.get("record_id") != meta.get("source_record_id")
                or parent_ref.get("kind") != meta.get("source_record_kind")):
            return "source_lineage_invalid"
        if reason := self._refs(record, [meta.get("source_record_ref")], depth=depth,
                               kinds={"claim_card", "knowledge_page"}):
            return reason
        return self._refs(record, meta.get("supporting_claim_refs"), depth=depth, kinds={"claim_card"})


def _payload(record):
    return record.to_dict() if hasattr(record, "to_dict") else dict(record) if isinstance(record, Mapping) else {}


def _owner(value):
    if (not isinstance(value, Mapping) or set(value) != set(_SCOPE_FIELDS)
            or any(not isinstance(item, str) for item in value.values())):
        return None
    return ScopeRef.from_dict(dict(value))


def _identifier(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _digest(payload):
    return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str,
                             separators=(",", ":")).encode()).hexdigest()
