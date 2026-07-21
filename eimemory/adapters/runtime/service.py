from __future__ import annotations

import json
from hashlib import sha256
from typing import Any

from eimemory.adapters.runtime.channel import (
    AUTHORITY_MODE,
    RUNTIME_ADAPTER_CONTRACT_VERSION,
    base_scope_from_channel,
    normalize_runtime_channel,
    resolve_channel_scope,
)
from eimemory.api.runtime import Runtime
from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef


DEFAULT_MAX_CONTEXT_CHARS = 7_200
DEFAULT_MAX_TURN_CHARS = 12_000
DEFAULT_MAX_MEMORY_CHARS = 16_000


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
        )
        if record.status != "active":
            record.meta["authoritative"] = False
            business_meta = record.meta.get("business_meta")
            if isinstance(business_meta, dict):
                business_meta["authoritative"] = False
        return self._memory_result(record, channel=channel_id, scope=channel_scope, idempotent=False)

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
        turn_text = self._bounded_text(
            f"User: {str(user_text or '').strip()}\nAssistant: {str(assistant_text or '').strip()}",
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
        verification: str = "",
        result: str = "",
        tool_receipts: list[dict[str, Any]] | None = None,
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
        verification_text = self._bounded_text(verification, 512)
        result_text = self._bounded_text(result, 2_000)
        lifecycle_only = normalized_end_kind == "session_end"
        event_payload: dict[str, Any] = {
            "id": f"evt_{channel_id}_{trace_id[-24:]}",
            "idempotency_key": f"{method}:{normalized_event_id}",
            "source": method,
            "hook": normalized_end_kind,
            "session_id": normalized_session_id,
            "outcome_trace_id": trace_id,
            "outcome_trace_task_type": normalized_task_type,
            "event_type": normalized_task_type,
            "goal": normalized_task_type,
            "verification": verification_text,
            "verification_receipts": list(tool_receipts or [])[:32],
            "result": result_text,
            "evidence_class": "lifecycle_event" if lifecycle_only else "verified_real_task",
            "runtime_channel": channel_id,
            "authority_mode": AUTHORITY_MODE,
        }
        release = current_release_identity(self.runtime, ScopeRef.from_dict(channel_scope))
        if release is None and channel_id != "openclaw":
            release = current_release_identity(
                self.runtime,
                ScopeRef.from_dict(base_scope_from_channel(channel_id, channel_scope)),
            )
        if release is not None:
            event_payload.update(release_identity_payload(release))
        recorded_event = self.runtime.record_event(event_payload, scope=channel_scope)
        if lifecycle_only:
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
        recorded_outcome = self.runtime.record_outcome(recorded_event["id"], outcome_payload, scope=channel_scope)
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
                "status": "success" if success is True else "bad" if success is False else "uncertain",
                "success": success,
                "rehearsal": bool(rehearsal),
            },
            "verifier": {
                "passed": bool(success is True and explicit_verification),
                "method": method,
                "evidence_refs": [recorded_event["id"]],
                "checks": {
                    "verification": verification_text,
                    "result": result_text,
                    "tool_receipts": list(tool_receipts or [])[:32],
                },
            },
        }
        if release is not None:
            outcome_trace_payload.update(release_identity_payload(release))
            outcome_trace_payload["evidence_class"] = "verified_real_task"
        outcome_trace = self.runtime.record_outcome_trace(outcome_trace_payload, scope=channel_scope)
        return {
            "ok": bool(outcome_trace.get("ok")),
            "event": recorded_event,
            "outcome": recorded_outcome,
            "outcome_trace": outcome_trace,
        }

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
    def _bounded_text(value: object, limit: int) -> str:
        text = str(value or "").strip()
        return text if len(text) <= limit else text[:limit]

    @staticmethod
    def _positive_limit(value: object, default: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return number if number > 0 else default
