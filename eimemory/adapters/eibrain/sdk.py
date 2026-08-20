from __future__ import annotations

from typing import Any, Mapping

from eimemory.adapters.runtime.capability import AdapterCapabilityService
from eimemory.api.runtime import Runtime
from eimemory.identity import hongtu_scope
from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef
from eimemory.ei_bridge.protocol import BridgeScope


class EIBrainMemoryClient:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def recall_for_decision(
        self,
        *,
        query: str,
        task_type: str,
        goal: str,
        scope: BridgeScope,
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
        scope: BridgeScope,
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

    def advertise_capabilities(
        self,
        *,
        adapter_context: Mapping[str, Any],
        scope: BridgeScope,
        now: str = "",
    ) -> dict[str, Any]:
        """Advertise eibrain RPC support independently from local modules."""

        return AdapterCapabilityService(
            self.runtime,
            adapter_id="eibrain",
            provider_kind="eibrain",
        ).advertise_capabilities(
            adapter_context,
            runtime_scope=self._capability_runtime_scope(scope),
            now=now,
        )

    def capability_health(
        self,
        *,
        binding_id: str,
        scope: BridgeScope,
        capability_scope: str = "global",
        at_time: str = "",
    ) -> dict[str, Any]:
        """Return internal eibrain advertisement health for a binding."""

        return AdapterCapabilityService(
            self.runtime,
            adapter_id="eibrain",
            provider_kind="eibrain",
        ).capability_health(
            binding_id,
            runtime_scope=self._capability_runtime_scope(scope),
            capability_scope=capability_scope,
            at_time=at_time,
        )

    def normalize_capability_outcome(
        self,
        *,
        host_event: Mapping[str, Any] | None,
        event_type: str,
        scope: BridgeScope,
        capability_scope: str = "global",
    ) -> dict[str, Any]:
        """Normalize a declared eibrain RPC outcome without capability guessing."""

        return AdapterCapabilityService(
            self.runtime,
            adapter_id="eibrain",
            provider_kind="eibrain",
        ).normalize_capability_outcome(
            host_event,
            runtime_scope=self._capability_runtime_scope(scope),
            event_type=event_type,
            capability_scope=capability_scope,
        )

    def record_verified_capability_outcome(
        self,
        *,
        host_event: Mapping[str, Any] | None,
        event_type: str,
        scope: BridgeScope,
        independent_verifier: Mapping[str, Any],
        environment_fingerprint: Mapping[str, Any],
        provenance: Mapping[str, Any],
        capability_scope: str = "global",
    ) -> dict[str, Any]:
        """Persist an eibrain outcome only from a trusted in-process SDK host.

        The public eibrain RPC may normalize diagnostics but deliberately does
        not expose this write path: a remote bearer must not self-assert the
        independent verifier required for L5 evidence.
        """

        return AdapterCapabilityService(
            self.runtime,
            adapter_id="eibrain",
            provider_kind="eibrain",
        ).record_verified_capability_outcome(
            host_event,
            runtime_scope=self._capability_runtime_scope(scope),
            event_type=event_type,
            independent_verifier=(
                dict(independent_verifier) if isinstance(independent_verifier, Mapping) else {}
            ),
            environment_fingerprint=(
                dict(environment_fingerprint) if isinstance(environment_fingerprint, Mapping) else {}
            ),
            provenance=dict(provenance) if isinstance(provenance, Mapping) else {},
            capability_scope=capability_scope,
        )

    @staticmethod
    def _capability_runtime_scope(scope: BridgeScope) -> dict[str, str]:
        if isinstance(scope, Mapping) and scope.get("preserve_scope") is True:
            resolved = ScopeRef.from_dict(scope)
            return {
                "tenant_id": resolved.tenant_id,
                "agent_id": resolved.agent_id,
                "workspace_id": resolved.workspace_id,
                "user_id": resolved.user_id,
            }
        return hongtu_scope(scope)
