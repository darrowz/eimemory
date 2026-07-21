from __future__ import annotations

import json
from hashlib import sha256
from datetime import datetime, timedelta, timezone
import re
import unicodedata
from typing import Any, Mapping

from eimemory.adapters.runtime.channel import (
    AUTHORITY_MODE,
    RUNTIME_ADAPTER_CONTRACT_VERSION,
    base_scope_from_channel,
    normalize_runtime_channel,
    resolve_channel_scope,
)
from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.redaction import bounded_redacted_text
from eimemory.governance.evidence_contract import (
    current_release_identity,
    release_identity_from_record,
    release_identity_payload,
)
from eimemory.governance.tool_receipts import (
    ATTESTATION_PRODUCERS,
    MAX_ELIGIBLE_RECEIPTS_PER_RUN,
    STRUCTURED_TEST_POLICY_ID,
    TRUSTED_TEST_POLICY_IDS,
    V2_RECEIPT_VERSION,
    canonical_tool_receipt,
    sign_tool_receipt,
    verify_tool_receipt,
)
from eimemory.models.memory_edges import MemoryEdge
from eimemory.models.records import LinkRef, RecallBundle, RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_id, normalize_source_ids


DEFAULT_MAX_CONTEXT_CHARS = 7_200
DEFAULT_MAX_TURN_CHARS = 12_000
DEFAULT_MAX_MEMORY_CHARS = 16_000
_HERMES_MUTATION_SCHEMA = "adapter.hermes.mutation.v1"
_HERMES_MUTATION_ACTIONS = frozenset({"add", "replace", "remove"})
_HERMES_TARGETS = frozenset({"memory", "user"})
_HERMES_PROVENANCE_FIELDS = frozenset(
    {
        "write_origin",
        "execution_context",
        "session_id",
        "parent_session_id",
        "platform",
        "tool_name",
        "task_id",
        "task_call_id",
        "tool_call_id",
    }
)
_HERMES_LEGACY_TARGET_LOOKUP_LIMIT = 32


class AgentRuntimeMemoryService:
    def __init__(
        self,
        runtime: Runtime,
        *,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
        max_turn_chars: int = DEFAULT_MAX_TURN_CHARS,
        max_memory_chars: int = DEFAULT_MAX_MEMORY_CHARS,
    ) -> None:
        self.runtime = runtime
        self.max_context_chars = self._positive_limit(max_context_chars, DEFAULT_MAX_CONTEXT_CHARS)
        self.max_turn_chars = self._positive_limit(max_turn_chars, DEFAULT_MAX_TURN_CHARS)
        self.max_memory_chars = self._positive_limit(max_memory_chars, DEFAULT_MAX_MEMORY_CHARS)

    def prefetch(
        self,
        *,
        channel: str,
        scope: dict,
        query: str,
        task_type: str = "",
        limit: int = 8,
        task_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        channel_id = normalize_runtime_channel(channel)
        channel_scope = resolve_channel_scope(channel_id, scope)
        normalized_query = str(query or "").strip()
        context = dict(task_context or {})
        context["runtime_channel"] = channel_id
        context["authority_mode"] = AUTHORITY_MODE
        if task_type:
            context["task_type"] = str(task_type).strip()
        bundle = self.runtime.memory.recall(
            query=normalized_query,
            scope=channel_scope,
            task_context=context,
            limit=max(1, min(50, self._positive_limit(limit, 8))),
        )
        return {
            "ok": True,
            "adapter_contract_version": RUNTIME_ADAPTER_CONTRACT_VERSION,
            "channel": channel_id,
            "scope": channel_scope,
            "bundle": bundle.to_dict(),
            "context": self._render_context(bundle),
        }

    def proactive_prefetch(
        self,
        *,
        channel: str,
        scope: dict,
        source_ids: list[str],
        session_id: str,
        turn_id: str,
        query: str,
        task_type: str = "",
    ) -> dict[str, Any]:
        channel_id, channel_scope, sources, session, turn = self._proactive_namespace(
            channel=channel, scope=scope, source_ids=source_ids,
            session_id=session_id, turn_id=turn_id,
        )
        normalized_query = self._bounded_text(query, self.max_turn_chars)
        if not normalized_query:
            raise ValueError("query is required")
        return self.runtime.proactive.decide(
            channel=channel_id,
            scope=channel_scope,
            source_ids=sources,
            session_id=session,
            query_id=turn,
            query=normalized_query,
            task_type=str(task_type or "").strip(),
        )

    def proactive_ack(
        self,
        *,
        channel: str,
        scope: dict,
        source_ids: list[str],
        session_id: str,
        turn_id: str,
        decision_id: str,
        injected_citations: list[str],
    ) -> dict[str, Any]:
        channel_id, channel_scope, sources, session, turn = self._proactive_namespace(
            channel=channel, scope=scope, source_ids=source_ids,
            session_id=session_id, turn_id=turn_id,
        )
        decision = str(decision_id or "").strip()
        if not decision:
            raise ValueError("decision_id is required")
        return self.runtime.proactive.mark_injected(
            decision_id=decision, channel=channel_id, scope=channel_scope,
            source_ids=sources, session_id=session, turn_id=turn,
            release_identity=self._proactive_release(channel_id, channel_scope),
            injected_citations=injected_citations,
        )

    def proactive_terminal(
        self,
        *,
        channel: str,
        scope: dict,
        source_ids: list[str],
        session_id: str,
        turn_id: str,
        used_citations: list[str],
        rejected_citations: list[str] | None = None,
        decision_id: str = "",
        terminal_outcome: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        channel_id, channel_scope, sources, session, turn = self._proactive_namespace(
            channel=channel, scope=scope, source_ids=source_ids,
            session_id=session_id, turn_id=turn_id,
        )
        release = self._proactive_release(channel_id, channel_scope)
        decision = str(decision_id or "").strip()
        if not decision:
            found = self.runtime.store.find_proactive_decision(
                {
                    "channel": channel_id,
                    "scope": channel_scope,
                    "source_key": self._proactive_source_key(sources),
                    "session_id": session,
                    "turn_id": turn,
                    "release_identity": dict(release),
                }
            )
            decision = str((found or {}).get("decision_id") or "")
        if not decision:
            return {"ok": True, "decision_id": "", "changed": 0, "reason": "decision_not_found"}
        used = [str(item) for item in used_citations]
        rejected = [str(item) for item in (rejected_citations or [])]
        feedback = {"ok": True, "changed": 0}
        if used or rejected:
            feedback = self.runtime.proactive.record_feedback(
                decision_id=decision, used_citations=used, rejected_citations=rejected,
                channel=channel_id, scope=channel_scope, source_ids=sources,
                session_id=session, turn_id=turn, release_identity=release,
            )
        terminal = self.runtime.proactive.mark_terminal(
            decision_id=decision, channel=channel_id, scope=channel_scope,
            source_ids=sources, session_id=session, turn_id=turn, release_identity=release,
            terminal_outcome=terminal_outcome,
        )
        return {
            "ok": bool(feedback.get("ok")) and bool(terminal.get("ok")),
            "decision_id": decision,
            "feedback_changed": int(feedback.get("changed") or 0),
            "terminal_changed": int(terminal.get("changed") or 0),
        }

    def proactive_complete_turn(
        self,
        *,
        channel: str,
        scope: dict,
        source_ids: list[str],
        session_id: str,
        turn_id: str,
        user_summary: str,
        assistant_summary: str,
    ) -> dict[str, Any]:
        channel_id, channel_scope, sources, session, turn = self._proactive_namespace(
            channel=channel, scope=scope, source_ids=source_ids,
            session_id=session_id, turn_id=turn_id,
        )
        self.runtime.proactive.complete_turn(
            channel=channel_id, scope=channel_scope, source_ids=sources,
            session_id=session, turn_id=turn,
            user_summary=self._bounded_text(user_summary, self.max_turn_chars),
            assistant_summary=self._bounded_text(assistant_summary, self.max_turn_chars),
        )
        return {"ok": True, "session_id": session, "turn_id": turn}

    def remember(
        self,
        *,
        channel: str,
        scope: dict,
        text: str,
        memory_type: str = "durable_fact",
        event_id: str,
        title: str = "",
        force_capture: bool = False,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        channel_id = normalize_runtime_channel(channel)
        channel_scope = resolve_channel_scope(channel_id, scope)
        normalized_text = self._bounded_text(text, self.max_memory_chars)
        normalized_event_id = str(event_id or "").strip()
        if not normalized_text:
            raise ValueError("memory text is required")
        if not normalized_event_id:
            raise ValueError("event_id is required")
        idempotency_key = self._idempotency_key(
            operation="remember",
            channel=channel_id,
            scope=channel_scope,
            event_id=normalized_event_id,
        )
        existing = self.runtime.store.get_by_idempotency_key(
            kinds=["memory"],
            scope=ScopeRef.from_dict(channel_scope),
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return self._memory_result(existing, channel=channel_id, scope=channel_scope, idempotent=True)

        record = self.runtime.memory.ingest(
            text=normalized_text,
            memory_type=str(memory_type or "durable_fact").strip() or "durable_fact",
            title=str(title or f"{channel_id.title()} long-term memory"),
            scope=channel_scope,
            source=f"{channel_id}.memory",
            force_capture=bool(force_capture),
            meta={
                **dict(meta or {}),
                "runtime_channel": channel_id,
                "authority_mode": AUTHORITY_MODE,
                "authoritative": True,
                "adapter_contract_version": RUNTIME_ADAPTER_CONTRACT_VERSION,
                "idempotency_key": idempotency_key,
                "source_event_id": normalized_event_id,
            },
            record_id=self._deterministic_record_id(
                kind="memory",
                channel=channel_id,
                scope=channel_scope,
                operation="remember",
                idempotency_key=idempotency_key,
            ),
        )
        if record.status != "active":
            record.meta["authoritative"] = False
            business_meta = record.meta.get("business_meta")
            if isinstance(business_meta, dict):
                business_meta["authoritative"] = False
        return self._memory_result(record, channel=channel_id, scope=channel_scope, idempotent=False)

    def mutate_memory(
        self,
        *,
        channel: str,
        scope: dict,
        action: str,
        target: str,
        source_id: str,
        content: str,
        idempotency_key: str,
        provenance: dict[str, Any],
        target_record_id: str = "",
        old_text: str = "",
        expected_revision: str = "",
    ) -> dict[str, Any]:
        """Apply one Hermes memory lifecycle transition under the authority lock."""

        channel_id = normalize_runtime_channel(channel)
        if channel_id != "hermes":
            raise ValueError("memory mutation is restricted to the hermes channel")
        channel_scope = resolve_channel_scope(channel_id, scope)
        action_id = str(action or "").strip().lower()
        target_id = str(target or "").strip().lower()
        source_partition = normalize_source_id(source_id)
        request_key = str(idempotency_key or "").strip()
        if action_id not in _HERMES_MUTATION_ACTIONS:
            raise ValueError("unsupported memory mutation action")
        if target_id not in _HERMES_TARGETS:
            raise ValueError("unsupported hermes memory target")
        if not request_key or len(request_key) > 256:
            raise ValueError("idempotency_key is required and bounded")
        normalized_content = self._required_mutation_content(content) if action_id != "remove" else ""
        normalized_old_text = self._optional_mutation_text(old_text)
        requested_target_id = str(target_record_id or "").strip()
        requested_revision = str(expected_revision or "").strip().lower()
        if requested_revision and not self._is_sha256_digest(requested_revision):
            raise ValueError("expected_revision must be a SHA-256 digest")
        if action_id in {"replace", "remove"}:
            if not normalized_old_text and not (requested_target_id and requested_revision):
                raise ValueError("replace/remove require old_text or target_record_id with expected_revision")
            derived_old_revision = self._content_revision(normalized_old_text) if normalized_old_text else ""
            if normalized_old_text and requested_revision and requested_revision != derived_old_revision:
                raise ValueError("expected_revision does not match old_text")
            requested_revision = requested_revision or derived_old_revision
        elif requested_target_id or normalized_old_text or requested_revision:
            raise ValueError("add does not accept a target record or expected revision")
        safe_provenance = self._hermes_provenance(provenance)
        content_revision = self._content_revision(normalized_content) if normalized_content else ""
        request_digest = self._mutation_request_digest(
            channel=channel_id,
            scope=channel_scope,
            action=action_id,
            target=target_id,
            source_id=source_partition,
            content_revision=content_revision,
            expected_revision=requested_revision,
            target_record_id=requested_target_id,
            provenance=safe_provenance,
        )
        scope_ref = ScopeRef.from_dict(channel_scope)

        def mutation(sqlite) -> dict[str, Any]:
            existing = sqlite.get_by_idempotency_key(
                kinds=["memory"], scope=scope_ref, idempotency_key=request_key
            )
            if existing is not None:
                prior_digest = str(existing.meta.get("mutation_request_digest") or "")
                if prior_digest != request_digest:
                    return ({"ok": False, "error": "mutation_idempotency_conflict"}, [], [])
                return (
                    self._mutation_result(
                        existing,
                        channel=channel_id,
                        scope=channel_scope,
                        action=str(existing.meta.get("mutation_action") or action_id),
                        content_revision=str(existing.meta.get("content_revision") or ""),
                        idempotent=True,
                    ),
                    [],
                    [],
                )

            if action_id == "add":
                record_id = self._mutation_record_id(
                    channel=channel_id,
                    scope=channel_scope,
                    source_id=source_partition,
                    action="add",
                    target=target_id,
                    content_revision=content_revision,
                )
                deterministic = sqlite.get_by_id(record_id, scope=scope_ref)
                if deterministic is not None:
                    if not self._is_matching_hermes_record(
                        deterministic, source_id=source_partition, target=target_id, active_only=True
                    ) or str(deterministic.meta.get("idempotency_key") or "") != request_key or str(
                        deterministic.meta.get("mutation_request_digest") or ""
                    ) != request_digest:
                        return ({"ok": False, "error": "mutation_target_conflict"}, [], [])
                    return (
                        self._mutation_result(
                            deterministic,
                            channel=channel_id,
                            scope=channel_scope,
                            action="add",
                            content_revision=str(deterministic.meta.get("content_revision") or content_revision),
                            idempotent=True,
                        ),
                        [],
                        [],
                    )
                record = self._new_hermes_record(
                    record_id=record_id,
                    source_id=source_partition,
                    scope=scope_ref,
                    target=target_id,
                    content=normalized_content,
                    content_revision=content_revision,
                    action="add",
                    idempotency_key=request_key,
                    request_digest=request_digest,
                    provenance=safe_provenance,
                )
                sqlite.upsert(record, commit=False)
                return (
                    self._mutation_result(
                        record, channel=channel_id, scope=channel_scope, action="add", content_revision=content_revision, idempotent=False
                    ),
                    [record],
                    [],
                )

            resolved = self._resolve_hermes_mutation_target(
                sqlite=sqlite,
                scope=scope_ref,
                source_id=source_partition,
                target=target_id,
                target_record_id=requested_target_id,
                old_text=normalized_old_text,
            )
            if isinstance(resolved, str):
                return ({"ok": False, "error": resolved}, [], [])
            old_record = resolved
            actual_revision = str(old_record.meta.get("content_revision") or self._content_revision(self._record_content(old_record)))
            if actual_revision != requested_revision:
                return ({"ok": False, "error": "mutation_stale_revision"}, [], [])
            if action_id == "replace":
                successor_id = self._mutation_record_id(
                    channel=channel_id,
                    scope=channel_scope,
                    source_id=source_partition,
                    action="replace",
                    target=target_id,
                    content_revision=content_revision,
                    predecessor_id=old_record.record_id,
                )
                successor = self._new_hermes_record(
                    record_id=successor_id,
                    source_id=source_partition,
                    scope=scope_ref,
                    target=target_id,
                    content=normalized_content,
                    content_revision=content_revision,
                    action="replace",
                    idempotency_key=request_key,
                    request_digest=request_digest,
                    provenance=safe_provenance,
                    links=[LinkRef(relation="supersedes", target_kind="record", target_id=old_record.record_id)],
                    predecessor_id=old_record.record_id,
                )
                old_record.status = "superseded"
                old_record.links = self._linked(old_record.links, "superseded_by", successor.record_id)
                old_record.meta = {
                    **old_record.meta,
                    "mutation_state": "superseded",
                    "superseded_by": successor.record_id,
                }
                old_record.touch()
                sqlite.upsert(successor, commit=False)
                sqlite.upsert(old_record, commit=False)
                edges = self._mutation_edges(
                    scope=scope_ref,
                    mutation_record_id=successor.record_id,
                    target_record_id=old_record.record_id,
                    forward_relation="supersedes",
                    reverse_relation="superseded_by",
                    source_id=source_partition,
                    target=target_id,
                )
                return (
                    self._mutation_result(
                        successor, channel=channel_id, scope=channel_scope, action="replace", content_revision=content_revision, idempotent=False
                    ),
                    [successor, old_record],
                    edges,
                )

            tombstone_id = self._mutation_record_id(
                channel=channel_id,
                scope=channel_scope,
                source_id=source_partition,
                action="remove",
                target=target_id,
                content_revision=requested_revision,
                predecessor_id=old_record.record_id,
            )
            tombstone = self._new_hermes_record(
                record_id=tombstone_id,
                source_id=source_partition,
                scope=scope_ref,
                target=target_id,
                content="",
                content_revision="",
                action="remove",
                idempotency_key=request_key,
                request_digest=request_digest,
                provenance=safe_provenance,
                links=[LinkRef(relation="removes", target_kind="record", target_id=old_record.record_id)],
                predecessor_id=old_record.record_id,
                status="removed",
            )
            old_record.status = "removed"
            old_record.links = self._linked(old_record.links, "removed_by", tombstone.record_id)
            old_record.meta = {
                **old_record.meta,
                "mutation_state": "removed",
                "removed_by": tombstone.record_id,
            }
            old_record.touch()
            sqlite.upsert(tombstone, commit=False)
            sqlite.upsert(old_record, commit=False)
            edges = self._mutation_edges(
                scope=scope_ref,
                mutation_record_id=tombstone.record_id,
                target_record_id=old_record.record_id,
                forward_relation="removes",
                reverse_relation="removed_by",
                source_id=source_partition,
                target=target_id,
            )
            return (
                self._mutation_result(
                    tombstone, channel=channel_id, scope=channel_scope, action="remove", content_revision="", idempotent=False
                ),
                [tombstone, old_record],
                edges,
            )

        return self.runtime.store.mutate_records_atomically(mutation)

    def sync_turn(
        self,
        *,
        channel: str,
        scope: dict,
        session_id: str,
        turn_id: str,
        user_text: str,
        assistant_text: str,
    ) -> dict[str, Any]:
        normalized_session_id = str(session_id or "").strip()
        normalized_turn_id = str(turn_id or "").strip()
        if not normalized_session_id:
            raise ValueError("session_id is required")
        if not normalized_turn_id:
            raise ValueError("turn_id is required")
        normalized_user_text = str(user_text or "").strip()
        normalized_assistant_text = str(assistant_text or "").strip()
        if not normalized_user_text and not normalized_assistant_text:
            raise ValueError("turn text is required")
        turn_text = self._bounded_text(
            f"User: {normalized_user_text}\nAssistant: {normalized_assistant_text}",
            self.max_turn_chars,
        )
        return self.remember(
            channel=channel,
            scope=scope,
            text=turn_text,
            memory_type="conversation",
            event_id=f"{normalized_session_id}:{normalized_turn_id}",
            title=f"{normalize_runtime_channel(channel).title()} completed turn",
            meta={"session_id": normalized_session_id, "turn_id": normalized_turn_id, "capture_origin": "turn_sync"},
        )

    def _resolve_hermes_mutation_target(
        self,
        *,
        sqlite: Any,
        scope: ScopeRef,
        source_id: str,
        target: str,
        target_record_id: str,
        old_text: str,
    ) -> RecordEnvelope | str:
        if target_record_id:
            record = sqlite.get_by_id(target_record_id, scope=scope)
            if record is None:
                return "mutation_target_not_found"
            if not self._is_matching_hermes_record(record, source_id=source_id, target=target, active_only=False):
                return "mutation_target_scope_mismatch"
            if record.status != "active":
                return "mutation_target_inactive"
            if old_text and self._content_revision(self._record_content(record)) != self._content_revision(old_text):
                return "mutation_stale_revision"
            return record
        candidates = sqlite.list_records(
            kinds=["memory"],
            scope=scope,
            status="active",
            source_ids=[source_id],
            limit=_HERMES_LEGACY_TARGET_LOOKUP_LIMIT,
        )
        old_revision = self._content_revision(old_text)
        exact = [
            record
            for record in candidates
            if self._is_matching_hermes_record(record, source_id=source_id, target=target, active_only=True)
            and self._content_revision(self._record_content(record)) == old_revision
        ]
        if len(exact) != 1:
            return "mutation_target_ambiguous" if len(exact) > 1 else "mutation_target_not_found"
        return exact[0]

    @staticmethod
    def _is_matching_hermes_record(
        record: RecordEnvelope,
        *,
        source_id: str,
        target: str,
        active_only: bool,
    ) -> bool:
        return (
            (not active_only or record.status == "active")
            and record.source_id == source_id
            and str(record.meta.get("runtime_channel") or "") == "hermes"
            and str(record.meta.get("hermes_target") or "") == target
        )

    @classmethod
    def _new_hermes_record(
        cls,
        *,
        record_id: str,
        source_id: str,
        scope: ScopeRef,
        target: str,
        content: str,
        content_revision: str,
        action: str,
        idempotency_key: str,
        request_digest: str,
        provenance: dict[str, str],
        links: list[LinkRef] | None = None,
        predecessor_id: str = "",
        status: str = "active",
    ) -> RecordEnvelope:
        is_tombstone = action == "remove"
        record = RecordEnvelope.create(
            kind="memory",
            title=("Hermes memory removal audit tombstone" if is_tombstone else "Hermes durable long-term memory"),
            summary=("Hermes memory removal audit tombstone" if is_tombstone else content),
            content=({} if is_tombstone else {"text": content, "memory_type": "preference" if target == "user" else "durable_fact"}),
            scope=scope,
            source="hermes.memory_write",
            source_id=source_id,
            status=status,
            links=list(links or []),
            provenance=provenance,
            meta={
                "memory_type": "audit_record" if is_tombstone else ("preference" if target == "user" else "durable_fact"),
                "runtime_channel": "hermes",
                "authority_mode": AUTHORITY_MODE,
                "authoritative": True,
                "adapter_contract_version": RUNTIME_ADAPTER_CONTRACT_VERSION,
                "hermes_target": target,
                "mutation_schema": _HERMES_MUTATION_SCHEMA,
                "mutation_action": action,
                "mutation_request_digest": request_digest,
                "idempotency_key": idempotency_key,
                "content_revision": content_revision,
                "predecessor_record_id": predecessor_id,
                "non_recallable": is_tombstone,
            },
        )
        record.record_id = record_id
        return record

    @staticmethod
    def _linked(links: list[LinkRef], relation: str, target_id: str) -> list[LinkRef]:
        existing = list(links)
        if not any(link.relation == relation and link.target_kind == "record" and link.target_id == target_id for link in existing):
            existing.append(LinkRef(relation=relation, target_kind="record", target_id=target_id))
        return existing

    @staticmethod
    def _mutation_edges(
        *,
        scope: ScopeRef,
        mutation_record_id: str,
        target_record_id: str,
        forward_relation: str,
        reverse_relation: str,
        source_id: str,
        target: str,
    ) -> list[MemoryEdge]:
        common_meta = {
            "schema_version": _HERMES_MUTATION_SCHEMA,
            "source_id": source_id,
            "hermes_target": target,
        }
        return [
            MemoryEdge.create(
                from_id=mutation_record_id,
                to_id=target_record_id,
                edge_type="temporal",
                confidence=1.0,
                evidence_id=mutation_record_id,
                scope=scope,
                reason=forward_relation,
                meta={**common_meta, "relation": forward_relation},
            ),
            MemoryEdge.create(
                from_id=target_record_id,
                to_id=mutation_record_id,
                edge_type="temporal",
                confidence=1.0,
                evidence_id=mutation_record_id,
                scope=scope,
                reason=reverse_relation,
                meta={**common_meta, "relation": reverse_relation},
            ),
        ]

    @staticmethod
    def _record_content(record: RecordEnvelope) -> str:
        return str(record.content.get("text") or record.summary or record.detail or "")

    def _required_mutation_content(self, value: object) -> str:
        text = self._optional_mutation_text(value)
        if not text:
            raise ValueError("memory content is required")
        return text

    def _optional_mutation_text(self, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("memory content must be text")
        text = value.strip()
        if len(text) > self.max_memory_chars:
            raise ValueError("memory content exceeds configured limit")
        return text

    @staticmethod
    def _content_revision(text: str) -> str:
        normalized = " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())
        return sha256(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _is_sha256_digest(value: str) -> bool:
        return len(value) == 64 and all(char in "0123456789abcdef" for char in value)

    @staticmethod
    def _hermes_provenance(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError("provenance must be an object")
        unexpected = set(value) - _HERMES_PROVENANCE_FIELDS
        if unexpected:
            raise ValueError("provenance contains unsupported fields")
        normalized: dict[str, str] = {}
        for field, raw_value in value.items():
            if not isinstance(raw_value, str):
                raise ValueError("provenance values must be text")
            text = raw_value.strip()
            if text:
                normalized[field] = text[:512]
        return normalized

    @staticmethod
    def _mutation_request_digest(
        *,
        channel: str,
        scope: dict[str, str],
        action: str,
        target: str,
        source_id: str,
        content_revision: str,
        expected_revision: str,
        target_record_id: str,
        provenance: dict[str, str],
    ) -> str:
        payload = json.dumps(
            {
                "schema": _HERMES_MUTATION_SCHEMA,
                "channel": channel,
                "scope": scope,
                "action": action,
                "target": target,
                "source_id": source_id,
                "content_revision": content_revision,
                "expected_revision": expected_revision,
                "target_record_id": target_record_id,
                "provenance": provenance,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _mutation_record_id(
        *,
        channel: str,
        scope: dict[str, str],
        source_id: str,
        action: str,
        target: str,
        content_revision: str,
        predecessor_id: str = "",
    ) -> str:
        payload = json.dumps(
            {
                "schema": _HERMES_MUTATION_SCHEMA,
                "channel": channel,
                "scope": scope,
                "source_id": source_id,
                "action": action,
                "target": target,
                "content_revision": content_revision,
                "predecessor_id": predecessor_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return "mem_hermes_" + sha256(payload.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _mutation_result(
        record: RecordEnvelope,
        *,
        channel: str,
        scope: dict[str, str],
        action: str,
        content_revision: str,
        idempotent: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "adapter_contract_version": RUNTIME_ADAPTER_CONTRACT_VERSION,
            "channel": channel,
            "scope": scope,
            "action": action,
            "record": record.to_dict(),
            "content_revision": content_revision,
            "idempotent": idempotent,
        }

    def record_terminal(
        self,
        *,
        channel: str,
        scope: dict,
        end_kind: str,
        session_id: str,
        event_id: str,
        task_type: str,
        success: bool | None,
        verification: Any = "",
        result: Any = "",
        tool_receipts: list[dict[str, Any]] | None = None,
        receipt_ids: list[str] | None = None,
        rehearsal: bool = False,
    ) -> dict[str, Any]:
        channel_id = normalize_runtime_channel(channel)
        normalized_end_kind = str(end_kind or "").strip().lower()
        allowed_end_kinds = {
            "openclaw": {"agent_end", "task_end", "session_end"},
            "codex": {"stop", "session_end"},
            "hermes": {"task_end", "session_end"},
        }
        if normalized_end_kind not in allowed_end_kinds[channel_id]:
            raise ValueError(f"unsupported terminal event for {channel_id}: {end_kind}")
        normalized_session_id = str(session_id or "").strip()
        normalized_event_id = str(event_id or "").strip()
        normalized_task_type = str(task_type or "").strip()
        if not normalized_session_id or not normalized_event_id:
            raise ValueError("session_id and event_id are required")
        if not normalized_task_type:
            raise ValueError("task_type is required")
        if success is not None and not isinstance(success, bool):
            raise ValueError("success must be a boolean or null")

        channel_scope = resolve_channel_scope(channel_id, scope)
        method = f"{channel_id}.{normalized_end_kind}"
        trace_id = self._terminal_trace_id(
            channel=channel_id,
            scope=channel_scope,
            session_id=normalized_session_id,
            event_id=normalized_event_id,
        )
        release = current_release_identity(self.runtime, ScopeRef.from_dict(channel_scope))
        if release is None and channel_id != "openclaw":
            release = current_release_identity(
                self.runtime,
                ScopeRef.from_dict(base_scope_from_channel(channel_id, channel_scope)),
            )
        verification_text = bounded_redacted_text(verification, max_chars=512)
        result_text = bounded_redacted_text(result, max_chars=2_000)
        verified_receipts: list[dict[str, Any]] = []
        raw_ids: list[str] = []
        if channel_id in {"codex", "hermes"}:
            submitted_receipt_ids = list(receipt_ids or [])
            if len(submitted_receipt_ids) > MAX_ELIGIBLE_RECEIPTS_PER_RUN:
                raise ValueError("terminal receipt set exceeds the protected per-run bound")
            raw_ids = list(
                dict.fromkeys(
                    str(value).strip()
                    for value in submitted_receipt_ids
                    if str(value).strip()
                )
            )
            receipt_states = self.runtime.store.sqlite.load_adapter_tool_receipt_states(
                raw_ids,
                channel=channel_id,
                session_id=normalized_session_id,
                run_id=normalized_event_id,
                scope=channel_scope,
            )
            if set(receipt_states) != set(raw_ids):
                raise ValueError(
                    "terminal receipt set does not match the protected pending set "
                    "(unknown or cross-scope receipt)"
                )
            loaded_receipts = self.runtime.store.sqlite.load_claimable_adapter_tool_receipts(
                channel=channel_id,
                session_id=normalized_session_id,
                run_id=normalized_event_id,
                trace_id=trace_id,
                scope=channel_scope,
            )
            expected_source = ATTESTATION_PRODUCERS[channel_id][1]
            stale_ids: set[str] = set()
            for receipt in loaded_receipts:
                receipt_id = str(receipt.get("receipt_id") or "")
                trusted = (
                    receipt.get("receipt_version") == V2_RECEIPT_VERSION
                    and receipt.get("channel") == channel_id
                    and receipt.get("source") == expected_source
                    and receipt.get("passed") is True
                    and receipt.get("verification_policy_id") in TRUSTED_TEST_POLICY_IDS
                    and verify_tool_receipt(
                        receipt,
                        session_id=normalized_session_id,
                        run_id=normalized_event_id,
                    )
                    and (release is None or release_identity_from_record(receipt) == release)
                )
                if trusted:
                    verified_receipts.append(
                        {
                            **canonical_tool_receipt(receipt),
                            "signature": str(receipt.get("signature") or "").lower(),
                        }
                    )
                else:
                    stale_ids.add(receipt_id)
            if stale_ids:
                self.runtime.store.sqlite.quarantine_adapter_tool_receipts(
                    sorted(stale_ids),
                    channel=channel_id,
                    session_id=normalized_session_id,
                    run_id=normalized_event_id,
                    scope=channel_scope,
                )
            verified_ids = {str(receipt["receipt_id"]) for receipt in verified_receipts}
            ignored_ids = stale_ids | {
                receipt_id
                for receipt_id, state in receipt_states.items()
                if state["eligible"] is False and not state["consumed_trace_id"]
            }
            raw_id_set = set(raw_ids)
            if verified_ids - raw_id_set or raw_id_set - verified_ids - ignored_ids:
                raise ValueError("terminal receipt set does not match the protected pending set")
            # Caller-provided prose and inline receipts are diagnostic only.
            verification_text = (
                f"{verified_receipts[0]['source']}:{verified_receipts[0]['receipt_id']}"
                if verified_receipts
                else ""
            )
        else:
            verified_receipts = [
                {**canonical_tool_receipt(receipt), "signature": str(receipt.get("signature") or "").lower()}
                for receipt in list(tool_receipts or [])[:32]
                if verify_tool_receipt(
                    receipt,
                    session_id=normalized_session_id,
                    run_id=normalized_event_id,
                )
            ]
        lifecycle_only = normalized_end_kind == "session_end"
        if channel_id in {"codex", "hermes"} and not lifecycle_only:
            if verified_receipts:
                success = True
                normalized_task_type = "code.test" if channel_id == "codex" else "research.test"
            else:
                success = None
                normalized_task_type = (
                    "code.unverified" if channel_id == "codex" else "research.unverified"
                )
        event_payload: dict[str, Any] = {
            "id": f"evt_{channel_id}_{trace_id[-24:]}",
            "idempotency_key": f"{method}:{normalized_event_id}",
            "source": method,
            "hook": normalized_end_kind,
            "session_id": normalized_session_id,
            "run_id": normalized_event_id,
            "outcome_trace_id": trace_id,
            "outcome_trace_task_type": normalized_task_type,
            "event_type": normalized_task_type,
            "goal": normalized_task_type,
            "verification": verification_text,
            "verification_receipts": verified_receipts,
            "result": result_text,
            "evidence_class": (
                "lifecycle_event"
                if lifecycle_only
                else ("verified_real_task" if verified_receipts else "diagnostic_task")
            ),
            "runtime_channel": channel_id,
            "authority_mode": AUTHORITY_MODE,
        }
        if release is not None:
            event_payload.update(release_identity_payload(release))
        if lifecycle_only:
            recorded_event = self.runtime.record_event(event_payload, scope=channel_scope)
            return {
                "ok": True,
                "event": recorded_event,
                "outcome": None,
                "outcome_trace": None,
            }

        explicit_verification = bool(verification_text)
        if success is True and explicit_verification:
            outcome_name = "good"
        elif success is True:
            outcome_name = "verification_missing"
        elif success is False:
            outcome_name = "bad"
        else:
            outcome_name = "uncertain"
        outcome_payload = {
            "outcome": outcome_name,
            "reason": verification_text or result_text or "terminal outcome was not explicitly verified",
            "source": method,
            "source_trust": "system_verified" if explicit_verification else "system_diagnostic",
            "verification": verification_text,
            "result": result_text,
        }
        outcome_trace_payload = {
            "source": method,
            "session_id": normalized_session_id,
            "trace_id": trace_id,
            "idempotency_key": f"{method}:{normalized_session_id}:{normalized_event_id}",
            "task_type": normalized_task_type,
            "input_summary": result_text or normalized_task_type,
            "selected_tools": [],
            "actions": [],
            "outcome": {
                "status": outcome_name,
                "success": success,
                "rehearsal": bool(rehearsal),
            },
            "verifier": {
                "passed": bool(success is True and explicit_verification),
                "method": method,
                "evidence_refs": [event_payload["id"]],
                "checks": {
                    "verification": verification_text,
                    "result": result_text,
                    "receipt_ids": [receipt["receipt_id"] for receipt in verified_receipts],
                },
            },
            "evidence_class": "verified_real_task" if verified_receipts else "diagnostic_task",
        }
        if release is not None:
            outcome_trace_payload.update(release_identity_payload(release))
        terminal_contract_digest = self._terminal_contract_digest(
            {
                "channel": channel_id,
                "scope": channel_scope,
                "end_kind": normalized_end_kind,
                "session_id": normalized_session_id,
                "event_id": normalized_event_id,
                "task_type": normalized_task_type,
                "success": success,
                "rehearsal": bool(rehearsal),
                "verification": verification_text,
                "result": result_text,
                "receipt_ids": [receipt["receipt_id"] for receipt in verified_receipts],
            }
        )
        event_payload["terminal_contract_digest"] = terminal_contract_digest
        outcome_payload["terminal_contract_digest"] = terminal_contract_digest
        outcome_trace_payload["terminal_contract_digest"] = terminal_contract_digest
        outcome_trace_payload["recorded_at"] = datetime.now(timezone.utc).isoformat()
        from eimemory.experience.outcome import build_outcome_trace_record

        trace_build = build_outcome_trace_record(
            outcome_trace_payload,
            scope=ScopeRef.from_dict(channel_scope),
        )
        terminal = self.runtime.store.record_terminal_bundle(
            verified_receipts=verified_receipts
            if channel_id in {"codex", "hermes"}
            else [],
            channel=channel_id,
            session_id=normalized_session_id,
            run_id=normalized_event_id,
            trace_id=trace_id,
            event_payload=event_payload,
            outcome_payload=outcome_payload,
            trace_record=trace_build.record,
            scope=channel_scope,
        )
        recorded_event = terminal["event"]
        recorded_outcome = terminal["outcome"]
        outcome_trace = terminal["outcome_trace"]
        return {
            "ok": bool(outcome_trace.get("ok")),
            "event": recorded_event,
            "outcome": recorded_outcome,
            "outcome_trace": outcome_trace,
        }

    def attest_tool_result(
        self,
        *,
        producer: str,
        channel: str,
        scope: dict,
        session_id: str,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        result: Any,
        tool_input: Any = None,
        duration_ms: int = 0,
    ) -> dict[str, Any]:
        producer_id = str(producer or "").strip().lower()
        channel_id = normalize_runtime_channel(channel)
        expected = ATTESTATION_PRODUCERS.get(producer_id)
        if expected is None or expected[0] != channel_id:
            raise ValueError("attestation producer is not authorized for channel")
        normalized_session = str(session_id or "").strip()
        normalized_run = str(run_id or "").strip()
        normalized_call = str(tool_call_id or "").strip()
        normalized_tool = self._bounded_text(tool_name, 200)
        if not all((normalized_session, normalized_run, normalized_call, normalized_tool)):
            raise ValueError("session_id, run_id, tool_call_id, and tool_name are required")
        channel_scope = resolve_channel_scope(channel_id, scope)
        safe_input = self._bounded_attestation_result(tool_input)
        safe_result = self._bounded_attestation_result(result)
        invocation_digest = sha256(safe_input.encode("utf-8", errors="replace")).hexdigest()
        result_digest = sha256(safe_result.encode("utf-8", errors="replace")).hexdigest()
        # Preserve the host result shape as part of the trust decision. Codex
        # raw output strings do not prove process exit status; Hermes strings
        # are accepted only as the host's documented JSON status envelope.
        policy_id, passed = self._verification_policy(
            normalized_tool,
            safe_input,
            safe_result,
            structured_envelope=(
                isinstance(result, Mapping)
                or (channel_id == "hermes" and isinstance(result, str))
            ),
            require_complete_envelope=(channel_id == "hermes" and isinstance(result, str)),
        )
        release = current_release_identity(self.runtime, ScopeRef.from_dict(base_scope_from_channel(channel_id, channel_scope)))
        issued_at = datetime.now(timezone.utc)
        stable = json.dumps(
            {"channel": channel_id, "scope": channel_scope, "session_id": normalized_session, "run_id": normalized_run, "tool_call_id": normalized_call},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        receipt_id = f"rcpt_{channel_id}_{sha256(stable.encode('utf-8')).hexdigest()[:32]}"
        receipt = {
            "receipt_version": V2_RECEIPT_VERSION,
            "attestation_id": receipt_id,
            "receipt_id": receipt_id,
            "channel": channel_id,
            "source": expected[1],
            "tool_name": normalized_tool,
            "tool_call_id": normalized_call,
            "duration_ms": max(0, min(300_000, int(duration_ms or 0))),
            "passed": passed,
            "invocation_digest": invocation_digest,
            "result_digest": result_digest,
            "verification_policy_id": policy_id,
            "retrieval_policy_digest": sha256(b"adapter.attestation.policy.v1").hexdigest(),
            "session_id": normalized_session,
            "run_id": normalized_run,
            "issued_at": issued_at.isoformat(),
            "expires_at": (issued_at + timedelta(minutes=15)).isoformat(),
            **(release_identity_payload(release) if release is not None else {}),
        }
        signed = sign_tool_receipt(receipt)
        stored, idempotent = self.runtime.store.sqlite.register_adapter_tool_receipt(signed, scope=channel_scope)
        return {"ok": True, "receipt_id": stored["receipt_id"], "receipt": canonical_tool_receipt(stored), "idempotent": idempotent}

    def status(self, *, channel: str, scope: dict) -> dict[str, Any]:
        channel_id = normalize_runtime_channel(channel)
        channel_scope = resolve_channel_scope(channel_id, scope)
        release = current_release_identity(self.runtime, ScopeRef.from_dict(channel_scope))
        if release is None and channel_id != "openclaw":
            release = current_release_identity(
                self.runtime,
                ScopeRef.from_dict(base_scope_from_channel(channel_id, channel_scope)),
            )
        return {
            "ok": True,
            "adapter_contract_version": RUNTIME_ADAPTER_CONTRACT_VERSION,
            "channel": channel_id,
            "authority_mode": AUTHORITY_MODE,
            "scope": channel_scope,
            "release": release_identity_payload(release) if release is not None else {},
            "attestation_available": channel_id in set(
                getattr(self.runtime, "_attestation_available_channels", ())
            ),
            "attestation_reason": (
                "operator_separated_profile_active"
                if channel_id in set(getattr(self.runtime, "_attestation_available_channels", ()))
                else (
                    str(
                        getattr(
                            self.runtime,
                            "_attestation_unavailable_reason",
                            "operator_separated_attestation_profile_not_configured",
                        )
                    )
                    or "operator_separated_attestation_profile_not_configured_for_channel"
                )
            ),
        }

    def _render_context(self, bundle: RecallBundle) -> str:
        entries: list[str] = []
        for record in [*bundle.items, *bundle.rules, *bundle.reflections]:
            text = self._record_text(record)
            if not text:
                continue
            entries.append(f"- [{record.kind}] {record.title}: {text}")
        if not entries:
            return ""
        return self._bounded_text("Relevant eimemory context:\n" + "\n".join(entries), self.max_context_chars)

    @staticmethod
    def _proactive_source_key(source_ids: list[str]) -> str:
        payload = json.dumps(list(source_ids), ensure_ascii=False, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()

    def _proactive_namespace(
        self,
        *,
        channel: str,
        scope: dict,
        source_ids: list[str],
        session_id: str,
        turn_id: str,
    ) -> tuple[str, dict[str, str], list[str], str, str]:
        channel_id = normalize_runtime_channel(channel)
        channel_scope = resolve_channel_scope(channel_id, scope)
        sources = normalize_source_ids(source_ids)
        if not sources or sources == ("*",):
            raise ValueError("an exact non-wildcard source_ids boundary is required")
        session = str(session_id or "").strip()
        turn = str(turn_id or "").strip()
        if not session or not turn:
            raise ValueError("session_id and turn_id are required")
        return channel_id, channel_scope, list(sources), session, turn

    def _proactive_release(self, channel: str, scope: dict[str, str]) -> dict[str, str]:
        release_provider = getattr(self.runtime.proactive, "current_release", None)
        if callable(release_provider):
            return dict(release_provider(channel=channel, scope=scope))
        identity = current_release_identity(self.runtime, ScopeRef.from_dict(scope))
        if identity is None and channel in {"codex", "hermes"}:
            identity = current_release_identity(
                self.runtime,
                ScopeRef.from_dict(base_scope_from_channel(channel, scope)),
            )
        return release_identity_payload(identity) if identity is not None else {
            "release_commit": "",
            "release_version": "",
            "deployment_receipt_id": "",
            "release_session_id": "",
        }

    @staticmethod
    def _record_text(record: RecordEnvelope) -> str:
        return str(record.content.get("text") or record.summary or record.detail or "").strip()

    @staticmethod
    def _memory_result(
        record: RecordEnvelope,
        *,
        channel: str,
        scope: dict[str, str],
        idempotent: bool,
    ) -> dict[str, Any]:
        active = record.status == "active"
        return {
            "ok": active,
            "adapter_contract_version": RUNTIME_ADAPTER_CONTRACT_VERSION,
            "channel": channel,
            "scope": scope,
            "authoritative": active,
            "idempotent": idempotent,
            "record": record.to_dict(),
        }

    @staticmethod
    def _idempotency_key(
        *,
        operation: str,
        channel: str,
        scope: dict[str, str],
        event_id: str,
    ) -> str:
        payload = json.dumps(
            {"operation": operation, "channel": channel, "scope": scope, "event_id": event_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"adapter.{channel}.{operation}:" + sha256(payload.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _terminal_trace_id(
        *,
        channel: str,
        scope: dict[str, str],
        session_id: str,
        event_id: str,
    ) -> str:
        payload = json.dumps(
            {"channel": channel, "scope": scope, "session_id": session_id, "event_id": event_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"trace_{channel}_" + sha256(payload.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _deterministic_record_id(
        *,
        kind: str,
        channel: str,
        scope: dict[str, str],
        operation: str,
        idempotency_key: str,
    ) -> str:
        payload = json.dumps(
            {
                "channel": channel,
                "scope": scope,
                "operation": operation,
                "idempotency_key": idempotency_key,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prefix = "mem" if kind == "memory" else "rec"
        return f"{prefix}_" + sha256(payload.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _terminal_contract_digest(payload: dict[str, Any]) -> str:
        stable = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(stable.encode("utf-8")).hexdigest()

    @staticmethod
    def _bounded_text(value: object, limit: int) -> str:
        text = str(value or "").strip()
        return text if len(text) <= limit else text[:limit]

    @classmethod
    def _bounded_attestation_result(cls, value: Any) -> str:
        return bounded_redacted_text(value, max_chars=16_000)

    @staticmethod
    def _verification_policy(
        tool_name: str,
        safe_input: str,
        safe_result: str,
        *,
        structured_envelope: bool = True,
        require_complete_envelope: bool = False,
    ) -> tuple[str, bool]:
        try:
            parsed = json.loads(safe_result)
        except json.JSONDecodeError:
            parsed = None
        name = str(tool_name or "").strip().lower()
        direct_test_tool = name in {"pytest", "unittest", "cargo_test", "npm_test"}
        wrapped_test_tool = False
        if name in {"shell_command", "exec_command", "bash", "powershell", "terminal"}:
            try:
                invocation = json.loads(safe_input)
            except json.JSONDecodeError:
                invocation = None
            command = str(invocation.get("command") or "") if isinstance(invocation, dict) else ""
            control_view = command.lstrip()
            if control_view.startswith("&"):
                control_view = control_view[1:]
            has_shell_control = re.search(r"[\r\n;|`&]|\$\(", control_view) is not None
            wrapped_test_tool = not has_shell_control and re.match(
                r"^\s*(?:"
                r"python(?:\.exe)?\s+-m\s+pytest|pytest|cargo\s+test|npm\s+test|"
                r"rtk(?:\.exe)?\s+pytest|&\s+['\"][^'\"]*rtk(?:\.exe)?['\"]\s+pytest"
                r")(?:\s|$)",
                command,
                re.I,
            ) is not None
        recognized_test = direct_test_tool or wrapped_test_tool
        if recognized_test and structured_envelope and isinstance(parsed, dict):
            output_value = parsed.get("output") if require_complete_envelope else (
                parsed.get("summary") or parsed.get("output")
            )
            error_value = parsed.get("error", "")
            exit_code = parsed.get("exit_code")
            empty_error = error_value is None or error_value == ""
            if (
                isinstance(output_value, str)
                and (not require_complete_envelope or "error" in parsed)
                and type(exit_code) is int
                and exit_code == 0
                and empty_error
                and AgentRuntimeMemoryService._positive_test_output(output_value)
            ):
                return STRUCTURED_TEST_POLICY_ID, True
        return "execution_only.v1", False

    @staticmethod
    def _positive_test_output(output: str) -> bool:
        text = str(output or "")
        positive = bool(
            re.search(r"\b[1-9]\d*\s+(?:passed|passing)\b", text, re.I)
            or re.search(
                r"\bRan\s+[1-9]\d*\s+tests?\b[\s\S]{0,500}(?:^|\n)\s*OK\b",
                text,
                re.I,
            )
        )
        return positive

    @staticmethod
    def _positive_limit(value: object, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default
