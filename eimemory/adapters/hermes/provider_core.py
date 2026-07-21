from __future__ import annotations

from collections import OrderedDict, deque
from hashlib import sha256
import json
import os
from pathlib import Path
import threading
from typing import Any, Dict, List, Mapping, Optional

from eimemory.adapters.runtime.http_client import AgentRuntimeRPCClient


MAX_PREFETCH_CONTEXT_CHARS = 7_200
MAX_TURN_CHARS = 8_000
MAX_MEMORY_CHARS = 16_000
DEFAULT_MAX_WRITE_QUEUE = 16
DEFAULT_MAX_PREFETCH_CACHE_ENTRIES = 16


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
        self._hermes_home = ""
        self._max_write_queue = max(1, min(128, int(max_write_queue)))
        self._max_prefetch_cache_entries = max(1, min(128, int(max_prefetch_cache_entries)))
        self._write_queue: deque[tuple[str, dict[str, Any]]] = deque()
        self._prefetch_cache: OrderedDict[tuple[str, str], str] = OrderedDict()
        self._lock = threading.RLock()
        self._write_thread: threading.Thread | None = None
        self._prefetch_thread: threading.Thread | None = None
        self._pending_prefetch: tuple[tuple[str, str], str] | None = None

    @property
    def name(self) -> str:
        return "eimemory"

    @property
    def prefetch_cache_size(self) -> int:
        with self._lock:
            return len(self._prefetch_cache)

    @property
    def background_worker_count(self) -> int:
        with self._lock:
            return sum(
                1
                for worker in (self._write_thread, self._prefetch_thread)
                if worker is not None and worker.is_alive()
            )

    def is_available(self) -> bool:
        if self._client_injected:
            return True
        return bool(os.getenv("EIMEMORY_RPC_URL", "").strip() and os.getenv("EIMEMORY_RPC_TOKEN", "").strip())

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "").strip() or "hermes-session"
        self._hermes_home = str(kwargs.get("hermes_home") or "").strip()
        self._scope = self._scope_from_context(kwargs)
        agent_context = str(kwargs.get("agent_context") or "primary").strip().lower()
        self._write_enabled = agent_context not in {"cron", "flush", "subagent"}
        if self._client is None:
            self._client = hermes_client_from_env(hermes_home=self._hermes_home)
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
        key = (str(session_id or self._session_id), normalized_query)
        with self._lock:
            cached = self._prefetch_cache.get(key)
            if cached is not None:
                self._prefetch_cache.move_to_end(key)
                return cached
        context = self._fetch_context(normalized_query)
        self._cache_context(key, context)
        return context

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        normalized_query = _bounded_text(query, 8_000)
        if not self._active or not normalized_query:
            return
        key = (str(session_id or self._session_id), normalized_query)
        with self._lock:
            if key in self._prefetch_cache:
                return
            if self._prefetch_thread is not None and self._prefetch_thread.is_alive():
                self._pending_prefetch = (key, normalized_query)
                return
            worker = threading.Thread(
                target=self._prefetch_worker,
                args=(key, normalized_query),
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
                "description": "Record an explicitly verified Hermes task outcome bound to release evidence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "event_id": {"type": "string"},
                        "task_type": {"type": "string"},
                        "success": {"type": "boolean"},
                        "verification": {"type": "string"},
                        "result": {"type": "string"},
                    },
                    "required": ["session_id", "event_id", "task_type", "success", "verification"],
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
        if not self._active or not self._write_enabled or normalized_action not in {"add", "replace"} or not text:
            return
        meta = dict(metadata or {})
        event_id = str(meta.get("event_id") or "").strip()
        if not event_id:
            event_id = "memory-" + sha256(
                f"{self._session_id}\0{normalized_action}\0{target}\0{text}".encode("utf-8", errors="replace")
            ).hexdigest()[:24]
        self._enqueue_write(
            "adapter.remember",
            {
                **self._common_params(),
                "text": text,
                "event_id": event_id,
                "memory_type": "preference" if str(target or "") == "user" else "durable_fact",
                "title": "Hermes mirrored long-term memory",
                "force_capture": False,
                "meta": {
                    "capture_origin": "hermes.memory_write",
                    "hermes_action": normalized_action,
                    "hermes_target": str(target or "memory"),
                    "session_id": str(meta.get("session_id") or self._session_id),
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
        del parent_session_id, reset, rewound, kwargs
        self._session_id = str(new_session_id or "").strip() or self._session_id

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        del messages
        with self._lock:
            if not self._prefetch_cache:
                return ""
            return _bounded_text(next(reversed(self._prefetch_cache.values())), 2_000)

    def on_delegation(self, task: str, result: str, *, child_session_id: str = "", **kwargs: Any) -> None:
        del kwargs
        if not self._write_enabled:
            return
        self.sync_turn(
            f"Delegated task: {_bounded_text(task, MAX_TURN_CHARS)}",
            f"Delegated result: {_bounded_text(result, MAX_TURN_CHARS)}",
            session_id=child_session_id or self._session_id,
        )

    def shutdown(self) -> None:
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

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "rpc_url",
                "description": "Authenticated eimemory RPC endpoint.",
                "required": True,
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
            success = args.get("success")
            if not isinstance(success, bool):
                raise ValueError("success must be a boolean")
            return self._safe_call(
                "adapter.record_terminal",
                {
                    **common,
                    "end_kind": "task_end",
                    "session_id": _required_text(args, "session_id"),
                    "event_id": _required_text(args, "event_id"),
                    "task_type": _required_text(args, "task_type"),
                    "success": success,
                    "verification": _required_text(args, "verification")[:512],
                    "result": str(args.get("result") or "")[:2_000],
                    "tool_receipts": [],
                    "rehearsal": False,
                },
            )
        if tool_name == "eimemory_status":
            return self._safe_call("adapter.status", common)
        raise ValueError("unknown eimemory tool")

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

    def _fetch_context(self, query: str) -> str:
        result = self._safe_call(
            "adapter.prefetch",
            {
                **self._common_params(),
                "query": query,
                "task_type": "research.task",
                "limit": 8,
            },
        )
        if result.get("ok") is not True or not isinstance(result.get("result"), dict):
            return ""
        return _bounded_text(result["result"].get("context"), MAX_PREFETCH_CONTEXT_CHARS)

    def _prefetch_worker(self, key: tuple[str, str], query: str) -> None:
        current_key = key
        current_query = query
        while True:
            self._cache_context(key, self._fetch_context(query))
            with self._lock:
                pending = self._pending_prefetch
                self._pending_prefetch = None
                if pending is not None:
                    current_key, current_query = pending
                    key, query = current_key, current_query
                    continue
                self._prefetch_thread = None
                return

    def _cache_context(self, key: tuple[str, str], context: str) -> None:
        with self._lock:
            self._prefetch_cache[key] = _bounded_text(context, MAX_PREFETCH_CONTEXT_CHARS)
            self._prefetch_cache.move_to_end(key)
            while len(self._prefetch_cache) > self._max_prefetch_cache_entries:
                self._prefetch_cache.popitem(last=False)

    def _enqueue_write(self, method: str, params: dict[str, Any]) -> None:
        with self._lock:
            if len(self._write_queue) >= self._max_write_queue:
                self._write_queue.popleft()
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


def _required_text(arguments: Mapping[str, Any], name: str) -> str:
    value = str(arguments.get(name) or "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value
