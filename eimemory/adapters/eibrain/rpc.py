from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_identity_meta, hongtu_scope
from eimemory.models.records import LinkRef


class EIBrainRPCBridge:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def handle(self, request: dict) -> dict:
        if not isinstance(request, dict):
            return self._invalid_request()
        method = request.get("method")
        if not isinstance(method, str):
            return self._invalid_request()
        params = request.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._invalid_request()
        if method == "memory.recall":
            limit = params.get("limit", 8)
            scope = params.get("scope", {})
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
                return self._invalid_request()
            bundle = self.runtime.memory.recall(
                query=query,
                scope=hongtu_scope(scope),
                task_context=task_context,
                limit=limit,
            )
            return {"ok": True, "result": bundle.to_dict()}
        if method == "memory.ingest":
            scope = params.get("scope", {})
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
                return self._invalid_request()
            source = str(params.get("source") or "eibrain.dialogue")
            record = self.runtime.memory.ingest(
                text=text,
                memory_type=memory_type,
                title=title,
                scope=hongtu_scope(scope),
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
            return {"ok": True, "result": record.to_dict()}
        if method == "evolution.observe":
            payload = params.get("payload", {})
            scope = params.get("scope", {})
            if (
                not isinstance(params.get("signal_type", ""), str)
                or not str(params.get("signal_type", "")).strip()
                or not isinstance(payload, dict)
                or not self._valid_scope(scope)
            ):
                return self._invalid_request()
            record = self.runtime.evolution.observe(
                signal_type=params.get("signal_type") or "",
                payload=payload,
                scope=hongtu_scope(scope),
            )
            return {"ok": True, "result": record.to_dict()}
        if method == "evolution.get_active_policy":
            scope = params.get("scope", {})
            task_type = params.get("task_type", "")
            if not isinstance(task_type, str) or not task_type.strip() or not self._valid_scope(scope):
                return self._invalid_request()
            policy = self.runtime.evolution.get_active_policy(
                task_type=task_type,
                scope=hongtu_scope(scope),
            )
            return {"ok": True, "result": policy}
        return {"ok": False, "error": "unknown_method"}

    def _invalid_request(self) -> dict:
        return {"ok": False, "error": "invalid_request"}

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
