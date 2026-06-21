from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.experience import record_experience_item, record_skill_trace
from eimemory.identity import extract_user_aliases, hongtu_identity_meta, hongtu_scope
from eimemory.models.records import LinkRef, ScopeRef
from eimemory.ei_bridge.protocol import (
    EIMEMORY_RPC_CONTRACT_VERSION,
    BridgeScope,
    EIMemoryRPCRequest,
    EIMemoryRPCResponse,
)


class EIBrainRPCBridge:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

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
        if method == "memory.recall":
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
        if method == "memory.ingest":
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
            return self._with_contract({"ok": True, "result": record.to_dict()})
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
