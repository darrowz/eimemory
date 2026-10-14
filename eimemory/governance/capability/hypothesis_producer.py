"""Capability hypothesis producer driven only by real observed profile gaps.

The dynamic evolution planner blocks every profile gap until exactly one
``candidate`` hypothesis exists for that exact capability revision.  This
producer closes that loop without inventing evidence:

* its inputs are the planner's own blocked work items (exact capability,
  revision, and provider binding observed in the current projection);
* a hypothesis is created only when exactly one currently applicable knowledge
  link exists for that exact revision; the automatic bridge can review an
  already approved, registry-trusted canonical source against that live
  revision and register an attribution link. It never promotes raw knowledge
  or interprets goal wording or knowledge volume as applicability;
* when no such link exists, an independently revalidated registry/profile gap
  may run its registered evaluation cases as a diagnostic hypothesis. Only
  observations and a diagnostic reflection are written, never a knowledge
  link or a behavior-authorizing capability hypothesis;
* every hypothesis carries the gap provenance (work items, projection digest,
  input watermark) in its expected metric, stays ``behavior_influence=False``
  until independent evaluation, and can be revoked with
  :func:`revoke_produced_hypothesis` (append-only; honoured by the planner);
* ``EIMEMORY_CAPABILITY_HYPOTHESIS_PRODUCER=0`` disables it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
import os
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.jsonl import payload_digest

PRODUCER_ID = "eimemory.capability_hypothesis_producer.v1"
REVOCATION_REPORT_TYPE = "capability_hypothesis_revocation"
_GAP_REASON = "hypothesis_missing_or_ambiguous"


def hypothesis_producer_enabled() -> bool:
    value = str(os.environ.get("EIMEMORY_CAPABILITY_HYPOTHESIS_PRODUCER", "1")).strip().lower()
    return value not in {"0", "false", "no", "off"}


def _scope(value: ScopeRef | Mapping[str, Any]) -> ScopeRef:
    return value if isinstance(value, ScopeRef) else ScopeRef.from_dict(dict(value or {}))


def revoked_hypothesis_ids(runtime: Any, *, runtime_scope: ScopeRef | Mapping[str, Any],
                           strict: bool = False) -> set[str]:
    """Revoked or invalidated ids; strict forward-effect checks fail closed."""

    scope = _scope(runtime_scope)
    store = getattr(runtime, "store", runtime)
    lookup = getattr(store, "list_records_by_meta_value", None)
    records: list[Any] = []
    if callable(lookup):
        try:
            queried = lookup(
                    kinds=["reflection"],
                    scope=scope,
                    meta_key="report_type",
                    meta_value=REVOCATION_REPORT_TYPE,
                    limit=501 if strict else 500,
                )
            if strict and queried is None:
                raise RuntimeError("hypothesis_revocation_inventory_unavailable")
            records = list(queried or [])
        except Exception:
            if strict:
                raise RuntimeError("hypothesis_revocation_inventory_unavailable") from None
            records = []
    else:
        queried = store.list_records(kinds=["reflection"], scope=scope, limit=501 if strict else 500)
        if strict and queried is None:
            raise RuntimeError("hypothesis_revocation_inventory_unavailable")
        raw_records = list(queried or [])
        if strict and len(raw_records) > 500:
            raise RuntimeError("hypothesis_revocation_inventory_truncated")
        records = [
            record
            for record in raw_records
            if str((record.meta or {}).get("report_type") or "") == REVOCATION_REPORT_TYPE
        ]
    if strict and len(records) > 500:
        raise RuntimeError("hypothesis_revocation_inventory_truncated")
    revoked: set[str] = set()
    for record in records:
        content = record.content if isinstance(record.content, Mapping) else {}
        hypothesis_id = str(content.get("hypothesis_id") or "").strip()
        if hypothesis_id and record.scope == scope:
            revoked.add(hypothesis_id)
    # A producer-created hypothesis depends on its immutable applicability
    # receipt. Revocation or changed source/approval/revision inputs removes
    # that candidate from planning without rewriting historical evidence.
    from eimemory.governance.capability.knowledge_gap_review import knowledge_gap_review_current
    hypotheses = store.list_records(kinds=["capability_hypothesis"], scope=scope, limit=501 if strict else 500)
    if strict and (hypotheses is None or len(hypotheses) > 500):
        raise RuntimeError("hypothesis_inventory_unavailable")
    for record in hypotheses:
        if record.scope != scope:
            continue
        content = record.content if isinstance(record.content, Mapping) else {}
        metric = content.get("expected_metric") if isinstance(content.get("expected_metric"), Mapping) else {}
        gap = metric.get("gap_evidence") if isinstance(metric.get("gap_evidence"), Mapping) else {}
        review_id = str(gap.get("knowledge_review_receipt_id") or "")
        if review_id and not knowledge_gap_review_current(runtime, runtime_scope=scope, review_receipt_id=review_id):
            revoked.add(record.record_id)
        bounds = content.get("candidate_bounds") if isinstance(content.get("candidate_bounds"), Mapping) else {}
        # Immutable producer candidates retain the authority snapshot under
        # which they were created. A different current machine policy cannot
        # silently widen that old candidate; a fresh bounded one is required.
        if gap.get("producer") == PRODUCER_ID and bounds.get("write_authority_snapshot") is True:
            current, _ = _candidate_bounds(
                runtime, scope=scope, profile_key=str(gap.get("profile_key") or ""),
                capability_scope=str(content.get("capability_scope") or ""),
                capability_id=str(content.get("capability_id") or ""),
                revision_id=str(content.get("capability_revision_id") or ""),
                binding_ids=list(gap.get("provider_binding_ids") or []),
            )
            if current != dict(bounds):
                revoked.add(record.record_id)
    return revoked


def revoke_produced_hypothesis(
    runtime: Any,
    *,
    runtime_scope: ScopeRef | Mapping[str, Any],
    hypothesis_id: str,
    reason: str,
    actor: str = "operator",
) -> RecordEnvelope:
    """Append a revocation; the planner then ignores that hypothesis."""

    scope = _scope(runtime_scope)
    normalized_id = str(hypothesis_id or "").strip()
    normalized_reason = str(reason or "").strip()[:1_000]
    if not normalized_id or not normalized_reason:
        raise ValueError("hypothesis_id and reason are required")
    store = getattr(runtime, "store", runtime)
    ts = now_iso()
    digest = payload_digest({"hypothesis_id": normalized_id, "reason": normalized_reason, "actor": actor})
    record = RecordEnvelope(
        record_id=f"capability_hypothesis_revocation_{digest[:32]}",
        kind="reflection",
        status="active",
        title=f"Capability hypothesis revoked: {normalized_id}",
        summary=normalized_reason,
        detail="Append-only revocation; the dynamic evolution planner ignores revoked hypotheses.",
        content={
            "report_type": REVOCATION_REPORT_TYPE,
            "hypothesis_id": normalized_id,
            "reason": normalized_reason,
            "actor": str(actor or "operator"),
            "producer": PRODUCER_ID,
        },
        tags=["capability", "hypothesis", "revocation"],
        links=[LinkRef(relation="revokes", target_kind="capability_hypothesis", target_id=normalized_id)],
        evidence=[f"capability_hypothesis:{normalized_id}"],
        source="eimemory.governance.capability.hypothesis_producer",
        scope=scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        provenance={"producer": PRODUCER_ID, "actor": str(actor or "operator")},
        meta={"report_type": REVOCATION_REPORT_TYPE, "hypothesis_id": normalized_id},
    )
    return store.append(record)


def _applicable_link_rows(runtime: Any, *, scope: ScopeRef, capability_scope: str, capability_id: str,
                          revision_id: str, environment_context: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    from eimemory.knowledge.capabilities import assess_knowledge_capability_eligibility, list_registered_knowledge_links
    from eimemory.governance.capability.knowledge_gap_review import knowledge_gap_review_current

    rows = list_registered_knowledge_links(
        runtime,
        runtime_scope=scope,
        capability_scope=capability_scope,
        capability_id=capability_id,
        capability_revision_id=revision_id,
        limit=500,
    )
    applicable: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
        if (
            str(payload.get("capability_revision_id") or row.get("capability_revision_id") or "") == revision_id
            and str(payload.get("applicability") or "") == "applicable"
            and str(payload.get("source_status") or "") == "active"
            and str(payload.get("review_state") or "") in {"reviewed", "approved"}
            and str(payload.get("source_trust") or "") in {"medium", "high"}
            and str(payload.get("contradiction_state") or "") != "contradicted"
        ):
            provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
            review_refs = provenance.get("review_evidence_refs") or []
            if review_refs and (not isinstance(review_refs, list) or any(
                not knowledge_gap_review_current(runtime, runtime_scope=scope, review_receipt_id=review_id)
                for review_id in review_refs
            )):
                continue
            from eimemory.storage import knowledge_review_inventory as inventory
            snapshot = inventory.record_snapshot(runtime.store, scope, str(payload.get("knowledge_record_id") or ""))
            knowledge = snapshot[0] if snapshot else None
            if (knowledge is None or knowledge.scope != scope
                    or payload_digest(knowledge.to_dict()) != provenance.get("knowledge_record_digest")):
                continue
            assessment = assess_knowledge_capability_eligibility(
                runtime, knowledge_record=knowledge, runtime_scope=scope,
                source_trust=str(payload.get("source_trust") or ""),
                review_state=str(payload.get("review_state") or ""),
                temporal_validity=payload.get("temporal_validity"),
                environment_constraints=payload.get("environment_constraints"),
                environment_context=environment_context or {"supported": True},
            )
            if assessment.applicability != "applicable":
                continue
            applicable.append({**dict(row), "payload": dict(payload)})
    return applicable


def produce_capability_hypotheses(
    runtime: Any,
    *,
    profile_key: str,
    runtime_scope: ScopeRef | Mapping[str, Any],
    capability_scope: str = "global",
    plan: Mapping[str, Any] | None = None,
    catalog: Any = None,
) -> dict[str, Any]:
    """Create at most one hypothesis per real gap revision; report every skip."""

    scope = _scope(runtime_scope)
    report: dict[str, Any] = {
        "ok": True,
        "report_type": "capability_hypothesis_producer",
        "producer": PRODUCER_ID,
        "enabled": hypothesis_producer_enabled(),
        "profile_key": str(profile_key or ""),
        "capability_scope": capability_scope,
        "gap_count": 0,
        "revision_count": 0,
        "created": [],
        "reused": [],
        "knowledge_link_reviews": [],
        "diagnostics": [],
        "skipped": [],
    }
    if not report["enabled"]:
        report["status"] = "disabled"
        return report
    if plan is None:
        from eimemory.governance.evolution.dynamic_capability_evolution import (
            build_dynamic_capability_evolution_plan,
        )

        plan = build_dynamic_capability_evolution_plan(
            runtime,
            profile_key=str(profile_key),
            runtime_scope=scope,
            capability_scope=capability_scope,
            catalog=catalog,
        )
    gaps = [
        item
        for item in list(plan.get("work_items") or [])
        if isinstance(item, Mapping)
        and item.get("status") == "blocked"
        and item.get("reason") == _GAP_REASON
        and int((item.get("detail") or {}).get("candidate_hypothesis_count", -1)) == 0
        and str(item.get("capability_id") or "")
        and str(item.get("capability_revision_id") or "")
        and str(item.get("provider_binding_id") or "")
    ]
    report["gap_count"] = len(gaps)
    by_revision: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for item in gaps:
        by_revision.setdefault(
            (str(item["capability_id"]), str(item["capability_revision_id"])), []
        ).append(item)
    report["revision_count"] = len(by_revision)
    from eimemory.governance.capability.capability_hypotheses import (
        CapabilityHypothesisError,
        create_capability_hypothesis,
    )

    for (capability_id, revision_id), items in sorted(by_revision.items()):
        binding_ids = sorted({str(item["provider_binding_id"]) for item in items})
        work_item_ids = sorted({str(item.get("work_item_id") or "") for item in items})
        base = {
            "capability_id": capability_id,
            "capability_revision_id": revision_id,
            "provider_binding_ids": binding_ids,
            "work_item_ids": work_item_ids,
        }
        environment_context: dict[str, Any] = {"supported": True, "provider_binding_ids": binding_ids}
        try:
            from eimemory.governance.capability.knowledge_gap_review import _live_target
            environment_context = _live_target(
                runtime, scope, capability_scope, capability_id, revision_id, binding_ids,
            )["environment_context"]
        except Exception:
            # Existing link/hypothesis creation independently rejects an
            # absent live revision; diagnostic mode revalidates the profile.
            pass
        try:
            links = _applicable_link_rows(
                runtime,
                scope=scope,
                capability_scope=capability_scope,
                capability_id=capability_id,
                revision_id=revision_id,
                environment_context=environment_context,
            )
        except Exception as exc:  # link registry unavailable is a visible skip
            report["skipped"].append({**base, "reason": "knowledge_link_query_failed", "error": str(exc)[:300]})
            continue
        if not links:
            from eimemory.governance.capability.knowledge_gap_review import review_gap_knowledge
            review = _review_current_gap(
                runtime, scope=scope, profile_key=str(profile_key), capability_scope=capability_scope,
                capability_id=capability_id, revision_id=revision_id, binding_ids=binding_ids,
                catalog=catalog, reviewer=review_gap_knowledge,
            )
            report["knowledge_link_reviews"].append({**base, **review})
            if review.get("applicable_count", 0) > 1:
                report["skipped"].append({**base, "reason": "ambiguous_reviewed_knowledge_for_gap_revision",
                                          "review_receipt_ids": review.get("review_receipt_ids", [])})
                continue
            if review.get("links"):
                links = _applicable_link_rows(
                    runtime, scope=scope, capability_scope=capability_scope,
                    capability_id=capability_id, revision_id=revision_id,
                    environment_context=environment_context,
                )
        if not links:
            diagnostic = _diagnose_profile_gap(
                runtime, scope=scope, profile_key=str(profile_key),
                capability_scope=capability_scope, capability_id=capability_id,
                revision_id=revision_id, binding_ids=binding_ids, catalog=catalog,
            )
            if diagnostic.get("executed") is True:
                report["diagnostics"].append({**base, **diagnostic})
            report["skipped"].append({**base, "reason": "no_applicable_knowledge_link_for_gap_revision",
                                      "diagnostic": diagnostic})
            continue
        if len(links) != 1:
            report["skipped"].append(
                {**base, "reason": "ambiguous_applicable_knowledge_links", "link_count": len(links)}
            )
            continue
        link = links[0]
        link_id = str(link.get("link_id") or "")
        link_digest = str(link.get("link_digest") or "")
        gap_evidence = {
            "producer": PRODUCER_ID,
            "profile_key": str(profile_key),
            "projection_digest": str(plan.get("projection_digest") or ""),
            "input_watermark": str(plan.get("input_watermark") or ""),
            "work_item_ids": work_item_ids,
            "provider_binding_ids": binding_ids,
            "observed_gap_reason": _GAP_REASON,
        }
        payload = link.get("payload") if isinstance(link.get("payload"), Mapping) else {}
        link_provenance = payload.get("provenance") if isinstance(payload.get("provenance"), Mapping) else {}
        review_refs = link_provenance.get("review_evidence_refs") or []
        if review_refs:
            gap_evidence["knowledge_review_receipt_id"] = str(review_refs[0])
        candidate_bounds, bounds_reason = _candidate_bounds(
            runtime, scope=scope, profile_key=str(profile_key), capability_scope=capability_scope,
            capability_id=capability_id, revision_id=revision_id, binding_ids=binding_ids,
        )
        request_key = "gap-hypothesis:" + payload_digest({
            "profile_key": str(profile_key), "capability_scope": capability_scope,
            "capability_id": capability_id, "capability_revision_id": revision_id,
            "link_id": link_id, "link_digest": link_digest, "provider_binding_ids": binding_ids,
            "candidate_bounds": candidate_bounds,
        })
        previous = runtime.store.list_records(kinds=["capability_hypothesis"], scope=scope, limit=500)
        existing_ids = {record.record_id for record in previous if record.scope == scope}
        try:
            record = create_capability_hypothesis(
                runtime,
                runtime_scope=scope,
                capability_scope=capability_scope,
                link_id=link_id,
                link_digest=link_digest,
                statement=(
                    f"Observed profile gap on {revision_id} (bindings: {', '.join(binding_ids)}) "
                    f"may close using registered knowledge link {link_id}; "
                    "only independent evaluation of the profile-selected cases can confirm it."
                ),
                expected_metric={
                    "metric": "profile_gap_closed_by_independent_evaluation",
                    "capability_revision_id": revision_id,
                    "gap_evidence": gap_evidence,
                },
                environment_context=environment_context,
                candidate_bounds=candidate_bounds,
                loop_id="capability_hypothesis_producer",
                request_key=request_key,
            )
        except CapabilityHypothesisError as exc:
            report["skipped"].append({**base, "reason": "hypothesis_rejected", "error": str(exc)[:300]})
            continue
        if record.record_id in revoked_hypothesis_ids(runtime, runtime_scope=scope, strict=True):
            report["skipped"].append({**base, "reason": "produced_hypothesis_revoked", "hypothesis_id": record.record_id})
            continue
        report["reused" if record.record_id in existing_ids else "created"].append(
            {
                **base,
                "hypothesis_id": record.record_id,
                "status": record.status,
                "link_id": link_id,
                "blocked_reasons": list((record.content or {}).get("blocked_reasons") or []),
                "candidate_bounds_status": "bounded_write_candidate" if candidate_bounds["max_changes"] > 0 else "evaluation_only",
                "candidate_bounds_reason": bounds_reason,
                "candidate_max_changes": candidate_bounds["max_changes"],
                # The hypothesis itself never supplies forward-effect authority.
                "code_changes_authorized": False,
            }
        )
    report["status"] = ("produced" if report["created"] else "reused" if report["reused"] else "diagnosed" if report["diagnostics"]
                        else "no_gaps" if not gaps else "no_eligible_evidence")
    return report


def _candidate_bounds(runtime, *, scope, profile_key, capability_scope, capability_id,
                      revision_id, binding_ids):
    """Only the official immutable Hermes contract and live policy can bound writes."""
    bounds = {"side_effect_class": "none", "max_changes": 0,
              "capability_revision_id": revision_id, "provider_binding_ids": binding_ids,
              "write_authority_snapshot": True}
    try:
        from eimemory.governance.evolution.code_evolution_dynamic_context import dynamic_patch_candidate_bounds
        result = dynamic_patch_candidate_bounds(
            runtime, runtime_scope=scope, profile_key=profile_key, capability_scope=capability_scope,
            capability_id=capability_id, capability_revision_id=revision_id,
            provider_binding_ids=binding_ids,
        )
    except Exception:
        return bounds, "dynamic_patch_candidate_bounds_unavailable"
    if result.get("ok") is not True:
        return bounds, str(result.get("reason") or "no_registered_writable_candidate_bounds")
    declared = result.get("candidate_bounds")
    if (not isinstance(declared, Mapping) or type(declared.get("max_changes")) is not int
            or declared["max_changes"] < 1 or not declared.get("machine_authority_digest")):
        return bounds, "dynamic_patch_candidate_bounds_invalid"
    return {**bounds, **dict(declared)}, ""


def _review_current_gap(runtime, *, scope, profile_key, capability_scope, capability_id,
                        revision_id, binding_ids, catalog, reviewer):
    """A caller-supplied stale gap cannot select arbitrary targets for the model."""
    from eimemory.governance.evolution.dynamic_capability_evolution import build_dynamic_capability_evolution_plan
    try:
        fresh = build_dynamic_capability_evolution_plan(
            runtime, profile_key=profile_key, runtime_scope=scope,
            capability_scope=capability_scope, catalog=catalog,
        )
        current = {str(item.get("provider_binding_id") or "") for item in fresh.get("work_items") or []
                   if item.get("reason") == _GAP_REASON and (item.get("detail") or {}).get("candidate_hypothesis_count") == 0
                   and item.get("capability_id") == capability_id and item.get("capability_revision_id") == revision_id}
        if not set(binding_ids).issubset(current):
            return {"status": "blocked", "reason_counts": {"knowledge_review_gap_not_current": 1}}
        return reviewer(runtime, runtime_scope=scope, capability_scope=capability_scope,
                        capability_id=capability_id, revision_id=revision_id, binding_ids=binding_ids)
    except Exception:
        return {"status": "blocked", "reason_counts": {"knowledge_review_unavailable": 1}}


def _diagnose_profile_gap(runtime, *, scope, profile_key, capability_scope,
                          capability_id, revision_id, binding_ids, catalog):
    """Collect independent evidence without manufacturing a knowledge link.

    Rebuild the plan, even when the producer received a caller-supplied plan.
    A stale or forged gap cannot cause an evaluation of an arbitrary target.
    The catalog is the same trusted in-process authority used by acceptance.
    """
    from eimemory.evaluation.capability_catalog import resolve_application_capability_catalog
    from eimemory.governance.capability.capability_acceptance import run_capability_acceptance
    from eimemory.governance.evolution.dynamic_capability_evolution import build_dynamic_capability_evolution_plan

    try:
        catalog = resolve_application_capability_catalog(catalog)
        fresh = build_dynamic_capability_evolution_plan(
            runtime, profile_key=profile_key, runtime_scope=scope,
            capability_scope=capability_scope, catalog=catalog,
        )
        current = {
            str(item.get("provider_binding_id") or "")
            for item in fresh.get("work_items") or []
            if item.get("reason") == _GAP_REASON
            and (item.get("detail") or {}).get("candidate_hypothesis_count") == 0
            and item.get("capability_id") == capability_id
            and item.get("capability_revision_id") == revision_id
        }
        if not set(binding_ids).issubset(current):
            return {"executed": False, "reason": "diagnostic_gap_not_current"}
        selection = catalog.resolve_profile_cases(
            runtime, profile_key=profile_key, runtime_scope=scope,
            capability_scope=capability_scope,
        )
        if selection.get("ok") is not True:
            return {"executed": False, "reason": str(selection.get("reason") or "diagnostic_selection_blocked")}
        cases = [entry for entry in selection.get("cases") or []
                 if entry["target"].get("capability_id") == capability_id
                 and entry["target"].get("capability_revision_id") == revision_id
                 and entry["target"].get("provider_binding_id") in binding_ids]
        evaluated_bindings = sorted({entry["target"]["provider_binding_id"] for entry in cases})
        missing_bindings = sorted(set(binding_ids) - set(evaluated_bindings))
        if not cases:
            return {"executed": False, "reason": "profile_selected_evaluation_case_missing"}
        case_ids = sorted({entry["artifact"]["case_id"] for entry in cases})
        # Acceptance resolves the live profile again and persists independent
        # traces, evaluation specs/runs and observations. No hypothesis gate,
        # evolution opportunity, machine policy, patch or apply is invoked.
        evaluation = run_capability_acceptance(
            runtime, scope=scope, runtime_scope=scope, profile_key=profile_key,
            capability_scope=capability_scope, catalog=catalog,
            case_ids=case_ids, persist=True,
        )
        expected_targets = {
            (entry["artifact"]["case_id"], entry["target"]["capability_id"],
             entry["target"]["capability_revision_id"], entry["target"]["provider_binding_id"])
            for entry in cases
        }
        actual_targets = {
            (row.get("case_id"), row.get("capability"), row.get("capability_revision_id"), row.get("provider_binding_id"))
            for row in evaluation.get("results") or []
        }
        target_binding_verified = actual_targets == expected_targets
        from eimemory.capabilities.projector import CapabilityStateProjector
        # Catalog observations have microsecond timestamps. A second-truncated
        # projection would incorrectly omit evidence just collected in this
        # same second. Use the actual current instant, never a future cutoff.
        projection_after = CapabilityStateProjector(runtime.store).project(
            profile_key, runtime_scope=scope, capability_scope=capability_scope,
            at_time=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            persist=False,
        ).to_dict()
    except Exception as exc:
        return {"executed": False, "reason": "diagnostic_unavailable", "error_type": type(exc).__name__}
    content = {
        "report_type": "capability_gap_diagnostic", "producer": PRODUCER_ID,
        "profile_key": profile_key, "capability_scope": capability_scope,
        "capability_id": capability_id, "capability_revision_id": revision_id,
        "provider_binding_ids": binding_ids, "projection_digest": fresh.get("projection_digest"),
        "evaluated_provider_binding_ids": evaluated_bindings,
        "missing_provider_binding_ids": missing_bindings,
        "input_watermark": fresh.get("input_watermark"), "evaluation_cases": cases,
        "statement": "The profile gap may reflect missing independent evaluation; execute registered cases to test it.",
        "evaluation": evaluation, "projection_after": projection_after,
        "target_binding_verified": target_binding_verified,
        "behavior_influence": {"allowed": False},
        "code_changes_authorized": False, "certifies_improvement": False, "certifies_l5": False,
    }
    ts = now_iso()
    record = runtime.store.append(RecordEnvelope(
        record_id=f"capability_gap_diagnostic_{payload_digest(content)[:32]}",
        kind="reflection", status="archived", title="Independent capability gap diagnostic",
        summary="Registered cases passed." if evaluation.get("ok") is True and target_binding_verified else "Independent diagnostic did not pass.",
        detail="Diagnostic evidence only; no knowledge link, behavior influence or code-change authority.",
        content=content, tags=["capability", "hypothesis", "diagnostic"],
        links=[], provenance={"producer": PRODUCER_ID, "origin": "profile_gap_diagnostic"},
        evidence=list(evaluation.get("trace_record_ids") or []),
        source="eimemory.governance.capability.hypothesis_producer", scope=scope,
        time=TimeRef(created_at=ts, updated_at=ts, occurred_at=ts),
        meta={"report_type": "capability_gap_diagnostic"},
    ))
    gap_closed = target_binding_verified and _diagnostic_gap_closed(projection_after, capability_id, revision_id, binding_ids)
    return {"executed": True, "record_id": record.record_id,
            "passed": evaluation.get("ok") is True and target_binding_verified,
            "gap_closed": gap_closed,
            "evaluated_provider_binding_ids": evaluated_bindings,
            "missing_provider_binding_ids": missing_bindings,
            "execution_id": evaluation.get("execution_id"), "case_ids": case_ids,
            "blocked_reason": evaluation.get("blocked_reason", "") or (
                "diagnostic_target_changed" if not target_binding_verified else
                "profile_selected_evaluation_case_missing" if missing_bindings and not gap_closed else ""),
            "code_changes_authorized": False}


def _diagnostic_gap_closed(projection, capability_id, revision_id, binding_ids):
    targets = {(capability_id, revision_id, binding_id) for binding_id in binding_ids}
    def target(row):
        return (row.get("capability_id"), row.get("capability_revision_id"), row.get("provider_binding_id"))
    observed = {target(row) for row in projection.get("snapshots") or []}
    blocked = {target(row) for row in projection.get("blocked") or []}
    return targets.issubset(observed) and not targets.intersection(blocked)


__all__ = [
    "PRODUCER_ID",
    "REVOCATION_REPORT_TYPE",
    "hypothesis_producer_enabled",
    "produce_capability_hypotheses",
    "revoke_produced_hypothesis",
    "revoked_hypothesis_ids",
]
