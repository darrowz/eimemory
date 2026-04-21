from __future__ import annotations

from eimemory.api.runtime import Runtime


class EIBrainRPCBridge:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def handle(self, request: dict) -> dict:
        method = str(request.get("method") or "")
        params = dict(request.get("params") or {})
        if method == "memory.recall":
            bundle = self.runtime.memory.recall(
                query=str(params.get("query") or ""),
                scope=dict(params.get("scope") or {}),
                task_context=dict(params.get("task_context") or {}),
                limit=int(params.get("limit", 8)),
            )
            return {"ok": True, "result": bundle.to_dict()}
        if method == "evolution.observe":
            record = self.runtime.evolution.observe(
                signal_type=str(params.get("signal_type") or ""),
                payload=dict(params.get("payload") or {}),
                scope=dict(params.get("scope") or {}),
            )
            return {"ok": True, "result": record.to_dict()}
        if method == "evolution.get_active_policy":
            policy = self.runtime.evolution.get_active_policy(
                task_type=str(params.get("task_type") or ""),
                scope=dict(params.get("scope") or {}),
            )
            return {"ok": True, "result": policy}
        return {"ok": False, "error": f"unknown method: {method}"}
