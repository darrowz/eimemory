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
            if (
                not isinstance(params.get("query", ""), str)
                or not isinstance(limit, int)
                or isinstance(limit, bool)
                or not isinstance(scope, dict)
                or not isinstance(task_context, dict)
            ):
                return self._invalid_request()
            bundle = self.runtime.memory.recall(
                query=params.get("query") or "",
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
                or not isinstance(payload, dict)
                or not isinstance(scope, dict)
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
            if not isinstance(params.get("task_type", ""), str) or not isinstance(scope, dict):
                return self._invalid_request()
            policy = self.runtime.evolution.get_active_policy(
                task_type=params.get("task_type") or "",
                scope=scope,
            )
            return {"ok": True, "result": policy}
        return {"ok": False, "error": f"unknown method: {method}"}

    def _invalid_request(self) -> dict:
        return {"ok": False, "error": "invalid_request"}
