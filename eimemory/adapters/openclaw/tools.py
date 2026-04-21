from __future__ import annotations

from eimemory.api.runtime import Runtime


class OpenClawMemoryTools:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def memory_search(self, *, query: str, scope: dict, limit: int = 5) -> dict:
        bundle = self.runtime.memory.recall(
            query=query,
            scope=scope,
            task_context={"task_type": "openclaw.tool.search"},
            limit=limit,
        )
        return {"ok": True, "items": bundle.to_dict()["items"], "meta": {"confidence": bundle.confidence}}

    def memory_store(self, *, text: str, title: str, scope: dict, memory_type: str = "fact") -> dict:
        record = self.runtime.memory.ingest(
            text=text,
            memory_type=memory_type,
            title=title,
            scope=scope,
            source="openclaw.tool.store",
        )
        return {"ok": True, "record": record.to_dict()}

    def memory_explain(self, *, query: str, task_context: dict, scope: dict, limit: int = 5) -> dict:
        bundle = self.runtime.memory.recall(
            query=query,
            scope=scope,
            task_context=task_context,
            limit=limit,
        )
        return {"ok": True, "explanation": bundle.explanation, "items": bundle.to_dict()["items"]}

    def memory_feedback(self, *, target_id: str, decision: str, reason: str, scope: dict) -> dict:
        record = self.runtime.evolution.feedback(
            target_ref={"kind": "memory", "record_id": target_id},
            decision=decision,
            reason=reason,
            reviewed_by="openclaw.tool.feedback",
            scope=scope,
        )
        return {"ok": True, "record": record.to_dict()}
