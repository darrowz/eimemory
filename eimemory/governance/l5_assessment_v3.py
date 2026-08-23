"""Read-only-friendly L5 v3 assessment over dynamic capability evidence.

L5 v3 has four intentionally independent axes: loop maturity, capability
readiness, adapter readiness, and deployment assurance.  None is inferred from
the package version, machine fingerprint, process health, or a fixed list of
capability names.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
from typing import Any

from eimemory.capabilities.models import L5AssessmentV3
from eimemory.capabilities.projector import CapabilityStateProjector
from eimemory.capabilities.registry import MutationReceipt, exact_runtime_scope
from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


ASSESSMENT_SCHEMA = "l5.assessment.v3"
ASSESSMENT_ALGORITHM_REVISION = "l5-assessment.v3"


class L5AssessmentV3Error(ValueError):
    """An L5 v3 assessment cannot safely be constructed from current inputs."""


def build_l5_assessment_v3(
    runtime: Any,
    *,
    profile_key: str,
    scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    persist: bool = False,
    at_time: str = "",
    max_candidates: int = 100,
    observation_limit: int = 500,
) -> dict[str, Any]:
    """Build an L5 v3 report without changing v2 state or promotion behavior."""

    runtime_scope = exact_runtime_scope(scope)
    projector = CapabilityStateProjector(runtime.store)
    try:
        projection = projector.project(
            profile_key,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            at_time=at_time,
            max_candidates=max_candidates,
            observation_limit=observation_limit,
            persist=persist,
        )
    except Exception as exc:
        # Reader cutover must fail closed as structured state.  In particular,
        # an uninstalled explicit default profile is a provisioning gap, never
        # a reason to throw into a scheduler or fall back to v2 taxonomy.
        return {
            "schema": ASSESSMENT_SCHEMA,
            "ok": False,
            "status": "blocked",
            "reason": f"capability_projection_unavailable:{type(exc).__name__}",
            "profile_key": str(profile_key or ""),
            "capability_scope": capability_scope,
            "scope": {
                "tenant_id": runtime_scope.tenant_id,
                "agent_id": runtime_scope.agent_id,
                "workspace_id": runtime_scope.workspace_id,
                "user_id": runtime_scope.user_id,
            },
            "loop_maturity": "observing",
            "capability_readiness": {},
            "adapter_readiness": {"adapter_registry": "unknown"},
            "deployment_assurance": {
                "ok": None,
                "required": False,
                "blocking": False,
                "status": "not_evaluated",
            },
            "gaps": [{"reason": "capability_projection_unavailable"}],
            "persisted": False,
        }
    projection_dict = projection.to_dict()
    snapshots = list(projection.snapshots)
    adapter_axis = _adapter_readiness(runtime.store, runtime_scope, capability_scope, at_time=at_time or now_iso())
    deployment_axis = _deployment_assurance(runtime, runtime_scope, capability_scope)
    loop_axis = _loop_maturity(runtime.store, runtime_scope, capability_scope, projection_dict)
    capability_axis, gaps = _capability_readiness(projection_dict)
    if not snapshots:
        return {
            "schema": ASSESSMENT_SCHEMA,
            "ok": False,
            "status": "blocked",
            "reason": "no_evidence_backed_capability_snapshots",
            "profile": projection_dict.get("profile_id"),
            "capability_scope": capability_scope,
            "loop_maturity": loop_axis,
            "capability_readiness": capability_axis,
            "adapter_readiness": adapter_axis,
            "deployment_assurance": deployment_axis,
            "gaps": gaps,
            "projection": projection_dict,
            "persisted": False,
        }

    created_at = _assessment_created_at(snapshots)
    assessment_material = {
        "projection_digest": projection_dict["projection_digest"],
        "loop_maturity": loop_axis,
        "capability_readiness": capability_axis,
        "adapter_readiness": adapter_axis,
        "deployment_assurance": deployment_axis,
        "gaps": gaps,
    }
    assessment_id = f"l5-assessment-{_digest(assessment_material)[:40]}"
    assessment = L5AssessmentV3(
        assessment_id=assessment_id,
        profile_id=str(projection_dict["profile_id"]),
        loop_maturity=loop_axis,
        capability_snapshot_ids=[str(item["snapshot_id"]) for item in snapshots],
        capability_readiness=capability_axis,
        adapter_readiness=adapter_axis,
        deployment_assurance=deployment_axis,
        evidence_refs=[str(item["snapshot_id"]) for item in snapshots],
        created_at=created_at,
        scope=capability_scope,
        algorithm_revision=ASSESSMENT_ALGORITHM_REVISION,
        input_watermarks={
            "projection_digest": projection_dict["projection_digest"],
            "projection_watermark": projection_dict["input_watermark"],
            "adapter_digest": _digest(adapter_axis),
            "deployment_digest": _digest(deployment_axis),
        },
    )
    receipt: MutationReceipt | None = None
    if persist:
        stored = runtime.store.mutate_capabilities_atomically(
            lambda repository: repository.register_assessment(
                assessment,
                scope=runtime_scope,
                request_key=f"l5-assessment:{assessment.assessment_id}",
            )
        )
        receipt = MutationReceipt.from_stored(stored)
    return {
        "schema": ASSESSMENT_SCHEMA,
        "ok": not gaps and projection_dict.get("ok") is True,
        "status": "ready" if not gaps and projection_dict.get("ok") is True else "degraded",
        "assessment": assessment.to_dict(),
        "assessment_id": assessment.assessment_id,
        "assessment_digest": assessment.assessment_digest,
        "loop_maturity": loop_axis,
        "capability_readiness": capability_axis,
        "adapter_readiness": adapter_axis,
        "deployment_assurance": deployment_axis,
        "gaps": gaps,
        "projection": projection_dict,
        "persisted": receipt.to_dict() if receipt is not None else None,
    }


def _capability_readiness(projection: Mapping[str, Any]) -> tuple[dict[str, dict[str, dict[str, Any]]], list[dict[str, Any]]]:
    readiness: dict[str, dict[str, dict[str, Any]]] = {}
    for snapshot in projection.get("snapshots") or ():
        if not isinstance(snapshot, Mapping):
            continue
        revision_id = str(snapshot.get("capability_revision_id") or "")
        binding_id = str(snapshot.get("provider_binding_id") or "")
        if not revision_id or not binding_id:
            continue
        readiness.setdefault(revision_id, {})[binding_id] = {
            "maturity": str(snapshot.get("maturity") or "unknown"),
            "snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "capability_id": str(snapshot.get("capability_id") or ""),
            "confidence": float(snapshot.get("confidence") or 0.0),
            "reason_codes": list(snapshot.get("reason_codes") or ()),
        }
    gaps = [dict(item) for item in projection.get("blocked") or () if isinstance(item, Mapping)]
    return readiness, gaps


def _adapter_readiness(
    store: RuntimeStore,
    scope: ScopeRef,
    capability_scope: str,
    *,
    at_time: str = "",
) -> dict[str, str]:
    """Summarize advertised adapters dynamically; no known-adapter list exists."""

    def reader(repository):
        method = getattr(repository, "list_adapter_advertisements", None)
        if not callable(method):
            return []
        checked_at = str(at_time or now_iso())
        return method(
            scope=scope,
            capability_scope=capability_scope,
            at_time=checked_at,
            fresh_at=checked_at,
            limit=500,
        )

    advertisements = store.read_capabilities(reader)
    if not advertisements:
        return {"adapter_registry": "unknown"}
    states: dict[str, str] = {}
    for advertisement in advertisements:
        payload = getattr(advertisement, "payload", None)
        status = str(getattr(advertisement, "status", "") or "")
        if not isinstance(payload, Mapping):
            continue
        adapter_id = str(payload.get("adapter_id") or "")
        if not adapter_id:
            continue
        advertised_status = str(payload.get("status") or status or "unknown")
        if status != "active" or advertised_status not in {"active", "ready"}:
            states[adapter_id] = "degraded"
        else:
            states[adapter_id] = "ready"
    return states or {"adapter_registry": "unknown"}


def _deployment_assurance(
    runtime: Any,
    scope: ScopeRef,
    capability_scope: str,
) -> dict[str, Any]:
    """Use only declared deployment-dependent observations as release inputs."""

    from eimemory.governance.capability_release_evidence import (
        build_capability_deployment_assurance,
    )

    # Deployment evidence remains an independent axis.  Package version and
    # host data can be reported descriptively by the release subsystem but
    # never determine this result.
    return build_capability_deployment_assurance(
        runtime,
        scope=scope,
        capability_scope=capability_scope,
    )


def _loop_maturity(
    store: RuntimeStore,
    scope: ScopeRef,
    capability_scope: str,
    projection: Mapping[str, Any],
) -> str:
    """Derive loop stage from linked evidence, never process/runtime identity.

    The stage is intentionally conservative: a profile gap means the system is
    diagnosing; merely collecting evaluations is experimenting; evolving needs
    both a successful terminal evaluation and a current knowledge link; and
    compounding additionally needs more than one independently evidenced
    capability state.  None of these conditions names a particular capability,
    provider, host, package version, or machine.
    """

    if projection.get("blocked"):
        return "diagnosing"
    snapshots = [item for item in projection.get("snapshots") or () if isinstance(item, Mapping)]
    if not snapshots:
        return "observing"
    evaluation_runs = store.read_capabilities(
        lambda repository: repository.list_evaluation_runs(
            scope=scope,
            capability_scope=capability_scope,
            limit=500,
        )
    )
    if not evaluation_runs:
        return "observing"
    passing_targets = {
        (
            str((row.get("payload") or {}).get("capability_revision_id") or ""),
            str((row.get("payload") or {}).get("provider_binding_id") or ""),
        )
        for row in evaluation_runs
        if isinstance(row, Mapping)
        and str((row.get("payload") or {}).get("verdict") or "").lower() == "pass"
    }
    if not passing_targets:
        return "experimenting"
    linked_revisions = {
        str((row.get("payload") or {}).get("capability_revision_id") or "")
        for row in store.read_capabilities(
            lambda repository: repository.list_knowledge_links(
                scope=scope,
                capability_scope=capability_scope,
                limit=500,
            )
        )
        if isinstance(row, Mapping)
    }
    evidenced_snapshots = [
        item
        for item in snapshots
        if (
            str(item.get("capability_revision_id") or ""),
            str(item.get("provider_binding_id") or ""),
        )
        in passing_targets
        and str(item.get("capability_revision_id") or "") in linked_revisions
    ]
    if not evidenced_snapshots:
        return "experimenting"
    distinct_capabilities = {
        str(item.get("capability_id") or "")
        for item in evidenced_snapshots
        if str(item.get("capability_id") or "")
    }
    return "compounding" if len(distinct_capabilities) >= 2 else "evolving"


def _assessment_created_at(snapshots: list[Mapping[str, Any]]) -> str:
    timestamps = [str(snapshot.get("computed_at") or "") for snapshot in snapshots]
    timestamps = [value for value in timestamps if value]
    if not timestamps:
        raise L5AssessmentV3Error("evidence-backed snapshots must expose computed_at")
    # A repeat over identical immutable snapshots must recreate an identical
    # assessment descriptor.  Wall-clock time is deliberately not an input.
    return max(timestamps)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "ASSESSMENT_ALGORITHM_REVISION",
    "ASSESSMENT_SCHEMA",
    "L5AssessmentV3Error",
    "build_l5_assessment_v3",
]
