from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecallBundle, RecordEnvelope


class EIBrainMemoryClient:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def recall_for_decision(
        self,
        *,
        query: str,
        task_type: str,
        goal: str,
        scope: dict,
        limit: int = 8,
    ) -> RecallBundle:
        return self.runtime.memory.recall(
            query=query,
            scope=scope,
            task_context={"task_type": task_type, "goal": goal},
            limit=limit,
        )

    def observe_incident(
        self,
        *,
        incident_type: str,
        severity: str,
        title: str,
        summary: str,
        scope: dict,
    ) -> RecordEnvelope:
        return self.runtime.evolution.observe(
            signal_type="incident",
            payload={
                "incident_type": incident_type,
                "severity": severity,
                "title": title,
                "summary": summary,
            },
            scope=scope,
        )
