from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from typing import Any

from eimemory.capabilities.consumer_views import dynamic_capability_views, dynamic_evaluation_view
from eimemory.governance.capability_ledger import build_dynamic_capability_ledger
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef

def build_self_model(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    limit: int = 500,
    persist: bool = False,
    loop_id: str = "",
    capability_scope: str = "global",
    profile_key: str = "",
    catalog: Any | None = None,
    at_time: str = "",
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    reflections = _list_all(runtime, kinds=["reflection"], scope=scope_ref, limit=limit)
    incidents = _list_all(runtime, kinds=["incident", "unknown"], scope=scope_ref, limit=limit)
    rules = _list_all(runtime, kinds=["rule"], scope=scope_ref, limit=limit)
    replays = _list_all(runtime, kinds=["replay_result"], scope=scope_ref, limit=limit)
    weaknesses = _weaknesses_from_records(reflections + incidents)
    bounded_limit = max(1, min(499, int(limit or 1)))
    evaluation_view: dict[str, Any] = {}
    if catalog is not None:
        evaluation_view = dynamic_evaluation_view(
            runtime,
            scope=scope_ref,
            capability_scope=capability_scope,
            profile_key=profile_key,
            catalog=catalog,
            at_time=at_time,
            max_cases=min(256, bounded_limit),
        )
        if evaluation_view.get("ok") is not True:
            return {
                "ok": False,
                "status": "blocked",
                "scope": asdict(scope_ref),
                "capability_scope": capability_scope,
                "reason": str(evaluation_view.get("reason") or "capability_evaluation_selection_blocked"),
                "errors": [str(item) for item in evaluation_view.get("errors") or ()],
                "capability_evaluation_view": evaluation_view,
                "capabilities": [],
                "weaknesses": weaknesses,
                "metrics": _metrics(
                    reflections=reflections,
                    incidents=incidents,
                    rules=rules,
                    replays=replays,
                    weaknesses=weaknesses,
                ),
            }
        capability_view = (
            evaluation_view.get("capability_view")
            if isinstance(evaluation_view.get("capability_view"), dict)
            else {}
        )
    else:
        capability_view = dynamic_capability_views(
            runtime,
            scope=scope_ref,
            capability_scope=capability_scope,
            profile_key=profile_key,
            at_time=at_time,
            limit=bounded_limit,
        )
    dynamic_ledger = build_dynamic_capability_ledger(
        runtime,
        scope=scope_ref,
        capability_scope=capability_scope,
        limit=min(500, bounded_limit),
    )
    capabilities = _capabilities_from_dynamic_view(
        capability_view,
        dynamic_ledger,
        evaluation_view=evaluation_view,
    )
    if legacy_compatibility:
        # The retired score ledger is visible only through this explicit
        # compatibility switch; the normal path above remains registry- and
        # catalog-derived.
        from eimemory.governance.capability_ledger import build_capability_ledger

        legacy_ledger = build_capability_ledger(
            runtime,
            scope=scope_ref,
            limit=min(500, bounded_limit),
            attribute_outcomes=False,
            legacy_compatibility=True,
        )
        capability_view = {"capabilities": _legacy_capability_entries(legacy_ledger)}
        dynamic_ledger = {"capabilities": {}}
        capabilities = _capabilities_from_legacy_ledger(legacy_ledger)
    metrics = _metrics(reflections=reflections, incidents=incidents, rules=rules, replays=replays, weaknesses=weaknesses)
    model = {
        "schema_version": "autonomous_learning.v2",
        "scope": asdict(scope_ref),
        "capability_scope": capability_scope,
        "profile": capability_view.get("profile") or {},
        "capability_view_digest": str(capability_view.get("resolution_digest") or ""),
        "capability_ledger_digest": str(dynamic_ledger.get("projection_digest") or ""),
        "capability_evaluation_view": evaluation_view,
        "legacy_compatibility": bool(legacy_compatibility),
        "capabilities": capabilities,
        "weaknesses": weaknesses,
        "metrics": metrics,
    }
    if persist:
        persist_self_model(runtime, model, scope=scope_ref, loop_id=loop_id or "manual")
    return model


def _legacy_capability_entries(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    entries = ledger.get("capabilities") if isinstance(ledger.get("capabilities"), dict) else {}
    return [
        {"capability_id": str(capability), "display_name": str(capability)}
        for capability in sorted(entries)
    ]


def _capabilities_from_legacy_ledger(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    entries = ledger.get("capabilities") if isinstance(ledger.get("capabilities"), dict) else {}
    items: list[dict[str, Any]] = []
    for capability_id, value in sorted(entries.items()):
        entry = value if isinstance(value, dict) else {}
        record_id = str(entry.get("last_record_id") or "")
        items.append(
            {
                "kind": str(capability_id),
                "capability": str(capability_id),
                "title": str(capability_id),
                "status": str(entry.get("status") or "unobserved"),
                "score": float(entry.get("score") or 0.0),
                "observation_count": int(entry.get("evidence_count") or 0),
                "failure_count": 0,
                "revisions": {},
                "definition_digest": "",
                "profile_requirement": {},
                "evaluation_targets": [],
                "source_record_ids": [record_id] if record_id else [],
            }
        )
    return items


def persist_self_model(
    runtime: Any,
    model: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_id: str,
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    model_record = append_learning_record_once(
        runtime,
        kind="capability_model",
        title="Autonomous learning self-model",
        summary=f"{len(model.get('weaknesses') or [])} weaknesses, {len(model.get('capabilities') or [])} capability signals",
        scope=scope_ref,
        loop_id=loop_id,
        step_name="self_model",
        semantic_key=stable_semantic_key("self_model", scope_ref.agent_id, scope_ref.workspace_id, len(model.get("weaknesses") or [])),
        authority_tier="L0",
        status="active",
        content={"model": model},
        meta={"capability_count": len(model.get("capabilities") or []), "weakness_count": len(model.get("weaknesses") or [])},
    )
    weakness_records = []
    for weakness in list(model.get("weaknesses") or [])[:20]:
        record = append_learning_record_once(
            runtime,
            kind="weakness",
            title=str(weakness.get("title") or f"Weakness: {weakness.get('kind') or 'general'}"),
            summary=str(weakness.get("lesson") or weakness.get("summary") or ""),
            scope=scope_ref,
            loop_id=loop_id,
            step_name="weakness",
            semantic_key=str(weakness.get("semantic_key") or stable_semantic_key("weakness", weakness.get("kind"), weakness.get("lesson"))),
            authority_tier="L0",
            status="active",
            content={"weakness": weakness},
            meta={"capability": weakness.get("capability"), "weakness_kind": weakness.get("kind"), "severity": weakness.get("severity")},
        )
        weakness_records.append(record.record_id)
    return {"model_record_id": model_record.record_id, "weakness_record_ids": weakness_records}


def _weaknesses_from_records(records: list[RecordEnvelope]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        meta = business_metadata(record.meta)
        content = record.content if isinstance(record.content, dict) else {}
        payload = content.get("payload") if isinstance(content.get("payload"), dict) else content
        primary_label = str(meta.get("primary_label") or payload.get("primary_label") or "").strip()
        is_outcome = str(meta.get("report_type") or "").strip() == "outcome_trace"
        if is_outcome and primary_label == "success":
            continue
        tag = str(meta.get("tag") or payload.get("tag") or primary_label or meta.get("signal_type") or record.kind or "general").strip()
        lesson = _first_text(
            meta.get("fix"),
            meta.get("lesson"),
            payload.get("next_policy"),
            payload.get("policy_update"),
            payload.get("correction_from_user"),
            payload.get("fix"),
            record.summary,
        )
        if not lesson:
            continue
        capability = _explicit_capability(record, meta=meta, payload=payload)
        key = stable_semantic_key(capability, tag, lesson)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "semantic_key": key,
                "kind": tag,
                "capability": capability,
                "title": f"{capability}: {tag}",
                "lesson": lesson,
                "severity": _severity(record, primary_label),
                "evidence_tier": "T0" if is_outcome else "T2",
                "source_record_ids": [record.record_id],
            }
        )
    return sorted(items, key=lambda item: (-float(item.get("severity") or 0), str(item.get("capability") or "")))


def _capabilities_from_dynamic_view(
    capability_view: dict[str, Any],
    ledger: dict[str, Any],
    *,
    evaluation_view: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Render arbitrary registered capabilities without fixed labels/scores."""

    ledger_items = ledger.get("capabilities") if isinstance(ledger.get("capabilities"), dict) else {}
    targets_by_capability: dict[str, list[dict[str, Any]]] = {}
    for selected in (evaluation_view or {}).get("cases") or ():
        if not isinstance(selected, dict):
            continue
        artifact = selected.get("artifact") if isinstance(selected.get("artifact"), dict) else {}
        target = selected.get("target") if isinstance(selected.get("target"), dict) else {}
        capability_id = str(target.get("capability_id") or artifact.get("capability") or "").strip()
        if not capability_id:
            continue
        targets_by_capability.setdefault(capability_id, []).append(
            {
                "case_id": str(artifact.get("case_id") or ""),
                "evaluation_case_digest": str(artifact.get("evaluation_case_digest") or ""),
                "eval_spec_id": str(artifact.get("eval_spec_id") or ""),
                "capability_revision_id": str(target.get("capability_revision_id") or ""),
                "provider_binding_id": str(target.get("provider_binding_id") or ""),
            }
        )
    results: list[dict[str, Any]] = []
    for entry in capability_view.get("capabilities") or ():
        if not isinstance(entry, dict):
            continue
        capability_id = str(entry.get("capability_id") or "")
        if not capability_id:
            continue
        aggregate = _ledger_aggregate(ledger_items.get(capability_id))
        results.append(
            {
                "kind": capability_id,
                "capability": capability_id,
                "title": str(entry.get("display_name") or capability_id),
                "status": "observed" if aggregate["observation_count"] else "unobserved",
                "score": aggregate["pass_rate"],
                "observation_count": aggregate["observation_count"],
                "failure_count": aggregate["failure_count"],
                "revisions": aggregate["revisions"],
                "definition_digest": str(entry.get("definition_digest") or ""),
                "profile_requirement": entry.get("requirement") or {},
                "evaluation_targets": sorted(
                    targets_by_capability.get(capability_id) or [],
                    key=lambda item: (item["case_id"], item["provider_binding_id"]),
                ),
                "source_record_ids": [],
            }
        )
    return sorted(results, key=lambda item: str(item["capability"]))


def _ledger_aggregate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {"pass_rate": 0.0, "observation_count": 0, "failure_count": 0, "revisions": {}}
    decisive = passes = observation_count = failure_count = 0
    revisions: dict[str, Any] = {}
    for revision_id, revision in (value.get("revisions") or {}).items():
        if not isinstance(revision, dict):
            continue
        bindings = revision.get("bindings") if isinstance(revision.get("bindings"), dict) else {}
        revision_view: dict[str, Any] = {}
        for binding_id, binding in bindings.items():
            if not isinstance(binding, dict):
                continue
            count = int(binding.get("observation_count") or 0)
            binding_decisive = int(binding.get("decisive_count") or 0)
            binding_passes = int(binding.get("pass_count") or 0)
            binding_failures = int(binding.get("failure_count") or 0)
            observation_count += count
            decisive += binding_decisive
            passes += binding_passes
            failure_count += binding_failures
            revision_view[str(binding_id)] = {
                "observation_count": count,
                "pass_rate": binding.get("pass_rate"),
                "latest": binding.get("latest"),
            }
        revisions[str(revision_id)] = revision_view
    return {
        "pass_rate": round(passes / decisive, 6) if decisive else 0.0,
        "observation_count": observation_count,
        "failure_count": failure_count,
        "revisions": revisions,
    }


def _metrics(
    *,
    reflections: list[RecordEnvelope],
    incidents: list[RecordEnvelope],
    rules: list[RecordEnvelope],
    replays: list[RecordEnvelope],
    weaknesses: list[dict[str, Any]],
) -> dict[str, Any]:
    passed = sum(1 for replay in replays if str(replay.meta.get("verdict") or "").lower() == "pass")
    replay_pass_rate = round(passed / len(replays), 3) if replays else 0.0
    by_capability = Counter(str(item.get("capability") or "general") for item in weaknesses)
    return {
        "reflection_count": len(reflections),
        "incident_count": len(incidents),
        "active_rule_count": sum(1 for rule in rules if str(rule.status or "") == "active"),
        "replay_count": len(replays),
        "replay_pass_rate": replay_pass_rate,
        "weakness_count": len(weaknesses),
        "weakness_by_capability": dict(sorted(by_capability.items())),
    }


def _explicit_capability(
    record: RecordEnvelope,
    *,
    meta: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    """Read only an explicit capability declaration; never infer by prose."""

    for source in (meta, payload, record.provenance):
        for key in ("capability_id", "capability", "target_capability", "capability_domain"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return "unclassified"


def _severity(record: RecordEnvelope, primary_label: str) -> float:
    if primary_label in {"unsafe_or_high_risk", "recovery_failure"}:
        return 0.95
    if primary_label in {"user_correction", "missing_tool_call", "argument_mismatch"}:
        return 0.85
    if record.kind == "incident":
        return 0.8
    if record.kind == "unknown":
        return 0.65
    return 0.6


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _list_all(
    runtime: Any,
    *,
    kinds: list[str],
    scope: ScopeRef,
    limit: int,
    page_size: int = 500,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    while len(records) < limit:
        page = runtime.store.list_records(kinds=kinds, scope=scope, limit=min(page_size, limit - len(records)), offset=offset)
        if not page:
            break
        records.extend(page)
        offset += len(page)
    return records
