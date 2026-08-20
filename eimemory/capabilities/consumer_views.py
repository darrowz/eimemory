"""Dynamic capability views for secondary, non-authoritative consumers.

Goal queues, dashboards, and self-model rendering need a common way to read
the capability control plane without recreating a fixed taxonomy.  This module
only selects bounded descriptors and declarative planning policy; it neither
projects maturity nor grants execution authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from eimemory.capabilities.contracts import CapabilityContractError, normalize_capability_id
from eimemory.capabilities.registry import exact_runtime_scope
from eimemory.evaluation.capability_catalog import (
    CapabilityEvaluationCatalog,
    CatalogResolutionError,
    resolve_application_capability_catalog,
)
from eimemory.models.records import ScopeRef


CONSUMER_VIEW_SCHEMA = "capability.consumer_view.v1"
DYNAMIC_EVALUATION_VIEW_SCHEMA = "capability.consumer_evaluation_view.v1"
EXPLICIT_ATTRIBUTION_SCHEMA = "capability.consumer_attribution.v1"
_MAX_CAPABILITIES = 499
_MAX_EVALUATION_CASES = 256


class CapabilityConsumerViewError(ValueError):
    """A bounded dynamic consumer view could not be resolved safely."""


def dynamic_capability_views(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    profile_key: str = "",
    at_time: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    """Return a bounded set of active capability descriptors.

    With a profile key, the returned set is its frozen expansion and includes
    the rule's ``planning_policy``.  Without one, this is simply the active
    registry set.  Empty is a truthful result: no hidden legacy default list
    is substituted.
    """

    runtime_scope = exact_runtime_scope(scope)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= _MAX_CAPABILITIES:
        raise CapabilityConsumerViewError(f"limit must be an integer from 1 to {_MAX_CAPABILITIES}")
    service = getattr(runtime, "capabilities", None)
    if service is None:
        raise CapabilityConsumerViewError("runtime capability service is unavailable")

    normalized_profile_key = str(profile_key or "").strip()
    if normalized_profile_key:
        resolution = service.resolve_profile(
            normalized_profile_key,
            runtime_scope=runtime_scope,
            capability_scope=capability_scope,
            at_time=at_time,
            max_candidates=limit,
        )
        entries: list[dict[str, Any]] = []
        for item in resolution.get("requirements") or ():
            if not isinstance(item, Mapping):
                continue
            entry = _profile_entry(item)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda item: str(item["capability_id"]))
        return {
            "schema": CONSUMER_VIEW_SCHEMA,
            "source": "profile_resolution",
            "scope": asdict(runtime_scope),
            "capability_scope": capability_scope,
            "profile": deepcopy(dict(resolution.get("profile") or {})),
            "resolution_digest": str(resolution.get("resolution_digest") or ""),
            "registry_watermark": str(resolution.get("registry_watermark") or ""),
            "lifecycle_watermark": str(resolution.get("lifecycle_watermark") or ""),
            "capabilities": entries,
            "truncated": False,
        }

    definitions = service.list_definitions(
        runtime_scope=runtime_scope,
        capability_scope=capability_scope,
        status="active",
        at_time=at_time,
        limit=limit + 1,
    )
    if len(definitions) > limit:
        raise CapabilityConsumerViewError("active capability count exceeds limit; refusing truncation")
    entries = [_definition_entry(item) for item in definitions if isinstance(item, Mapping)]
    entries.sort(key=lambda item: str(item["capability_id"]))
    return {
        "schema": CONSUMER_VIEW_SCHEMA,
        "source": "registry",
        "scope": asdict(runtime_scope),
        "capability_scope": capability_scope,
        "profile": {},
        "resolution_digest": "",
        "registry_watermark": "",
        "lifecycle_watermark": "",
        "capabilities": entries,
        "truncated": False,
    }


def dynamic_evaluation_view(
    runtime: Any,
    *,
    scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    profile_key: str = "",
    catalog: CapabilityEvaluationCatalog | None = None,
    at_time: str = "",
    max_cases: int = 100,
) -> dict[str, Any]:
    """Resolve a bounded, exact catalog selection for a secondary consumer.

    This is intentionally a *read* surface.  It joins the registry/Profile
    control plane to the immutable evaluation catalog and returns only cases
    whose revision and provider binding are singular.  An ambiguous target is
    represented as ``blocked``; callers must not fall back to a compiled
    capability list or pick the most-recent binding.
    """

    runtime_scope = exact_runtime_scope(scope)
    if isinstance(max_cases, bool) or not isinstance(max_cases, int) or not 1 <= max_cases <= _MAX_EVALUATION_CASES:
        raise CapabilityConsumerViewError(
            f"max_cases must be an integer from 1 to {_MAX_EVALUATION_CASES}"
        )
    try:
        capability_view = dynamic_capability_views(
            runtime,
            scope=runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            at_time=at_time,
            limit=min(_MAX_CAPABILITIES, max_cases),
        )
    except Exception as exc:
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="capability_view_resolution_failed",
            errors=[type(exc).__name__],
        )

    # Application bootstrap may register trusted descriptors into the
    # process-local catalog.  This read path accepts neither stored payloads
    # nor duck-typed objects as executable catalogs, and never inserts a
    # historical case cohort merely because a caller omitted ``catalog``.
    try:
        active_catalog = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError as exc:
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="evaluation_catalog_untrusted",
            errors=[str(exc)],
            capability_view=capability_view,
        )
    list_cases = getattr(active_catalog, "list_cases", None)
    if not callable(list_cases):
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="evaluation_catalog_unavailable",
            capability_view=capability_view,
        )
    try:
        catalog_cases = list(list_cases())
    except Exception as exc:
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="evaluation_catalog_list_failed",
            errors=[type(exc).__name__],
            capability_view=capability_view,
        )
    # A process-wide catalog may hold evaluators for capabilities that are not
    # live in this runtime/profile.  They are not an ambiguity and must not
    # make an otherwise valid active selection fail.  Only the exact active
    # view participates in the bounded selector below.
    active_capability_ids = {
        str(item.get("capability_id") or "").strip()
        for item in capability_view.get("capabilities") or ()
        if isinstance(item, Mapping) and str(item.get("capability_id") or "").strip()
    }
    catalog_cases = [
        case
        for case in catalog_cases
        if str(getattr(case, "capability_id", "") or "").strip() in active_capability_ids
    ]
    if len(catalog_cases) > max_cases:
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="evaluation_catalog_case_limit_exceeded",
            errors=[f"catalog_cases:{len(catalog_cases)}", f"max_cases:{max_cases}"],
            capability_view=capability_view,
        )
    case_ids = [str(getattr(case, "case_id", "") or "") for case in catalog_cases]
    if not case_ids or any(not case_id for case_id in case_ids):
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="evaluation_catalog_has_no_active_cases",
            capability_view=capability_view,
        )

    try:
        if str(profile_key or "").strip():
            selection = active_catalog.resolve_profile_cases(
                runtime,
                profile_key=str(profile_key),
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                case_ids=case_ids,
                at_time=at_time,
                max_candidates=max_cases,
            )
        else:
            selection = active_catalog.resolve_active_cases(
                runtime,
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
                case_ids=case_ids,
                at_time=at_time,
            )
    except Exception as exc:
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="evaluation_catalog_selection_failed",
            errors=[type(exc).__name__],
            capability_view=capability_view,
        )
    if not isinstance(selection, Mapping) or selection.get("ok") is not True:
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason=str(selection.get("reason") or "evaluation_catalog_selection_blocked")
            if isinstance(selection, Mapping)
            else "evaluation_catalog_selection_invalid",
            errors=[str(item) for item in (selection.get("errors") or ())]
            if isinstance(selection, Mapping)
            else [],
            capability_view=capability_view,
            selection=selection if isinstance(selection, Mapping) else {},
        )
    entries = selection.get("cases") if isinstance(selection.get("cases"), Sequence) else ()
    cases: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            return _blocked_evaluation_view(
                runtime_scope,
                capability_scope=capability_scope,
                profile_key=profile_key,
                reason="evaluation_catalog_case_entry_invalid",
                capability_view=capability_view,
                selection=selection,
            )
        artifact = entry.get("artifact")
        target = entry.get("target")
        if not isinstance(artifact, Mapping) or not isinstance(target, Mapping):
            return _blocked_evaluation_view(
                runtime_scope,
                capability_scope=capability_scope,
                profile_key=profile_key,
                reason="evaluation_catalog_case_target_missing",
                capability_view=capability_view,
                selection=selection,
            )
        required_artifact = ("case_id", "capability", "evaluation_case_digest", "eval_spec_id")
        required_target = ("capability_id", "capability_revision_id", "provider_binding_id")
        if any(not str(artifact.get(key) or "").strip() for key in required_artifact) or any(
            not str(target.get(key) or "").strip() for key in required_target
        ):
            return _blocked_evaluation_view(
                runtime_scope,
                capability_scope=capability_scope,
                profile_key=profile_key,
                reason="evaluation_catalog_case_identity_missing",
                capability_view=capability_view,
                selection=selection,
            )
        if str(artifact.get("capability")) != str(target.get("capability_id")):
            return _blocked_evaluation_view(
                runtime_scope,
                capability_scope=capability_scope,
                profile_key=profile_key,
                reason="evaluation_catalog_case_target_mismatch",
                capability_view=capability_view,
                selection=selection,
            )
        cases.append({"artifact": deepcopy(dict(artifact)), "target": deepcopy(dict(target))})
    if not cases:
        return _blocked_evaluation_view(
            runtime_scope,
            capability_scope=capability_scope,
            profile_key=profile_key,
            reason="evaluation_catalog_has_no_resolved_cases",
            capability_view=capability_view,
            selection=selection,
        )
    return {
        "schema": DYNAMIC_EVALUATION_VIEW_SCHEMA,
        "ok": True,
        "status": "resolved",
        "scope": asdict(runtime_scope),
        "capability_scope": capability_scope,
        "profile": deepcopy(dict(selection.get("profile") or capability_view.get("profile") or {})),
        "capability_view": capability_view,
        "resolution_digest": str(capability_view.get("resolution_digest") or ""),
        "registry_watermark": str(selection.get("registry_watermark") or capability_view.get("registry_watermark") or ""),
        "lifecycle_watermark": str(selection.get("lifecycle_watermark") or capability_view.get("lifecycle_watermark") or ""),
        "cases": cases,
        "case_count": len(cases),
    }


def capability_aliases_from_view(view: Mapping[str, Any] | object) -> dict[str, str]:
    """Read versioned alias metadata without giving it classification power.

    A profile can carry historical label aliases in its immutable provenance,
    e.g. ``{"capability_aliases": {"legacy.memory": "memory.recall"}}``.
    Aliases only rewrite an already explicit label; they never inspect prose or
    invent a semantic target for an unclassified record.
    """

    if not isinstance(view, Mapping):
        return {}
    known = {
        str(item.get("capability_id") or "").strip()
        for item in (view.get("capabilities") or ())
        if isinstance(item, Mapping) and str(item.get("capability_id") or "").strip()
    }
    profile = view.get("profile") if isinstance(view.get("profile"), Mapping) else {}
    provenance = profile.get("provenance") if isinstance(profile.get("provenance"), Mapping) else {}
    migration = provenance.get("migration") if isinstance(provenance.get("migration"), Mapping) else {}
    raw_aliases = provenance.get("capability_aliases")
    if not isinstance(raw_aliases, Mapping):
        raw_aliases = migration.get("capability_aliases")
    if not isinstance(raw_aliases, Mapping):
        return {}
    aliases: dict[str, str] = {}
    for raw_alias, raw_target in raw_aliases.items():
        alias = str(raw_alias or "").strip()
        target = str(raw_target or "").strip()
        if not alias or not target:
            continue
        try:
            target = normalize_capability_id(target, field="capability_alias_target")
        except CapabilityContractError:
            continue
        if known and target not in known:
            continue
        aliases[alias] = target
    return dict(sorted(aliases.items()))


def resolve_explicit_capability_attribution(
    payloads: Sequence[Mapping[str, Any] | object],
    *,
    allowed_capability_ids: Sequence[str] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Resolve only a record-declared capability attribution.

    This deliberately has no keyword classifier.  A caller may carry a
    durable ``capability_attribution`` object with rule/migration metadata, or
    one of the historical explicit capability fields.  If the declaration is
    missing, malformed, or outside the selected registry/profile set, the
    result is ``unclassified`` rather than a guessed fallback.
    """

    known = {str(item or "").strip() for item in allowed_capability_ids or () if str(item or "").strip()}
    alias_map = {str(key or "").strip(): str(value or "").strip() for key, value in (aliases or {}).items()}
    candidate = ""
    source = ""
    rule_id = ""
    migration_id = ""
    for raw_payload in payloads:
        if not isinstance(raw_payload, Mapping):
            continue
        payload = raw_payload
        attribution = payload.get("capability_attribution")
        if isinstance(attribution, Mapping):
            candidate = str(
                attribution.get("capability_id")
                or attribution.get("target_capability")
                or attribution.get("capability")
                or ""
            ).strip()
            if candidate:
                source = "capability_attribution"
                rule_id = str(attribution.get("rule_id") or attribution.get("rule") or "").strip()
                migration_id = str(attribution.get("migration_id") or "").strip()
                break
        for key in ("capability_id", "target_capability", "capability", "capability_domain"):
            value = str(payload.get(key) or "").strip()
            if value:
                candidate = value
                source = f"explicit_field:{key}"
                break
        if candidate:
            break
    if not candidate:
        return {
            "schema": EXPLICIT_ATTRIBUTION_SCHEMA,
            "status": "unclassified",
            "capability_id": "unclassified",
            "reason": "missing_explicit_capability_attribution",
            "source": "",
            "rule_id": "",
            "migration_id": "",
        }
    resolved = alias_map.get(candidate, candidate)
    try:
        resolved = normalize_capability_id(resolved, field="explicit_capability_attribution")
    except CapabilityContractError:
        return {
            "schema": EXPLICIT_ATTRIBUTION_SCHEMA,
            "status": "unclassified",
            "capability_id": "unclassified",
            "reason": "invalid_explicit_capability_attribution",
            "source": source,
            "rule_id": rule_id,
            "migration_id": migration_id,
        }
    if known and resolved not in known:
        return {
            "schema": EXPLICIT_ATTRIBUTION_SCHEMA,
            "status": "unclassified",
            "capability_id": "unclassified",
            "reason": "capability_outside_selected_view",
            "source": source,
            "rule_id": rule_id,
            "migration_id": migration_id,
        }
    return {
        "schema": EXPLICIT_ATTRIBUTION_SCHEMA,
        "status": "classified",
        "capability_id": resolved,
        "reason": "explicit_capability_attribution",
        "source": source,
        "rule_id": rule_id,
        "migration_id": migration_id,
    }


def _blocked_evaluation_view(
    scope: ScopeRef,
    *,
    capability_scope: str,
    profile_key: str,
    reason: str,
    errors: Sequence[str] = (),
    capability_view: Mapping[str, Any] | None = None,
    selection: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": DYNAMIC_EVALUATION_VIEW_SCHEMA,
        "ok": False,
        "status": "blocked",
        "reason": str(reason),
        "errors": [str(item) for item in errors],
        "scope": asdict(scope),
        "capability_scope": capability_scope,
        "profile_key": str(profile_key or ""),
        "profile": deepcopy(dict((selection or {}).get("profile") or (capability_view or {}).get("profile") or {})),
        "capability_view": deepcopy(dict(capability_view or {})),
        "cases": [],
        "case_count": 0,
    }


def _profile_entry(item: Mapping[str, Any]) -> dict[str, Any] | None:
    capability_id = str(item.get("capability_id") or "").strip()
    definition = item.get("definition") if isinstance(item.get("definition"), Mapping) else {}
    descriptor = definition.get("descriptor") if isinstance(definition.get("descriptor"), Mapping) else {}
    requirement = item.get("requirement") if isinstance(item.get("requirement"), Mapping) else {}
    if not capability_id or not descriptor:
        return None
    return {
        "capability_id": capability_id,
        "display_name": str(descriptor.get("display_name") or capability_id),
        "description": str(descriptor.get("description") or ""),
        "owner": str(descriptor.get("owner") or ""),
        "risk_tier": str(descriptor.get("risk_tier") or ""),
        "tags": [str(item) for item in descriptor.get("tags") or ()],
        "definition_digest": str(definition.get("entity_digest") or descriptor.get("definition_digest") or ""),
        "selection": deepcopy(dict(item.get("selection") or {})),
        "requirement": deepcopy(dict(requirement)),
        "planning_policy": _planning_policy(requirement),
        "revisions": [deepcopy(dict(value)) for value in item.get("revisions") or () if isinstance(value, Mapping)],
        "bindings": [deepcopy(dict(value)) for value in item.get("bindings") or () if isinstance(value, Mapping)],
    }


def _definition_entry(item: Mapping[str, Any]) -> dict[str, Any]:
    descriptor = item.get("descriptor") if isinstance(item.get("descriptor"), Mapping) else {}
    capability_id = str(item.get("entity_id") or descriptor.get("capability_id") or "").strip()
    return {
        "capability_id": capability_id,
        "display_name": str(descriptor.get("display_name") or capability_id),
        "description": str(descriptor.get("description") or ""),
        "owner": str(descriptor.get("owner") or ""),
        "risk_tier": str(descriptor.get("risk_tier") or ""),
        "tags": [str(value) for value in descriptor.get("tags") or ()],
        "definition_digest": str(item.get("entity_digest") or descriptor.get("definition_digest") or ""),
        "selection": {},
        "requirement": {},
        "planning_policy": {},
        "revisions": [],
        "bindings": [],
    }


def _planning_policy(requirement: Mapping[str, Any]) -> dict[str, float]:
    raw = requirement.get("planning_policy") if isinstance(requirement.get("planning_policy"), Mapping) else {}
    result: dict[str, float] = {}
    for key in ("user_value", "risk", "cost", "priority_weight"):
        value = raw.get(key)
        if isinstance(value, bool):
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 <= numeric <= 1.0:
            result[key] = numeric
    return result


__all__ = [
    "CONSUMER_VIEW_SCHEMA",
    "DYNAMIC_EVALUATION_VIEW_SCHEMA",
    "EXPLICIT_ATTRIBUTION_SCHEMA",
    "CapabilityConsumerViewError",
    "capability_aliases_from_view",
    "dynamic_evaluation_view",
    "dynamic_capability_views",
    "resolve_explicit_capability_attribution",
]
