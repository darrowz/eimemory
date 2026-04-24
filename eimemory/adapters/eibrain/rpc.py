from __future__ import annotations

from eimemory.api.runtime import Runtime


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
                scope=scope,
                task_context=task_context,
                limit=limit,
            )
            return {"ok": True, "result": bundle.to_dict()}
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
                scope=scope,
            )
            return {"ok": True, "result": record.to_dict()}
        if method == "evolution.get_active_policy":
            scope = params.get("scope", {})
            task_type = params.get("task_type", "")
            if not isinstance(task_type, str) or not task_type.strip() or not self._valid_scope(scope):
                return self._invalid_request()
            policy = self.runtime.evolution.get_active_policy(
                task_type=task_type,
                scope=scope,
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
