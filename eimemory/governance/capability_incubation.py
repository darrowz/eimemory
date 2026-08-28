"""Evidence-gated incubation and revalidation for dynamic capabilities.

The ordinary L5 profile intentionally selects only active definitions.  This
module closes the bootstrap gap without weakening that profile: it inspects
*discovered* definitions and active definitions targeted by a newly bound
catalog case through exact-scope registry APIs, requires an active revision,
an exact provider binding, a fresh adapter advertisement, and trusted sealed
catalog cases, then executes bounded preflight probes before performing the
lifecycle transition to ``active``. Existing profile acceptance and projection
own all maturity after activation or revalidation.

No text classifier, LLM output, adapter name, package version, or knowledge
volume can activate a capability here.  Missing prerequisites remain explicit
incubation work items.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any

from eimemory.capabilities.registry import exact_runtime_scope
from eimemory.core.clock import now_iso
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogCase,
    CatalogResolutionError,
    resolve_application_capability_catalog,
)
from eimemory.models.records import RecordEnvelope, ScopeRef


CAPABILITY_INCUBATION_SCHEMA = "capability.incubation.v1"
_MAX_DISCOVERED = 499
_MAX_ACTIVATE = 20
_MAX_PREFLIGHT_PASSES = 5
_READY_STATUSES = frozenset({"ready_for_preflight", "ready_for_revalidation"})


class CapabilityIncubationError(ValueError):
    """An incubation request is unsafe, unbounded, or internally inconsistent."""


def build_capability_incubation_plan(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    catalog: CapabilityEvaluationCatalog | None = None,
    max_candidates: int = 100,
    fresh_at: str = "",
) -> dict[str, Any]:
    """Return a bounded plan for discovery activation and provider revalidation."""

    scope = exact_runtime_scope(runtime_scope)
    limit = _bounded_int(max_candidates, minimum=1, maximum=_MAX_DISCOVERED, field="max_candidates")
    try:
        active_catalog = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError as exc:
        raise CapabilityIncubationError("evaluation_catalog_untrusted") from exc
    discovered_definitions = runtime.capabilities.list_definitions(
        runtime_scope=scope,
        capability_scope=capability_scope,
        status="discovered",
        limit=limit + 1,
    )
    if len(discovered_definitions) > limit:
        raise CapabilityIncubationError("discovered capability count exceeds max_candidates; refusing truncation")
    catalog_capability_ids = {
        case.capability_id for case in active_catalog.list_cases()
    }
    active_definitions = [
        definition
        for definition in runtime.capabilities.list_definitions(
            runtime_scope=scope,
            capability_scope=capability_scope,
            status="active",
            limit=_MAX_DISCOVERED + 1,
        )
        if str(definition.get("entity_id") or "") in catalog_capability_ids
    ]
    if len(active_definitions) > limit:
        raise CapabilityIncubationError("active revalidation candidates exceed max_candidates; refusing truncation")
    checked_at = fresh_at or now_iso()
    discovered_items = [
        _incubation_item(
            runtime,
            definition=definition,
            definition_status="discovered",
            scope=scope,
            capability_scope=capability_scope,
            catalog=active_catalog,
            fresh_at=checked_at,
        )
        for definition in discovered_definitions
    ]
    active_items = [
        item
        for definition in active_definitions
        if (
            item := _incubation_item(
                runtime,
                definition=definition,
                definition_status="active",
                scope=scope,
                capability_scope=capability_scope,
                catalog=active_catalog,
                fresh_at=checked_at,
            )
        )["status"] != "current"
    ]
    items = [*discovered_items, *active_items]
    if len(items) > limit:
        raise CapabilityIncubationError("capability incubation candidates exceed max_candidates; refusing truncation")
    items.sort(key=lambda item: str(item.get("capability_id") or ""))
    material = {
        "schema": CAPABILITY_INCUBATION_SCHEMA,
        "ok": True,
        "status": "ready" if any(item["status"] in _READY_STATUSES for item in items) else "waiting",
        "runtime_scope": asdict(scope),
        "capability_scope": capability_scope,
        "checked_at": checked_at,
        "discovered_count": len(discovered_items),
        "revalidation_count": len(active_items),
        "ready_count": sum(item["status"] in _READY_STATUSES for item in items),
        "blocked_count": sum(item["status"] == "blocked" for item in items),
        "work_items": items,
    }
    return {**material, "plan_digest": _digest(material)}


def execute_capability_incubation(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    catalog: CapabilityEvaluationCatalog | None = None,
    max_candidates: int = 100,
    max_activate: int = 3,
    preflight_passes: int = 2,
    persist_report: bool = True,
) -> dict[str, Any]:
    """Preflight bounded definitions, activate or revalidate, then run acceptance."""

    scope = exact_runtime_scope(runtime_scope)
    activation_budget = _bounded_int(max_activate, minimum=0, maximum=_MAX_ACTIVATE, field="max_activate")
    required_passes = _bounded_int(
        preflight_passes,
        minimum=1,
        maximum=_MAX_PREFLIGHT_PASSES,
        field="preflight_passes",
    )
    try:
        active_catalog = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError as exc:
        raise CapabilityIncubationError("evaluation_catalog_untrusted") from exc
    plan = build_capability_incubation_plan(
        runtime,
        runtime_scope=scope,
        capability_scope=capability_scope,
        catalog=active_catalog,
        max_candidates=max_candidates,
    )
    results: list[dict[str, Any]] = []
    activated = 0
    revalidated = 0
    for item in plan["work_items"]:
        if item["status"] not in _READY_STATUSES:
            results.append({**item, "result": "waiting"})
            continue
        if activated >= activation_budget:
            results.append({**item, "result": "budget_exhausted"})
            continue
        preflight = _run_preflight(
            runtime,
            catalog=active_catalog,
            case_ids=item["case_ids"],
            required_passes=required_passes,
        )
        if preflight["ok"] is not True:
            results.append({**item, "result": "preflight_failed", "preflight": preflight})
            continue
        revalidation = item["status"] == "ready_for_revalidation"
        transition = runtime.capabilities.transition_status(
            entity_type="definition",
            entity_id=item["capability_id"],
            entity_digest=item["definition_digest"],
            target_status="active",
            runtime_scope=scope,
            capability_scope=capability_scope,
            expected_state_version=int(item["state_version"]),
            expected_state_digest=item["state_digest"],
            effective_at=now_iso(),
            reason=(
                "trusted catalog, current provider binding, fresh advertisement, and bounded revalidation passed"
                if revalidation
                else "trusted catalog, provider binding, fresh advertisement, and bounded preflight passed"
            ),
            provenance={
                "source": "eimemory.capability_incubation",
                "schema": CAPABILITY_INCUBATION_SCHEMA,
                "plan_digest": plan["plan_digest"],
                "case_ids": list(item["case_ids"]),
                "binding_ids": list(item["binding_ids"]),
                "preflight_passes": required_passes,
                "preflight_execution_digests": [
                    str(pass_row.get("execution_digest") or "")
                    for result in preflight["results"]
                    for pass_row in result["passes"]
                ],
                "provider_evaluation_receipts": [
                    dict(pass_row["provider_evaluation_receipt"])
                    for result in preflight["results"]
                    for pass_row in result["passes"]
                    if isinstance(pass_row.get("provider_evaluation_receipt"), Mapping)
                ],
                "provider_evaluation_receipt_digests": [
                    str(pass_row.get("provider_evaluation_receipt_digest") or "")
                    for result in preflight["results"]
                    for pass_row in result["passes"]
                    if str(pass_row.get("provider_evaluation_receipt_digest") or "")
                ],
            },
            request_key=f"capability-incubation:activate:{item['capability_id']}:{item['definition_digest']}",
        )
        acceptance = runtime.run_capability_acceptance(
            scope=asdict(scope),
            persist=True,
            case_ids=list(item["case_ids"]),
            catalog=active_catalog,
            profile_key="l5.default",
            capability_scope=capability_scope,
            runtime_scope=scope,
        )
        if acceptance.get("all_passed") is not True:
            _quarantine_after_failed_acceptance(
                runtime,
                scope=scope,
                capability_scope=capability_scope,
                capability_id=item["capability_id"],
                reason="post_activation_acceptance_failed",
            )
            results.append(
                {
                    **item,
                    "result": "quarantined",
                    "transition": transition.to_dict(),
                    "preflight": preflight,
                    "acceptance": acceptance,
                }
            )
            continue
        activated += 1
        if revalidation:
            revalidated += 1
        results.append(
            {
                **item,
                "result": "revalidated" if revalidation else "activated",
                "transition": transition.to_dict(),
                "preflight": preflight,
                "acceptance": {
                    "ok": acceptance.get("ok"),
                    "execution_id": acceptance.get("execution_id"),
                    "case_count": acceptance.get("case_count"),
                    "pass_count": acceptance.get("pass_count"),
                    "failed_count": acceptance.get("failed_count"),
                },
            }
        )
    report = {
        "schema": CAPABILITY_INCUBATION_SCHEMA,
        "ok": all(result.get("result") not in {"preflight_failed", "quarantined"} for result in results),
        "status": "revalidated" if revalidated and revalidated == activated else "activated" if activated else "waiting",
        "runtime_scope": asdict(scope),
        "capability_scope": capability_scope,
        "plan_digest": plan["plan_digest"],
        "discovered_count": plan["discovered_count"],
        "ready_count": plan["ready_count"],
        "activated_count": activated,
        "revalidated_count": revalidated,
        "results": results,
    }
    if persist_report:
        record = _persist_report(runtime, scope=scope, report=report)
        report["report_record_id"] = record.record_id
    return report


def _incubation_item(
    runtime: Any,
    *,
    definition: Mapping[str, Any],
    definition_status: str,
    scope: ScopeRef,
    capability_scope: str,
    catalog: CapabilityEvaluationCatalog,
    fresh_at: str,
) -> dict[str, Any]:
    capability_id = str(definition.get("entity_id") or "")
    context = runtime.capabilities.incubation_context(
        capability_id,
        runtime_scope=scope,
        capability_scope=capability_scope,
        limit=100,
    )
    revisions = [row for row in context.get("revisions") or [] if row.get("status") == "active"]
    bindings = [row for row in context.get("bindings") or [] if row.get("status") == "active"]
    cases = catalog.list_cases(capability_id=capability_id)
    reasons: list[str] = []
    if not revisions:
        reasons.append("active_revision_missing")
    if not bindings:
        reasons.append("active_provider_binding_missing")
    if not cases:
        reasons.append("trusted_catalog_case_missing")
    matched_case_ids: list[str] = []
    matched_binding_ids: list[str] = []
    stale_binding_ids: list[str] = []
    revision_ids = {str(row.get("entity_id") or "") for row in revisions}
    for binding in bindings:
        descriptor = binding.get("descriptor") if isinstance(binding.get("descriptor"), Mapping) else {}
        binding_id = str(binding.get("entity_id") or "")
        if str(descriptor.get("capability_revision_id") or "") not in revision_ids:
            continue
        selected_cases = [case for case in cases if _case_selects_binding(case, binding)]
        if not selected_cases:
            continue
        advertisements = runtime.capabilities.list_adapter_advertisements(
            runtime_scope=scope,
            capability_scope=capability_scope,
            binding_id=binding_id,
            status="active",
            fresh_at=fresh_at,
            limit=10,
        )
        if not any((row.get("freshness") or {}).get("is_fresh") is True for row in advertisements):
            stale_binding_ids.append(binding_id)
            continue
        matched_binding_ids.append(binding_id)
        matched_case_ids.extend(case.case_id for case in selected_cases)
    if bindings and cases and not matched_case_ids:
        reasons.append("fresh_advertised_catalog_target_missing")
    material = {
        "capability_id": capability_id,
        "definition_status": definition_status,
        "definition_digest": str(definition.get("entity_digest") or ""),
        "state_version": int(definition.get("state_version") or 0),
        "state_digest": str(definition.get("state_digest") or ""),
        "revision_ids": sorted(revision_ids),
        "binding_ids": sorted(set(matched_binding_ids)),
        "case_ids": sorted(set(matched_case_ids)),
        "stale_binding_ids": sorted(set(stale_binding_ids)),
        "reasons": sorted(set(reasons)),
    }
    if reasons:
        status = "blocked"
    elif definition_status == "active":
        status = (
            "current"
            if _has_current_catalog_activation(
                runtime,
                capability_id=capability_id,
                scope=scope,
                capability_scope=capability_scope,
                binding_ids=material["binding_ids"],
                case_ids=material["case_ids"],
            )
            else "ready_for_revalidation"
        )
    else:
        status = "ready_for_preflight"
    return {
        **material,
        "work_item_id": _digest({"schema": CAPABILITY_INCUBATION_SCHEMA, **material})[:40],
        "status": status,
    }


def _has_current_catalog_activation(
    runtime: Any,
    *,
    capability_id: str,
    scope: ScopeRef,
    capability_scope: str,
    binding_ids: Sequence[str],
    case_ids: Sequence[str],
) -> bool:
    if not binding_ids or not case_ids:
        return False
    if capability_id == "code.implementation":
        from eimemory.adapters.hermes.code_implementation import (
            code_implementation_catalog_activation_snapshot,
        )

        return code_implementation_catalog_activation_snapshot(
            runtime.capabilities,
            runtime_scope=scope,
            capability_scope=capability_scope,
        ) is not None
    try:
        events = runtime.capabilities.list_lifecycle_events(
            entity_type="definition",
            entity_id=capability_id,
            runtime_scope=scope,
            capability_scope=capability_scope,
            limit=100,
        )
    except (RuntimeError, TypeError, ValueError):
        return False
    expected_bindings = set(binding_ids)
    expected_cases = set(case_ids)
    for event in reversed(events):
        if not isinstance(event, Mapping) or event.get("status") != "active":
            continue
        provenance = event.get("provenance") if isinstance(event.get("provenance"), Mapping) else {}
        try:
            passes = int(provenance.get("preflight_passes") or 0)
        except (TypeError, ValueError):
            continue
        if (
            provenance.get("source") == "eimemory.capability_incubation"
            and provenance.get("schema") == CAPABILITY_INCUBATION_SCHEMA
            and passes >= 2
            and set(str(value) for value in provenance.get("binding_ids") or ()) == expected_bindings
            and set(str(value) for value in provenance.get("case_ids") or ()) == expected_cases
        ):
            return True
    return False


def _case_selects_binding(case: CatalogCase, binding: Mapping[str, Any]) -> bool:
    raw_descriptor = binding.get("descriptor")
    descriptor: Mapping[str, Any] = raw_descriptor if isinstance(raw_descriptor, Mapping) else {}
    binding_id = str(binding.get("entity_id") or "")
    selector = dict(case.binding_selector)
    if not selector:
        return True
    if selector.get("binding_ids") and binding_id not in set(selector["binding_ids"]):
        return False
    if selector.get("provider_kind") and str(descriptor.get("provider_kind") or "") != selector["provider_kind"]:
        return False
    if selector.get("provider_instance_id") and str(descriptor.get("provider_instance_id") or "") != selector["provider_instance_id"]:
        return False
    if selector.get("operations_all") and not set(selector["operations_all"]).issubset(
        set(str(value) for value in descriptor.get("operations") or ())
    ):
        return False
    return True


def _run_preflight(
    runtime: Any,
    *,
    catalog: CapabilityEvaluationCatalog,
    case_ids: Sequence[str],
    required_passes: int,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for case_id in case_ids:
        case = catalog.get_case(str(case_id))
        if case is None:
            return {"ok": False, "reason": "catalog_case_disappeared", "results": results}
        passes = []
        for index in range(required_passes):
            execution = catalog.execute(
                case.to_artifact(),
                runtime=runtime,
                evidence_ref=f"incubation-preflight:{case.case_id}:{index + 1}",
            )
            output = execution.get("output") if isinstance(execution.get("output"), Mapping) else {}
            passed = execution.get("passed") is True
            provider_receipt: dict[str, Any] | None = None
            provider_receipt_digest = ""
            if case.case_id == "hongtu_code_implementation_v2":
                from eimemory.adapters.hermes.code_implementation import CodeImplementationError
                from eimemory.evaluation.hongtu_code_implementation import (
                    validate_code_implementation_catalog_receipt,
                )

                try:
                    provider_receipt_digest = str(output.get("receipt_digest") or "")
                    provider_receipt = validate_code_implementation_catalog_receipt(
                        output.get("receipt"),
                        receipt_digest=provider_receipt_digest,
                    )
                except (CodeImplementationError, TypeError, ValueError):
                    passed = False
            passes.append(
                {
                    "pass_index": index + 1,
                    "passed": passed,
                    "verdict": str(execution.get("verdict") or ""),
                    "execution_digest": str(execution.get("execution_digest") or ""),
                    "provider_evaluation_receipt": provider_receipt,
                    "provider_evaluation_receipt_digest": provider_receipt_digest,
                    "error": str(execution.get("error") or ""),
                }
            )
        results.append({"case_id": case.case_id, "passes": passes})
    ok = bool(results) and all(pass_row["passed"] for result in results for pass_row in result["passes"])
    return {"ok": ok, "required_passes": required_passes, "results": results}


def _quarantine_after_failed_acceptance(
    runtime: Any,
    *,
    scope: ScopeRef,
    capability_scope: str,
    capability_id: str,
    reason: str,
) -> None:
    current = next(
        (
            row
            for row in runtime.capabilities.list_definitions(
                runtime_scope=scope,
                capability_scope=capability_scope,
                status=None,
                limit=_MAX_DISCOVERED,
            )
            if row.get("entity_id") == capability_id
        ),
        None,
    )
    if not isinstance(current, Mapping) or current.get("status") != "active":
        return
    runtime.capabilities.transition_status(
        entity_type="definition",
        entity_id=capability_id,
        entity_digest=str(current.get("entity_digest") or ""),
        target_status="quarantined",
        runtime_scope=scope,
        capability_scope=capability_scope,
        expected_state_version=int(current.get("state_version") or 0),
        expected_state_digest=str(current.get("state_digest") or ""),
        effective_at=now_iso(),
        reason=reason,
        provenance={"source": "eimemory.capability_incubation", "schema": CAPABILITY_INCUBATION_SCHEMA},
        request_key=f"capability-incubation:quarantine:{capability_id}:{current.get('state_digest')}",
    )


def _persist_report(runtime: Any, *, scope: ScopeRef, report: Mapping[str, Any]) -> RecordEnvelope:
    digest = _digest(report)
    existing = runtime.store.list_records_by_meta_value(
        kinds=["reflection"],
        scope=scope,
        meta_key="idempotency_key",
        meta_value=f"capability-incubation-report:{digest}",
        limit=1,
    )
    if existing:
        return existing[0]
    return runtime.store.append(
        RecordEnvelope.create(
            kind="reflection",
            title="Dynamic capability incubation",
            summary=f"{report.get('activated_count', 0)} activated; {report.get('discovered_count', 0)} discovered",
            detail="Evidence-gated discovered capability incubation report.",
            scope=scope,
            source="eimemory.capability_incubation",
            status="active",
            content=dict(report),
            meta={
                "report_type": "capability_incubation",
                "schema_version": CAPABILITY_INCUBATION_SCHEMA,
                "idempotency_key": f"capability-incubation-report:{digest}",
                "report_digest": digest,
            },
        )
    )


def _bounded_int(value: object, *, minimum: int, maximum: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CapabilityIncubationError(f"{field} must be from {minimum} to {maximum}")
    return value


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "CAPABILITY_INCUBATION_SCHEMA",
    "CapabilityIncubationError",
    "build_capability_incubation_plan",
    "execute_capability_incubation",
]
