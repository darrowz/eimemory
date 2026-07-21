from __future__ import annotations

import json
from typing import Any

from eimemory.api.runtime import Runtime
from eimemory.experience import record_experience_item, record_skill_trace
from eimemory.identity import extract_user_aliases, hongtu_identity_meta, hongtu_scope
from eimemory.models.records import LinkRef, ScopeRef
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.ei_bridge.protocol import (
    EIMEMORY_RPC_CONTRACT_VERSION,
    BridgeScope,
    EIMemoryRPCRequest,
    EIMemoryRPCResponse,
)


class EIBrainRPCBridge:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.runtime_adapter = AgentRuntimeMemoryService(runtime)

    def handle(self, request: EIMemoryRPCRequest) -> EIMemoryRPCResponse:
        # RPC transport boundary: accept only dict-shaped request payloads then map
        # directly into runtime services with strict per-method validation.
        if not isinstance(request, dict):
            return self._with_contract(self._invalid_request())
        method = request.get("method")
        if not isinstance(method, str):
            return self._with_contract(self._invalid_request())
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._with_contract(self._invalid_request())
        if method.startswith("adapter."):
            return self._handle_runtime_adapter(method, params)
        if method in {"memory.recall", "memory.search"}:
            limit = params.get("limit", 8)
            scope: BridgeScope = params.get("scope", {})
            task_context = params.get("task_context", {})
            query = params.get("query", "")
            if (
                not isinstance(query, str)
                or not query.strip()
                or not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit <= 0
                or not self._valid_scope(scope)
                or not self._valid_task_context(task_context)
            ):
                return self._with_contract(self._invalid_request())
            resolved_scope = self._resolve_scope(scope, aliases=extract_user_aliases(task_context))
            bundle = self.runtime.memory.recall(
                query=query,
                scope=resolved_scope,
                task_context=task_context,
                limit=limit,
            )
            result = bundle.to_dict()
            if scope.get("preserve_scope") is True:
                result = self._filter_recall_result_to_scope(result, resolved_scope)
            return self._with_contract({"ok": True, "result": result})
        if method in {"memory.ingest", "memory.remember"}:
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            text = params.get("text", "")
            memory_type = params.get("memory_type", "conversation")
            title = params.get("title", "eibrain memory")
            outcome = params.get("outcome", {})
            content = params.get("content", {})
            meta = params.get("meta", {})
            tags = params.get("tags", [])
            evidence = params.get("evidence", [])
            links = params.get("links", [])
            force_capture = params.get("force_capture", False)
            if (
                not isinstance(text, str)
                or not text.strip()
                or not isinstance(memory_type, str)
                or not memory_type.strip()
                or not isinstance(title, str)
                or not title.strip()
                or not isinstance(outcome, dict)
                or not isinstance(content, dict)
                or not isinstance(meta, dict)
                or not self._valid_tags(tags)
                or not self._valid_list(evidence)
                or not self._valid_list(links)
                or not isinstance(force_capture, bool)
                or not self._valid_scope(scope)
            ):
                return self._with_contract(self._invalid_request())
            source = str(params.get("source") or "eibrain.dialogue")
            resolved_scope = self._resolve_scope(scope, aliases=meta)
            record = self.runtime.memory.ingest(
                text=text,
                memory_type=memory_type,
                title=title,
                scope=resolved_scope,
                source=source,
                tags=[str(tag) for tag in tags],
                content=content,
                evidence=self._normalize_evidence(evidence),
                links=self._normalize_links(links),
                force_capture=force_capture,
                meta=hongtu_identity_meta(
                    source=source,
                    channel="eibrain",
                    hardware_node=str(scope.get("hardware_node") or scope.get("node_id") or "honxin"),
                    organ=str(params.get("organ") or "cognition"),
                    modality=str(params.get("modality") or "text"),
                    extra={
                        "runtime_node": str(scope.get("agent_id") or "eibrain"),
                        **dict(meta),
                        **({"outcome": dict(outcome)} if outcome else {}),
                    },
                ),
            )
            result = record.to_dict()
            if record.status == "rejected":
                result["warnings"] = list(record.meta.get("capture_warnings") or [])
            return self._with_contract({"ok": True, "result": result})
        if method == "memory.observe":
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            observation = params.get("observation", params.get("payload", {}))
            if not isinstance(observation, dict) or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            result = self.runtime.observe_coding_memory(observation, scope=self._resolve_scope(scope))
            return self._with_contract({"ok": bool(result.get("ok")), "result": result})
        if method == "memory.graph":
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            query = params.get("query", "")
            limit = params.get("limit", 5)
            if (
                not isinstance(query, str)
                or not query.strip()
                or not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit <= 0
                or not self._valid_scope(scope)
            ):
                return self._with_contract(self._invalid_request())
            result = self.runtime.query_coding_memory_graph(
                query,
                scope=self._resolve_scope(scope),
                limit=limit,
            )
            return self._with_contract({"ok": bool(result.get("ok")), "result": result})
        if method == "memory.replay":
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            query = params.get("query", "")
            expected_relations = params.get("expected_relations", [])
            persist = params.get("persist", False)
            if (
                not isinstance(query, str)
                or not query.strip()
                or not self._valid_list(expected_relations)
                or not isinstance(persist, bool)
                or not self._valid_scope(scope)
            ):
                return self._with_contract(self._invalid_request())
            result = self.runtime.run_coding_graph_replay(
                query=query,
                expected_relations=[str(item) for item in expected_relations],
                scope=self._resolve_scope(scope),
                persist=persist,
            )
            return self._with_contract({"ok": bool(result.get("ok")), "result": result})
        if method == "memory.audit":
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            limit = params.get("limit", 50)
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit <= 0
                or not self._valid_scope(scope)
            ):
                return self._with_contract(self._invalid_request())
            result = self.runtime.audit_coding_memory_contract(scope=self._resolve_scope(scope), limit=limit)
            return self._with_contract({"ok": bool(result.get("ok")), "result": result})
        if method in {"memory.record_event", "memory.recordEvent"}:
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            payload = params.get("event", params.get("payload", {}))
            if not isinstance(payload, dict) or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            result = self.runtime.record_event(payload, scope=self._resolve_scope(scope))
            return self._with_contract({"ok": True, "result": result})
        if method in {"memory.record_outcome", "memory.recordOutcome"}:
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            event_id = params.get("event_id", "")
            payload = params.get("outcome", params.get("payload", {}))
            if not isinstance(event_id, str) or not event_id.strip() or not isinstance(payload, dict) or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            result = self.runtime.record_outcome(event_id, payload, scope=self._resolve_scope(scope))
            return self._with_contract({"ok": True, "result": result})
        if method in {"memory.upsert_intent_pattern", "memory.upsertIntentPattern"}:
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            payload = params.get("pattern", params.get("payload", {}))
            if not isinstance(payload, dict) or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            result = self.runtime.upsert_intent_pattern(payload, scope=self._resolve_scope(scope))
            return self._with_contract({"ok": True, "result": result})
        if method in {"memory.search_policy", "memory.searchPolicy"}:
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            query = params.get("query", params.get("user_phrase", ""))
            context = params.get("context", {})
            limit = params.get("limit", 5)
            if (
                not isinstance(query, str)
                or not query.strip()
                or not isinstance(context, dict)
                or not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit <= 0
                or not self._valid_scope(scope)
            ):
                return self._with_contract(self._invalid_request())
            result = self.runtime.search_policy(
                query,
                scope=self._resolve_scope(scope),
                context=context,
                limit=limit,
            )
            return self._with_contract({"ok": True, "result": result})
        if method == "evolution.observe":
            params = dict(params)
            payload = params.get("payload", {})
            scope: BridgeScope = params.get("scope", {})
            if (
                not isinstance(params.get("signal_type", ""), str)
                or not str(params.get("signal_type", "")).strip()
                or not isinstance(payload, dict)
                or not self._valid_scope(scope)
            ):
                return self._with_contract(self._invalid_request())
            record = self.runtime.evolution.observe(
                signal_type=params.get("signal_type") or "",
                payload=payload,
                scope=self._resolve_scope(scope),
            )
            return self._with_contract({"ok": True, "result": record.to_dict()})
        if method == "experience.record_skill_trace":
            params = dict(params)
            payload = params.get("payload", {})
            scope: BridgeScope = params.get("scope", {})
            if not isinstance(payload, dict) or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            result = record_skill_trace(self.runtime, payload, scope=self._resolve_scope(scope))
            if result.get("ok") is False:
                return self._with_contract({"ok": False, "error": result.get("error", "invalid_experience")})
            return self._with_contract({"ok": True, "result": result})
        if method == "experience.record_item":
            params = dict(params)
            payload = params.get("payload", {})
            scope: BridgeScope = params.get("scope", {})
            if not isinstance(payload, dict) or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            result = record_experience_item(self.runtime, payload, scope=self._resolve_scope(scope))
            if result.get("ok") is False:
                return self._with_contract({"ok": False, "error": result.get("error", "invalid_experience")})
            return self._with_contract({"ok": True, "result": result})
        if method == "experience.record_outcome_trace":
            params = dict(params)
            payload = params.get("payload", {})
            scope: BridgeScope = params.get("scope", {})
            if not isinstance(payload, dict) or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            result = self.runtime.record_outcome_trace(payload, scope=self._resolve_scope(scope))
            if result.get("ok") is False:
                return self._with_contract({"ok": False, "error": result.get("error", "invalid_experience")})
            return self._with_contract({"ok": True, "result": result})
        if method == "evolution.get_active_policy":
            params = dict(params)
            scope: BridgeScope = params.get("scope", {})
            task_type = params.get("task_type", "")
            if not isinstance(task_type, str) or not task_type.strip() or not self._valid_scope(scope):
                return self._with_contract(self._invalid_request())
            policy = self.runtime.evolution.get_active_policy(
                task_type=task_type,
                scope=self._resolve_scope(scope),
            )
            return self._with_contract({"ok": True, "result": policy})
        return self._with_contract({"ok": False, "error": "unknown_method"})

    def _handle_runtime_adapter(self, method: str, params: dict[str, Any]) -> EIMemoryRPCResponse:
        channel = params.get("channel", "")
        scope: BridgeScope = params.get("scope", {})
        if not isinstance(channel, str) or not channel.strip() or not self._valid_scope(scope):
            return self._with_contract(self._invalid_request())
        try:
            if method == "adapter.status":
                result = self.runtime_adapter.status(channel=channel, scope=scope)
            elif method == "adapter.prefetch":
                query = params.get("query", "")
                task_type = params.get("task_type", "")
                task_context = params.get("task_context", {})
                limit = params.get("limit", 8)
                if (
                    not isinstance(query, str)
                    or not query.strip()
                    or not isinstance(task_type, str)
                    or not isinstance(task_context, dict)
                    or not isinstance(limit, int)
                    or isinstance(limit, bool)
                    or limit <= 0
                ):
                    return self._with_contract(self._invalid_request())
                result = self.runtime_adapter.prefetch(
                    channel=channel,
                    scope=scope,
                    query=query,
                    task_type=task_type,
                    limit=limit,
                    task_context=task_context,
                )
            elif method == "adapter.remember":
                text = params.get("text", "")
                memory_type = params.get("memory_type", "durable_fact")
                event_id = params.get("event_id", "")
                title = params.get("title", "")
                force_capture = params.get("force_capture", False)
                meta = params.get("meta", {})
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or not isinstance(memory_type, str)
                    or not memory_type.strip()
                    or not isinstance(event_id, str)
                    or not event_id.strip()
                    or not isinstance(title, str)
                    or not isinstance(force_capture, bool)
                    or not isinstance(meta, dict)
                ):
                    return self._with_contract(self._invalid_request())
                result = self.runtime_adapter.remember(
                    channel=channel,
                    scope=scope,
                    text=text,
                    memory_type=memory_type,
                    event_id=event_id,
                    title=title,
                    force_capture=force_capture,
                    meta=meta,
                )
            elif method == "adapter.sync_turn":
                session_id = params.get("session_id", "")
                turn_id = params.get("turn_id", "")
                user_text = params.get("user_text", "")
                assistant_text = params.get("assistant_text", "")
                if not all(isinstance(value, str) for value in (session_id, turn_id, user_text, assistant_text)):
                    return self._with_contract(self._invalid_request())
                if not session_id.strip() or not turn_id.strip() or not (user_text.strip() or assistant_text.strip()):
                    return self._with_contract(self._invalid_request())
                result = self.runtime_adapter.sync_turn(
                    channel=channel,
                    scope=scope,
                    session_id=session_id,
                    turn_id=turn_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
            elif method == "adapter.record_terminal":
                end_kind = params.get("end_kind", "")
                session_id = params.get("session_id", "")
                event_id = params.get("event_id", "")
                task_type = params.get("task_type", "")
                success = params.get("success")
                verification = params.get("verification", "")
                terminal_result = params.get("result", "")
                tool_receipts = params.get("tool_receipts", [])
                rehearsal = params.get("rehearsal", False)
                if (
                    not all(isinstance(value, str) for value in (end_kind, session_id, event_id, task_type, verification, terminal_result))
                    or not end_kind.strip()
                    or not session_id.strip()
                    or not event_id.strip()
                    or not task_type.strip()
                    or (success is not None and not isinstance(success, bool))
                    or not isinstance(tool_receipts, list)
                    or not all(isinstance(item, dict) for item in tool_receipts)
                    or not isinstance(rehearsal, bool)
                ):
                    return self._with_contract(self._invalid_request())
                result = self.runtime_adapter.record_terminal(
                    channel=channel,
                    scope=scope,
                    end_kind=end_kind,
                    session_id=session_id,
                    event_id=event_id,
                    task_type=task_type,
                    success=success,
                    verification=verification,
                    result=terminal_result,
                    tool_receipts=tool_receipts,
                    rehearsal=rehearsal,
                )
            else:
                return self._with_contract({"ok": False, "error": "unknown_method"})
        except (TypeError, ValueError):
            return self._with_contract(self._invalid_request())
        return self._with_contract({"ok": bool(result.get("ok")), "result": result})

    @staticmethod
    def _resolve_scope(scope: BridgeScope, *, aliases: object = None) -> dict[str, str]:
        if scope.get("preserve_scope") is True:
            scope_ref = ScopeRef.from_dict(scope)
            return {
                "tenant_id": scope_ref.tenant_id,
                "agent_id": scope_ref.agent_id,
                "workspace_id": scope_ref.workspace_id,
                "user_id": scope_ref.user_id,
            }
        return hongtu_scope(scope, aliases=aliases)

    @classmethod
    def _filter_recall_result_to_scope(cls, result: dict, scope: dict[str, str]) -> dict:
        filtered = dict(result)
        items = filtered.get("items")
        if isinstance(items, list):
            filtered["items"] = [item for item in items if cls._item_matches_scope(item, scope)]
        recall_view = filtered.get("recall_view")
        if isinstance(recall_view, dict) and isinstance(recall_view.get("items"), list):
            recall_view = dict(recall_view)
            recall_view["items"] = [
                item for item in recall_view["items"] if cls._item_matches_scope(item, scope)
            ]
            filtered["recall_view"] = recall_view
        return filtered

    @staticmethod
    def _item_matches_scope(item: object, scope: dict[str, str]) -> bool:
        if not isinstance(item, dict):
            return False
        item_scope = item.get("scope")
        if not isinstance(item_scope, dict):
            return False
        return all(str(item_scope.get(key) or "") == str(scope.get(key) or "") for key in ("tenant_id", "agent_id", "workspace_id", "user_id"))

    def _invalid_request(self) -> EIMemoryRPCResponse:
        return {"ok": False, "error": "invalid_request"}

    @staticmethod
    def _with_contract(payload: EIMemoryRPCResponse) -> EIMemoryRPCResponse:
        return {
            "contract_version": EIMEMORY_RPC_CONTRACT_VERSION,
            **payload,
        }

    @staticmethod
    def _valid_scope(scope: object) -> bool:
        if not isinstance(scope, dict):
            return False
        return any(str(scope.get(key, "")).strip() for key in ("agent_id", "workspace_id", "tenant_id", "user_id"))

    @staticmethod
    def _valid_task_context(task_context: object) -> bool:
        if not isinstance(task_context, dict):
            return False
        task_type = task_context.get("task_type", "")
        return isinstance(task_type, str) and bool(task_type.strip())

    @staticmethod
    def _valid_tags(tags: object) -> bool:
        return isinstance(tags, list) and all(isinstance(tag, (str, int, float, bool)) for tag in tags)

    @staticmethod
    def _valid_list(value: object) -> bool:
        return isinstance(value, list)

    @staticmethod
    def _normalize_evidence(evidence: list[object]) -> list[str]:
        normalized: list[str] = []
        for item in evidence:
            if isinstance(item, str):
                normalized.append(item)
            elif isinstance(item, dict):
                normalized.append(json.dumps(item, ensure_ascii=False, sort_keys=True))
        return normalized

    @staticmethod
    def _normalize_links(links: list[object]) -> list[LinkRef]:
        normalized: list[LinkRef] = []
        for item in links:
            if not isinstance(item, dict):
                continue
            relation = str(item.get("relation") or item.get("rel") or "")
            target_kind = str(item.get("target_kind") or item.get("kind") or "")
            target_id = str(item.get("target_id") or item.get("id") or "")
            if relation and target_id:
                normalized.append(LinkRef(relation=relation, target_kind=target_kind, target_id=target_id))
        return normalized
