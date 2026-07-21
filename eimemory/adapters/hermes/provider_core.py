from __future__ import annotations

from collections import OrderedDict, deque
from hashlib import sha256
import json
import logging
import os
from pathlib import Path
import re
import threading
import unicodedata
from typing import Any, Dict, List, Mapping, Optional

from eimemory.adapters.runtime.http_client import AgentRuntimeRPCClient
from eimemory.adapters.runtime.receipt_handoff import ReceiptIdHandoff
from eimemory.governance.tool_receipts import MAX_ELIGIBLE_RECEIPTS_PER_RUN


MAX_PREFETCH_CONTEXT_CHARS = 7_200
MAX_TURN_CHARS = 8_000
MAX_MEMORY_CHARS = 16_000
DEFAULT_MAX_WRITE_QUEUE = 16
DEFAULT_MAX_PREFETCH_CACHE_ENTRIES = 16
PREFETCH_SINGLE_FLIGHT_WAIT_SECONDS = 3.0
logger = logging.getLogger(__name__)
_PROACTIVE_CITATION = re.compile(r"(?<![A-Za-z0-9])pm:[0-9a-f]{20}(?![A-Za-z0-9])")


def hermes_client_from_env(*, hermes_home: str = "") -> AgentRuntimeRPCClient:
    try:
        timeout_seconds = float(os.getenv("EIMEMORY_ADAPTER_TIMEOUT_SECONDS", "0.8"))
    except ValueError:
        timeout_seconds = 0.8
    ledger = os.getenv("EIMEMORY_ADAPTER_FAILURE_LEDGER", "").strip()
    if not ledger and hermes_home:
        ledger = str(Path(hermes_home) / "logs" / "eimemory-adapter-failures.jsonl")
    return AgentRuntimeRPCClient(
        base_url=os.getenv("EIMEMORY_RPC_URL", "http://127.0.0.1:8091/").strip(),
        auth_token=os.getenv("EIMEMORY_RPC_TOKEN", "").strip(),
        timeout_seconds=timeout_seconds,
        failure_ledger_path=ledger or None,
    )


class HermesMemoryProviderCore:
    def __init__(
        self,
        *,
        client: Any | None = None,
        max_write_queue: int = DEFAULT_MAX_WRITE_QUEUE,
        max_prefetch_cache_entries: int = DEFAULT_MAX_PREFETCH_CACHE_ENTRIES,
    ) -> None:
        self._client = client
        self._client_injected = client is not None
        self._active = False
        self._write_enabled = True
        self._session_id = ""
        self._scope = self._scope_from_context({})
        self._max_write_queue = max(1, min(128, int(max_write_queue)))
        self._max_prefetch_cache_entries = max(1, min(128, int(max_prefetch_cache_entries)))
        self._write_queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._pending_proactive: OrderedDict[tuple[str, ...], dict[str, Any]] = OrderedDict()
        self._pending_terminal_retries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._max_terminal_retries = self._max_prefetch_cache_entries * 2
        self._lock = threading.RLock()
        self._write_thread: threading.Thread | None = None
        self._prefetch_thread: threading.Thread | None = None
        self._pending_prefetch: tuple[tuple[str, ...], str, str] | None = None
        self._inflight_prefetch: dict[tuple[str, ...], threading.Event] = {}
        self._inflight_prefetch_results: dict[tuple[str, ...], str] = {}
        self._inflight_prefetch_waiters: dict[tuple[str, ...], int] = {}
        self._last_turn_summary = ""
        self._dropped_write_count = 0
        self._receipt_handoff = ReceiptIdHandoff.from_env()
        self._verified_host_turns: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._verified_host_turn_overflow = False

    @property
    def name(self) -> str:
        return "eimemory"

    @property
    def prefetch_cache_size(self) -> int:
        # Completed proactive context is deliberately never cached locally.
        return 0

    @property
    def background_worker_count(self) -> int:
        with self._lock:
            return sum(
                1
                for worker in (self._write_thread, self._prefetch_thread)
                if worker is not None and worker.is_alive()
            )

    @property
    def dropped_write_count(self) -> int:
        with self._lock:
            return self._dropped_write_count

    @property
    def pending_terminal_retry_count(self) -> int:
        with self._lock:
            return len(self._pending_terminal_retries)

    def is_available(self) -> bool:
        if self._client_injected:
            return True
        return bool(os.getenv("EIMEMORY_RPC_TOKEN", "").strip())

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "").strip() or "hermes-session"
        hermes_home = str(kwargs.get("hermes_home") or "").strip()
        self._scope = self._scope_from_context(kwargs)
        agent_context = str(kwargs.get("agent_context") or "primary").strip().lower()
        self._write_enabled = agent_context not in {"cron", "flush", "subagent"}
        self._last_turn_summary = ""
        with self._lock:
            self._verified_host_turns.clear()
            self._verified_host_turn_overflow = False
            abandoned = self._take_all_pending_proactive_locked()
        if self._client is None:
            self._client = hermes_client_from_env(hermes_home=hermes_home)
        self._flush_terminal_retries()
        self._close_abandoned_pending(abandoned)
        self._active = self._client_injected or self.is_available()

    def system_prompt_block(self) -> str:
        if not self._active:
            return ""
        return (
            "# eimemory\n"
            "Active as the independent Hermes authoritative memory channel (authority_mode=per_channel). "
            "Recall before durable decisions, remember only reusable knowledge, and explicitly verify task outcomes."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        normalized_query = _bounded_text(query, 8_000)
        if not self._active or not normalized_query:
            return ""
        if not self._flush_terminal_retries():
            return ""
        effective_session = str(session_id or self._session_id)
        key = self._prefetch_key(effective_session, normalized_query)
        with self._lock:
            waiter = self._inflight_prefetch.get(key)
            owner = waiter is None
            if owner:
                waiter = threading.Event()
                self._inflight_prefetch[key] = waiter
                self._inflight_prefetch_results.pop(key, None)
                self._inflight_prefetch_waiters[key] = 0
            else:
                self._inflight_prefetch_waiters[key] = self._inflight_prefetch_waiters.get(key, 0) + 1
        if not owner:
            assert waiter is not None
            if not waiter.wait(timeout=PREFETCH_SINGLE_FLIGHT_WAIT_SECONDS):
                self._abandon_prefetch_wait(key)
                return ""
            return self._consume_prefetch_result(key)
        try:
            context = self._fetch_context(normalized_query, session_id=effective_session)
            with self._lock:
                self._inflight_prefetch_results[key] = context
            return context
        finally:
            self._complete_prefetch(key)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        normalized_query = _bounded_text(query, 8_000)
        if not self._active or not normalized_query:
            return
        if not self._flush_terminal_retries():
            return
        effective_session = str(session_id or self._session_id)
        key = self._prefetch_key(effective_session, normalized_query)
        with self._lock:
            if key in self._inflight_prefetch:
                return
            self._inflight_prefetch[key] = threading.Event()
            self._inflight_prefetch_waiters[key] = 0
            if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
                previous = self._pending_prefetch
                if previous is not None:
                    previous_waiter = self._inflight_prefetch.pop(previous[0], None)
                    self._inflight_prefetch_results.pop(previous[0], None)
                    self._inflight_prefetch_waiters.pop(previous[0], None)
                    if previous_waiter is not None:
                        previous_waiter.set()
                self._pending_prefetch = (key, normalized_query, effective_session)
                return
            worker = threading.Thread(
                target=self._prefetch_worker,
                args=(key, normalized_query, effective_session),
                daemon=True,
                name="eimemory-hermes-prefetch",
            )
            self._prefetch_thread = worker
            worker.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        del messages
        if not self._active or not self._write_enabled:
            return
        user_text = _bounded_text(user_content, MAX_TURN_CHARS)
        assistant_text = _bounded_text(assistant_content, MAX_TURN_CHARS)
        if not user_text and not assistant_text:
            return
        with self._lock:
            self._last_turn_summary = _bounded_text(
                f"eimemory last completed Hermes turn:\nUser: {user_text}\nAssistant: {assistant_text}",
                2_000,
            )
        effective_session = str(session_id or self._session_id).strip() or "hermes-session"
        turn_digest = sha256(
            f"{effective_session}\0{user_text}\0{assistant_text}".encode("utf-8", errors="replace")
        ).hexdigest()[:24]
        self._enqueue_write(
            "adapter.sync_turn",
            {
                **self._common_params(),
                "session_id": effective_session,
                "turn_id": f"turn-{turn_digest}",
                "user_text": user_text,
                "assistant_text": assistant_text,
            },
        )

    def on_pre_llm_call(
        self,
        *,
        user_message: str,
        session_id: str = "",
        turn_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Acknowledge only the exact proactive block already returned to Hermes."""

        self._flush_terminal_retries()
        del kwargs  # In particular, never iterate conversation_history.
        query = _bounded_text(user_message, 8_000)
        session = str(session_id or self._session_id).strip() or "hermes-session"
        host_turn = str(turn_id or "").strip()
        key = self._prefetch_key(session, query)
        with self._lock:
            pending = dict(self._pending_proactive.get(key) or {})
            if pending and host_turn:
                pending["host_turn_id"] = host_turn
                self._pending_proactive[key] = pending
        if not pending:
            return
        self._safe_call(
            "adapter.proactive_ack",
            {
                **self._common_params(),
                "source_ids": _source_ids_from_env("default"),
                "session_id": session,
                "turn_id": pending["decision_turn_id"],
                "decision_id": pending["decision_id"],
                "injected_citations": list(pending.get("citations") or []),
            },
        )

    def on_post_llm_call(
        self,
        *,
        user_message: str,
        assistant_message: str,
        session_id: str = "",
        turn_id: str = "",
        **kwargs: Any,
    ) -> None:
        """Close explicit-citation feedback and append one bounded completed turn."""

        self._flush_terminal_retries()
        del kwargs  # In particular, never iterate conversation_history.
        query = _bounded_text(user_message, MAX_TURN_CHARS)
        assistant = _bounded_text(assistant_message, MAX_TURN_CHARS)
        session = str(session_id or self._session_id).strip() or "hermes-session"
        host_turn = str(turn_id or "").strip()
        key = self._prefetch_key(session, _bounded_text(user_message, 8_000))
        with self._lock:
            pending = self._pending_proactive.pop(key, None)
            if pending is None and not query:
                matches = [
                    (pending_key, value)
                    for pending_key, value in self._pending_proactive.items()
                    if str(value.get("session_id") or "") == session
                ]
                if len(matches) == 1:
                    pending_key, pending = matches[0]
                    self._pending_proactive.pop(pending_key, None)
        if pending and not query:
            query = _bounded_text(pending.get("query"), MAX_TURN_CHARS)
        if pending:
            terminal_params = {
                "channel": "hermes",
                "scope": dict(pending.get("scope") or self._scope),
                "source_ids": list(pending.get("source_ids") or _source_ids_from_env("default")),
                "session_id": str(pending.get("session_id") or session),
                "turn_id": pending["decision_turn_id"],
                "decision_id": pending["decision_id"],
                "used_citations": sorted(set(_PROACTIVE_CITATION.findall(assistant))),
                # post_llm_call is not a host-attested task outcome.
                "terminal_outcome": {},
            }
            terminal = self._safe_call(
                "adapter.proactive_terminal",
                terminal_params,
            )
            if not self._terminal_call_succeeded(terminal):
                self._retain_terminal_retries([terminal_params])
        completed_turn = host_turn or str((pending or {}).get("host_turn_id") or "")
        completed_turn = completed_turn or str((pending or {}).get("decision_turn_id") or "")
        if not completed_turn and (query or assistant):
            completed_turn = "hermes-turn-" + sha256(
                f"{session}\0{query}\0{assistant}".encode("utf-8", errors="replace")
            ).hexdigest()[:24]
        if completed_turn and (query or assistant):
            self._safe_call(
                "adapter.proactive_complete_turn",
                {
                    **self._common_params(),
                    "source_ids": _source_ids_from_env("default"),
                    "session_id": session,
                    "turn_id": completed_turn,
                    "user_summary": query,
                    "assistant_summary": assistant,
                },
            )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "eimemory_recall",
                "description": "Recall authoritative long-term memory from the independent Hermes channel.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "task_type": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "eimemory_remember",
                "description": "Write reusable accepted knowledge to authoritative Hermes long-term memory.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "event_id": {"type": "string"},
                        "memory_type": {"type": "string"},
                        "title": {"type": "string"},
                    },
                    "required": ["text", "event_id"],
                },
            },
            {
                "name": "eimemory_verify_outcome",
                "description": "Close the unique host-verified Hermes turn using its bound release evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {"result": {"type": "string"}},
                    "required": ["result"],
                },
            },
            {
                "name": "eimemory_status",
                "description": "Check Hermes channel scope, authority mode, runtime health, and release binding.",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        del kwargs
        try:
            payload = self._handle_tool_call(tool_name, dict(args or {}))
        except (TypeError, ValueError) as exc:
            payload = {"ok": False, "error": str(exc)}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        normalized_action = str(action or "").strip().lower()
        text = _bounded_text(content, MAX_MEMORY_CHARS)
        if not self._active or not self._write_enabled or normalized_action not in {"add", "replace", "remove"}:
            return
        if normalized_action != "remove" and not text:
            return
        meta = dict(metadata or {})
        event_id = str(meta.get("event_id") or "").strip()
        if not event_id:
            event_id = _memory_write_fallback_event_id(
                source_id="hermes",
                session_id=self._session_id,
                action=normalized_action,
                target=str(target or "memory"),
                content=text,
                old_text=str(meta.get("old_text") or ""),
                target_record_id=str(meta.get("target_record_id") or ""),
                expected_revision=str(meta.get("expected_revision") or ""),
            )
        self._enqueue_write(
            "adapter.mutate_memory",
            {
                **self._common_params(),
                "action": normalized_action,
                "target": str(target or "memory"),
                "source_id": "hermes",
                "content": text,
                "idempotency_key": f"hermes.memory_write:{event_id}",
                "old_text": str(meta.get("old_text") or ""),
                "target_record_id": str(meta.get("target_record_id") or ""),
                "expected_revision": str(meta.get("expected_revision") or ""),
                "provenance": {
                    "write_origin": "hermes.memory_write",
                    **{
                        key: str(meta[key])
                        for key in (
                            "execution_context", "session_id", "parent_session_id", "platform",
                            "tool_name", "task_id", "task_call_id", "tool_call_id",
                        )
                        if isinstance(meta.get(key), str) and str(meta.get(key)).strip()
                    },
                    **({"session_id": self._session_id} if not str(meta.get("session_id") or "").strip() else {}),
                },
            },
        )

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        del messages
        if not self._active or not self._write_enabled:
            return
        self._safe_call(
            "adapter.record_terminal",
            {
                **self._common_params(),
                "end_kind": "session_end",
                "session_id": self._session_id,
                "event_id": f"{self._session_id}:session_end",
                "task_type": "session.lifecycle",
                "success": None,
                "verification": "",
                "result": "Hermes session lifecycle ended",
                "tool_receipts": [],
                "rehearsal": False,
            },
        )

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        del parent_session_id, kwargs
        self._flush_terminal_retries()
        next_session_id = str(new_session_id or "").strip() or self._session_id
        abandoned: list[dict[str, Any]] = []
        with self._lock:
            if next_session_id != self._session_id:
                self._verified_host_turns.clear()
                self._verified_host_turn_overflow = False
                abandoned.extend(self._take_all_pending_proactive_locked())
                for waiter in self._inflight_prefetch.values():
                    waiter.set()
                self._inflight_prefetch.clear()
                self._inflight_prefetch_results.clear()
                self._inflight_prefetch_waiters.clear()
            self._session_id = next_session_id
            if reset or rewound:
                self._last_turn_summary = ""
                abandoned.extend(self._take_all_pending_proactive_locked())
        self._close_abandoned_pending(abandoned)

    def bind_verified_host_turn(self, *, session_id: str, turn_id: str) -> bool:
        """Bind one host-attested turn for a later model-requested terminal close."""
        normalized_session = str(session_id or "").strip()
        normalized_turn = str(turn_id or "").strip()
        if not normalized_session or not normalized_turn:
            return False
        with self._lock:
            if normalized_session != self._session_id:
                return False
            key = (normalized_session, normalized_turn)
            if key in self._verified_host_turns:
                self._verified_host_turns.move_to_end(key)
                return True
            if len(self._verified_host_turns) >= MAX_ELIGIBLE_RECEIPTS_PER_RUN:
                self._verified_host_turn_overflow = True
                return False
            self._verified_host_turns[key] = None
            return True

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        del messages
        with self._lock:
            return self._last_turn_summary

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "", **kwargs: Any) -> None:
        del kwargs
        self.sync_turn(
            f"Delegated task: {_bounded_text(task, MAX_TURN_CHARS)}",
            f"Delegated result: {_bounded_text(result, MAX_TURN_CHARS)}",
            session_id=child_session_id or self._session_id,
        )

    def shutdown(self) -> None:
        self._flush_terminal_retries()
        with self._lock:
            workers = [self._prefetch_thread, self._write_thread]
        for worker in workers:
            if worker is not None and worker.is_alive():
                worker.join(timeout=20.0)
        with self._lock:
            if self._prefetch_thread is not None and not self._prefetch_thread.is_alive():
                self._prefetch_thread = None
            if self._write_thread is not None and not self._write_thread.is_alive():
                self._write_thread = None
            abandoned = self._take_all_pending_proactive_locked()
        self._close_abandoned_pending(abandoned)
        self._flush_terminal_retries()

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "rpc_url",
                "description": "Authenticated eimemory RPC endpoint.",
                "required": False,
                "default": "http://127.0.0.1:8091/",
                "env_var": "EIMEMORY_RPC_URL",
            },
            {
                "key": "rpc_token",
                "description": "Strong eimemory RPC bearer token.",
                "required": True,
                "secret": True,
                "env_var": "EIMEMORY_RPC_TOKEN",
            },
        ]

    def _handle_tool_call(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        common = self._common_params()
        if tool_name == "eimemory_recall":
            return self._safe_call(
                "adapter.prefetch",
                {
                    **common,
                    "query": _required_text(args, "query"),
                    "task_type": str(args.get("task_type") or "research.task"),
                    "limit": max(1, min(50, int(args.get("limit", 8)))),
                },
            )
        if tool_name == "eimemory_remember":
            return self._safe_call(
                "adapter.remember",
                {
                    **common,
                    "text": _required_text(args, "text")[:MAX_MEMORY_CHARS],
                    "event_id": _required_text(args, "event_id"),
                    "memory_type": str(args.get("memory_type") or "durable_fact"),
                    "title": str(args.get("title") or "Hermes long-term memory"),
                    "force_capture": False,
                },
            )
        if tool_name == "eimemory_verify_outcome":
            with self._lock:
                bindings = tuple(self._verified_host_turns)
                if self._verified_host_turn_overflow or len(bindings) != 1:
                    raise ValueError("exactly one unfinalized host-verified Hermes turn is required")
                session_id, event_id = bindings[0]
                if session_id != self._session_id:
                    raise ValueError("terminal identity does not match the active Hermes session")
            receipt_ids = (
                self._receipt_handoff.list_ids(
                    channel="hermes",
                    scope=self._scope,
                    session_id=session_id,
                    run_id=event_id,
                )
                if self._receipt_handoff is not None
                else []
            )
            terminal = self._safe_call(
                "adapter.record_terminal",
                {
                    **common,
                    "end_kind": "task_end",
                    "session_id": session_id,
                    "event_id": event_id,
                    # Model-provided claims are diagnostic only. The runtime
                    # derives terminal status and task type from exact receipts.
                    "task_type": "research.unverified",
                    "success": None,
                    "verification": "",
                    "result": str(args.get("result") or "")[:2_000],
                    "tool_receipts": [],
                    "receipt_ids": receipt_ids,
                    "rehearsal": False,
                },
            )
            terminal_result = terminal.get("result") if isinstance(terminal, dict) else None
            if (
                terminal.get("ok") is True
                and isinstance(terminal_result, dict)
                and terminal_result.get("ok") is True
                and self._receipt_handoff is not None
            ):
                self._receipt_handoff.clear_exact(
                    channel="hermes",
                    scope=self._scope,
                    session_id=session_id,
                    run_id=event_id,
                    receipt_ids=receipt_ids,
                )
                with self._lock:
                    self._verified_host_turns.pop((session_id, event_id), None)
            return terminal
        if tool_name == "eimemory_status":
            status = self._safe_call("adapter.status", common)
            return {
                **status,
                "adapter_local": {
                    "dropped_writes": self.dropped_write_count,
                    "prefetch_cache_entries": self.prefetch_cache_size,
                    "pending_terminal_retries": self.pending_terminal_retry_count,
                    "background_workers": self.background_worker_count,
                },
            }
        raise ValueError(f"unknown eimemory tool: {tool_name!r}")

    def _common_params(self) -> dict[str, Any]:
        return {"channel": "hermes", "scope": dict(self._scope)}

    def _safe_call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            result = self._client.call_or_bypass(method, params)
        except Exception:
            return {"ok": False, "bypassed": True, "error": "adapter_unavailable", "result": None}
        return result if isinstance(result, dict) else {
            "ok": False,
            "bypassed": True,
            "error": "adapter_unavailable",
            "result": None,
        }

    def _fetch_context(self, query: str, *, session_id: str = "") -> str:
        session = str(session_id or self._session_id).strip() or "hermes-session"
        source_ids = _source_ids_from_env("default")
        decision_turn_id = "hermes-query-" + sha256(
            f"{session}\0{query}".encode("utf-8", errors="replace")
        ).hexdigest()[:24]
        result = self._safe_call(
            "adapter.proactive_prefetch",
            {
                **self._common_params(),
                "source_ids": source_ids,
                "session_id": session,
                "turn_id": decision_turn_id,
                "query": query,
                "task_type": "research.task",
            },
        )
        if result.get("ok") is not True or not isinstance(result.get("result"), dict):
            return ""
        payload = result["result"]
        context = _bounded_text(payload.get("context"), MAX_PREFETCH_CONTEXT_CHARS)
        decision_id = _bounded_text(payload.get("decision_id"), 200)
        if context and decision_id:
            key = self._prefetch_key(session, query)
            abandoned: list[dict[str, Any]] = []
            with self._lock:
                previous = self._pending_proactive.pop(key, None)
                if previous is not None and str(previous.get("decision_id") or "") != decision_id:
                    abandoned.append(dict(previous))
                self._pending_proactive[key] = {
                    "decision_id": decision_id,
                    "decision_turn_id": decision_turn_id,
                    "citations": sorted(set(_PROACTIVE_CITATION.findall(context))),
                    "query": query,
                    "session_id": session,
                    "scope": dict(self._scope),
                    "source_ids": list(source_ids),
                }
                self._pending_proactive.move_to_end(key)
                while len(self._pending_proactive) > self._max_prefetch_cache_entries:
                    _dropped_key, dropped = self._pending_proactive.popitem(last=False)
                    abandoned.append(dict(dropped))
            self._close_abandoned_pending(abandoned)
        return context

    def _prefetch_worker(self, key: tuple[str, ...], query: str, session_id: str) -> None:
        while True:
            try:
                context = self._fetch_context(query, session_id=session_id)
                with self._lock:
                    self._inflight_prefetch_results[key] = context
            except Exception as exc:  # noqa: BLE001 - keep the bounded worker alive
                logger.debug(
                    "Hermes eimemory prefetch failed type=%s",
                    type(exc).__name__,
                )
            finally:
                self._complete_prefetch(key, background=True)
            with self._lock:
                pending = self._pending_prefetch
                self._pending_prefetch = None
                if pending is not None:
                    key, query, session_id = pending
                    continue
                self._prefetch_thread = None
                return

    def _complete_prefetch(self, key: tuple[str, ...], *, background: bool = False) -> None:
        abandoned: dict[str, Any] = {}
        with self._lock:
            waiter = self._inflight_prefetch.get(key)
            if self._inflight_prefetch_waiters.get(key, 0) <= 0:
                self._inflight_prefetch.pop(key, None)
                self._inflight_prefetch_results.pop(key, None)
                self._inflight_prefetch_waiters.pop(key, None)
                if background:
                    abandoned = dict(self._pending_proactive.pop(key, None) or {})
            if waiter is not None:
                waiter.set()
        self._close_abandoned_pending([abandoned] if abandoned else [])

    def _take_all_pending_proactive_locked(self) -> list[dict[str, Any]]:
        abandoned = [dict(item) for item in self._pending_proactive.values()]
        self._pending_proactive.clear()
        return abandoned

    def _close_abandoned_pending(self, abandoned: List[Mapping[str, Any]]) -> None:
        """Terminalize every decision removed before a host can acknowledge it."""

        failed: list[dict[str, Any]] = []
        for pending in abandoned[: self._max_prefetch_cache_entries]:
            decision_id = str(pending.get("decision_id") or "")
            turn_id = str(pending.get("decision_turn_id") or "")
            if not decision_id or not turn_id:
                continue
            params = {
                "channel": "hermes",
                "scope": dict(pending.get("scope") or self._scope),
                "source_ids": list(pending.get("source_ids") or ["default"]),
                "session_id": str(pending.get("session_id") or self._session_id),
                "turn_id": turn_id,
                "decision_id": decision_id,
                "used_citations": [],
                "terminal_outcome": {},
            }
            result = self._safe_call(
                "adapter.proactive_terminal",
                params,
            )
            if not self._terminal_call_succeeded(result):
                failed.append(params)
        if failed:
            self._retain_terminal_retries(failed)

    def _retain_terminal_retries(self, entries: List[Mapping[str, Any]]) -> None:
        with self._lock:
            for raw in entries:
                if not self._valid_terminal_retry(raw):
                    continue
                params = dict(raw)
                key = self._terminal_retry_key(params)
                if key in self._pending_terminal_retries:
                    continue
                if len(self._pending_terminal_retries) >= self._max_terminal_retries:
                    raise RuntimeError("Hermes proactive terminal retry capacity exhausted")
                self._pending_terminal_retries[key] = params

    def _flush_terminal_retries(self) -> bool:
        with self._lock:
            pending = [(key, dict(params)) for key, params in self._pending_terminal_retries.items()]
        for key, params in pending:
            result = self._safe_call("adapter.proactive_terminal", params)
            if not self._terminal_call_succeeded(result):
                continue
            with self._lock:
                if self._pending_terminal_retries.get(key) == params:
                    self._pending_terminal_retries.pop(key, None)
        with self._lock:
            return not self._pending_terminal_retries

    @staticmethod
    def _terminal_retry_key(params: Mapping[str, Any]) -> str:
        canonical = json.dumps(dict(params), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode("utf-8", errors="replace")).hexdigest()

    @staticmethod
    def _valid_terminal_retry(raw: Any) -> bool:
        if not isinstance(raw, Mapping):
            return False
        return (
            raw.get("channel") == "hermes"
            and isinstance(raw.get("scope"), Mapping)
            and isinstance(raw.get("source_ids"), list)
            and all(isinstance(item, str) and item for item in raw.get("source_ids", []))
            and all(
                isinstance(raw.get(key), str) and bool(str(raw.get(key)).strip())
                for key in ("session_id", "turn_id", "decision_id")
            )
            and isinstance(raw.get("used_citations"), list)
            and all(
                isinstance(item, str) and bool(_PROACTIVE_CITATION.fullmatch(item))
                for item in raw.get("used_citations", [])
            )
            and raw.get("terminal_outcome") == {}
        )

    @staticmethod
    def _terminal_call_succeeded(result: Mapping[str, Any]) -> bool:
        if result.get("ok") is not True:
            return False
        nested = result.get("result")
        return not isinstance(nested, Mapping) or nested.get("ok") is not False

    def _consume_prefetch_result(self, key: tuple[str, ...]) -> str:
        with self._lock:
            context = self._inflight_prefetch_results.get(key, "")
            remaining = max(0, self._inflight_prefetch_waiters.get(key, 1) - 1)
            if remaining:
                self._inflight_prefetch_waiters[key] = remaining
            else:
                self._inflight_prefetch.pop(key, None)
                self._inflight_prefetch_results.pop(key, None)
                self._inflight_prefetch_waiters.pop(key, None)
            return context

    def _abandon_prefetch_wait(self, key: tuple[str, ...]) -> None:
        with self._lock:
            remaining = max(0, self._inflight_prefetch_waiters.get(key, 1) - 1)
            self._inflight_prefetch_waiters[key] = remaining

    def _prefetch_key(self, session_id: str, query: str) -> tuple[str, ...]:
        return (
            "hermes",
            str(self._scope.get("tenant_id") or ""),
            str(self._scope.get("agent_id") or ""),
            str(self._scope.get("workspace_id") or ""),
            str(self._scope.get("user_id") or ""),
            *tuple(_source_ids_from_env("default")),
            str(session_id or ""),
            str(query or ""),
        )

    def _enqueue_write(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            if len(self._write_queue) >= self._max_write_queue:
                dropped_method, _ = self._write_queue.popleft()
                self._dropped_write_count += 1
                logger.warning(
                    "Hermes eimemory write queue dropped oldest item method=%s total=%d",
                    dropped_method,
                    self._dropped_write_count,
                )
            self._write_queue.append((method, params))
            if self._write_thread is not None and self._write_thread.is_alive():
                return
            worker = threading.Thread(
                target=self._write_worker,
                daemon=True,
                name="eimemory-hermes-writer",
            )
            self._write_thread = worker
            worker.start()

    def _write_worker(self) -> None:
        while True:
            with self._lock:
                if not self._write_queue:
                    self._write_thread = None
                    return
                method, params = self._write_queue.popleft()
            self._safe_call(method, params)

    @staticmethod
    def _scope_from_context(context: Mapping[str, Any]) -> dict[str, str]:
        return {
            "tenant_id": os.getenv("EIMEMORY_TENANT_ID", "default").strip() or "default",
            "agent_id": (
                os.getenv("EIMEMORY_AGENT_ID", "").strip()
                or str(context.get("agent_identity") or "hermes").strip()
                or "hermes"
            ),
            "workspace_id": (
                os.getenv("EIMEMORY_WORKSPACE_ID", "").strip()
                or str(context.get("agent_workspace") or "default").strip()
                or "default"
            ),
            "user_id": (
                os.getenv("EIMEMORY_USER_ID", "").strip()
                or str(context.get("user_id") or "default").strip()
                or "default"
            ),
        }


def _bounded_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit]


def _source_ids_from_env(default_source: str) -> list[str]:
    configured = [
        value.strip()
        for value in os.getenv("EIMEMORY_SOURCE_IDS", "").split(",")
        if value.strip()
    ]
    return configured or [default_source]


def _memory_write_fallback_event_id(
    *,
    source_id: str,
    session_id: str,
    action: str,
    target: str,
    content: str,
    old_text: str,
    target_record_id: str,
    expected_revision: str,
) -> str:
    payload = json.dumps(
        {
            "source_id": str(source_id or "").strip().casefold(),
            "session_id": str(session_id or "").strip(),
            "action": str(action or "").strip().lower(),
            "target": str(target or "").strip().lower(),
            "content_revision": _normalized_text_revision(content),
            "old_content_revision": _normalized_text_revision(old_text) if str(old_text or "").strip() else "",
            "target_record_id": str(target_record_id or "").strip(),
            "expected_revision": str(expected_revision or "").strip().lower(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "memory-" + sha256(payload.encode("utf-8")).hexdigest()[:24]


def _normalized_text_revision(value: object) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())
    return sha256(normalized.encode("utf-8")).hexdigest()


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()
