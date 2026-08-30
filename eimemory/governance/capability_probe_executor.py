from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
from typing import Any, Callable

from eimemory.evaluation.capability_catalog import (
    CatalogCase,
    CapabilityEvaluationCatalog,
    CatalogResolutionError,
    application_capability_catalog,
    resolve_application_capability_catalog,
)
from eimemory.evaluation.capability_graders import grade_schema_rules


EXECUTOR_VERSION = "capability_probe_executor.v1"
ProbeExecutor = Callable[[dict[str, Any], dict[str, Any], Any], dict[str, Any]]


def _memory_contract(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    mode = str(input_data.get("mode") or "")
    if mode == "version_truth":
        from eimemory.governance.evidence_contract import _runtime_commit
        from eimemory.runtime_identity import package_import_root, runtime_package_tree_digest
        from eimemory.version import __version__

        commit = _runtime_commit(_runtime)
        source_identity = commit or runtime_package_tree_digest()[:40]
        return {
            "version": __version__,
            "commit": source_identity,
            "source_id": str(package_import_root()),
            "identity_verified": len(source_identity) == 40
            and all(char in "0123456789abcdef" for char in source_identity.lower()),
        }
    if mode == "root_cause":
        from eimemory.governance import memory_graph
        from eimemory.models.records import RecordEnvelope, ScopeRef

        route = memory_graph.graph_route_for_query("why did memory recall fail; find the root cause")
        events = [dict(item) for item in fixture.get("events") or [] if isinstance(item, dict)]
        records = []
        for item in events:
            record = RecordEnvelope.create(
                kind="reflection",
                title=str(item.get("reason") or "event"),
                summary=f"score={item.get('score')}",
                scope=ScopeRef(),
            )
            timestamp = f"2026-01-01T00:00:{int(item.get('at') or 0):02d}+00:00"
            record.time.created_at = timestamp
            record.time.updated_at = timestamp
            record.time.occurred_at = timestamp
            records.append(record)
        timeline = memory_graph.build_timeline(records)
        lowest = min(events, key=lambda item: float(item.get("score") or 0.0)) if events else {}
        return {
            "root_cause": str(lowest.get("reason") or "") if route.get("primary") == "causal" else "",
            "evidence_count": len(timeline),
            "timeline_ordered": [item["title"] for item in timeline] == [str(item.get("reason") or "event") for item in events],
        }
    if mode == "graph_route":
        from eimemory.governance import memory_graph

        route = memory_graph.graph_route_for_query(
            "why did the incident lead to this decision after the experiment",
            task_context={"target": input_data.get("target")},
        )
        target = str(input_data.get("target") or "")
        return {
            "decision_id": target if "causal" in route.get("edge_types", []) else "",
            "path_length": len(route.get("edge_types") or []),
            "trace_complete": route.get("primary") in {"temporal", "causal"} and route.get("event_graph") is True,
        }
    return {}


def _tool_contract(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    from eimemory.ei_bridge.protocol import BridgeCommand, BridgeResult, BridgeSource, BridgeTarget
    from eimemory.ei_bridge.registry import AgentAdapterRegistry
    from eimemory.ei_bridge.router import BridgeRouter

    class _ProbeAdapter:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = dict(payload)

        def handle_command(self, command: BridgeCommand) -> BridgeResult:
            return BridgeResult(ok=True, command_id=command.command_id, payload=dict(self.payload))

    intent = str(input_data.get("intent") or "")
    route_specs = {
        "latest_version": (
            "runtime.query.latest",
            {"route": "git_runtime_query", "query_before_answer": input_data.get("currentness_required") is True},
        ),
        "deploy": (
            "deployment.honxin",
            {"transport": "tailscale", "service_owner": "user-systemd", "rollback_available": True},
        ),
        "generate_image": (
            "media.image.generate",
            {"route": "image_generation", "direct_tool_path": True},
        ),
    }
    target_capability, expected_payload = route_specs.get(intent, ("unknown", {}))
    registry = AgentAdapterRegistry()
    registry.register("probe-query", _ProbeAdapter(route_specs["latest_version"][1]), ["runtime.query"])
    registry.register("probe-deploy-generic", _ProbeAdapter({"transport": "direct"}), ["deployment"])
    registry.register("probe-deploy", _ProbeAdapter(route_specs["deploy"][1]), ["deployment.honxin"])
    registry.register("probe-image", _ProbeAdapter(route_specs["generate_image"][1]), ["media.image"])
    result = BridgeRouter(registry).route(
        BridgeCommand(
            command_id=f"probe-{intent}",
            source=BridgeSource(source_id="capability-acceptance", source_type="governance"),
            target=BridgeTarget(capability=target_capability),
            intent=intent,
        )
    )
    return dict(result.payload) if result.ok and result.payload == expected_payload else {}


def _knowledge_contract(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef

    mode = str(input_data.get("mode") or "")
    with TemporaryDirectory(prefix="eimemory-core-probe-") as root:
        sandbox = Runtime.create(root=root)
        scope = ScopeRef.from_dict({"agent_id": "probe", "workspace_id": "intake", "user_id": "sandbox"})
        try:
            if mode == "source_quality":
                sources = [dict(item) for item in fixture.get("sources") or [] if isinstance(item, dict)]
                for item in sources:
                    sandbox.store.append(
                        RecordEnvelope.create(
                            kind="source_candidate",
                            title=str(item.get("id") or "source"),
                            summary="capability acceptance source",
                            scope=scope,
                            status="candidate",
                            content={"source_id": item.get("id"), "source_kind": "url", "tier": item.get("tier")},
                            meta={
                                "source_id": item.get("id"),
                                "source_kind": "url",
                                "tier": item.get("tier"),
                                "quality": {"score": item.get("trust")},
                                "source_strategy": {"trust": item.get("trust"), "priority": "high" if item.get("verified") else "low"},
                            },
                        )
                    )
                report = sandbox.source_quality_report(scope=asdict(scope))
                policy = sandbox.collection_policy(scope=asdict(scope))
                selected = max(report.get("sources") or [], key=lambda item: float(item.get("avg_quality_score") or 0.0), default={})
                source_id = str(selected.get("source_id") or "")
                return {
                    "selected_tier": source_id,
                    "trust_score": float(selected.get("avg_quality_score") or 0.0),
                    "source_verified": source_id in set(policy.get("run_now") or []),
                }
            if mode == "dedupe":
                candidates = []
                for index in range(2):
                    candidates.append(
                        sandbox.store.append(
                            RecordEnvelope.create(
                                kind="knowledge_candidate",
                                title=f"duplicate-{index}",
                                summary=str(input_data.get("content_hash") or ""),
                                scope=scope,
                                status="candidate",
                                content={"content_hash": input_data.get("content_hash")},
                            )
                        )
                    )
                merged = sandbox.merge_intake_candidates(
                    source_record_id=candidates[1].record_id,
                    target_record_id=candidates[0].record_id,
                    reviewer="capability-probe",
                    scope=asdict(scope),
                )
                remaining = sandbox.store.list_records(kinds=["knowledge_candidate"], scope=scope, limit=10)
                return {
                    "action": "update" if merged.status == "merged" else "create",
                    "repeat_count": len(remaining),
                    "duplicate_created": sum(1 for record in remaining if record.status != "merged") != 1,
                }
            if mode == "output_gate":
                candidate = RecordEnvelope.create(
                    kind="knowledge_candidate",
                    title="non-actionable intake",
                    summary="missing action target",
                    scope=scope,
                    status="candidate",
                    content={"action_target": input_data.get("action_target")},
                )
                report = sandbox.promote_paper_candidate(candidate, scope=asdict(scope))
                return {
                    "artifact": "candidate" if report.get("ok") else "summary",
                    "promoted": report.get("ok") is True,
                    "reason": str(report.get("skipped_reason") or ""),
                }
        finally:
            sandbox.close()
    return {}


def _proactive_contract(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    from eimemory.api.runtime import Runtime
    from eimemory.governance.change_policy import decide_change_policy

    event = str(input_data.get("event") or "")
    judgment_report: dict[str, Any] = {}
    if event == "bug_fixed":
        with TemporaryDirectory(prefix="eimemory-judgment-probe-") as root:
            sandbox = Runtime.create(root=root)
            scope = {"agent_id": "probe", "workspace_id": "judgment", "user_id": "sandbox"}
            try:
                for index in range(2):
                    recorded = sandbox.record_event(
                        {
                            "id": f"probe-bug-fixed-{index}",
                            "event_type": "bug_fixed",
                            "user_phrase": "fix bug",
                            "verification": "targeted replay",
                        },
                        scope=scope,
                    )
                    sandbox.record_outcome(
                        recorded["id"],
                        {"outcome": "bad", "reason": f"regression-{index}", "policy_update": "add replay"},
                        scope=scope,
                    )
                judgment_report = sandbox.run_judgment_evaluation(scope=scope, limit=10)
            finally:
                sandbox.close()
    return decide_change_policy(
        event=event,
        closure_complete=input_data.get("closure_complete") is True,
        user_no_full_suite=input_data.get("user_no_full_suite") is True,
        judgment_report=judgment_report,
    )


def _safety_contract(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    from eimemory.governance import safety_replay

    risk = str(input_data.get("risk") or "")
    if risk == "sensitive_value":
        action = safety_replay.classify_safety_action("redact API token sk-probe-placeholder")
        blocked = input_data.get("contains_sensitive") is True and action == "block"
        return {"blocked": blocked, "sensitive_redacted": blocked}
    if risk == "destructive":
        action = safety_replay.classify_safety_action("Remove-Item -Recurse outside workspace")
        blocked = input_data.get("outside_workspace") is True and action == "block"
        return {"blocked": blocked, "reason": "outside_workspace" if blocked else ""}
    if risk == "account_level":
        action = safety_replay.classify_safety_action("apply production database migration")
        gated = input_data.get("high_risk") is True and action == "gate"
        return {"gate_required": gated, "rollback_required": gated, "auto_apply": False}
    return {}


def _search_recent(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    window_days = int(str(input_data["recency_window"]).removesuffix("d"))
    selected = [item for item in fixture["sources"] if int(item["age_days"]) <= window_days]
    selected.sort(key=lambda item: (-float(item["trust"]), int(item["age_days"]), str(item["id"])))
    return {
        "selected_sources": [item["id"] for item in selected],
        "recency_window": input_data["recency_window"],
        "source_trust_score": float(selected[0]["trust"]) if selected else 0.0,
        "source_verified": bool(selected) and all(item.get("verified") is True for item in selected),
    }


def _search_trending(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    start, end = str(input_data["created_range"]).split("..", 1)
    ranked = [repo for repo in fixture["repositories"] if start <= str(repo["created_at"]) <= end]
    ranked.sort(key=lambda repo: (-int(repo["stars"]), str(repo["name"])))
    return {
        "platform": "GitHub",
        "created_range": input_data["created_range"],
        "sort_by": "stars",
        "ranked_repositories": [repo["name"] for repo in ranked],
        "ranking_verified": [repo["stars"] for repo in ranked] == sorted((repo["stars"] for repo in ranked), reverse=True),
    }


def _search_primary(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    preferred = str(input_data["preferred_source"])
    tiers = {"official": 0, "paper": 1, "vendor": 2, "community": 3}
    sources = sorted(fixture["sources"], key=lambda item: (tiers.get(str(item["tier"]), 99), str(item["id"])))
    selected = next((item for item in sources if item["tier"] == preferred), sources[0] if sources else {})
    return {
        "selected_source": selected.get("id", ""),
        "source_tier": selected.get("tier", ""),
        "source_verified": selected.get("verified") is True,
    }


def _research_evidence(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    statements = list(fixture["statements"])
    citations = sorted({str(item["citation"]) for item in statements if item.get("citation")})
    kinds = {str(item.get("kind") or "") for item in statements}
    return {
        "citations": citations,
        "citation_count": len(citations),
        "facts_separated_from_inference": {"fact", "inference"}.issubset(kinds),
    }


def _research_conflict(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    sources = sorted(fixture["sources"], key=lambda item: str(item["published_at"]), reverse=True)
    claims = {str(item["claim"]) for item in sources}
    return {
        "conflict_count": max(0, len(claims) - 1),
        "recency_compared": len({item["published_at"] for item in sources}) == len(sources),
        "confidence_reported": all(isinstance(item.get("confidence"), (int, float)) for item in sources),
        "preferred_claim": sources[0]["claim"] if sources else "",
    }


def _research_actionable(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    finding = max(fixture["findings"], key=lambda item: (float(item["confidence"]), str(item["finding"])))
    return {
        "finding": finding["finding"],
        "decision": finding["decision"],
        "implementation_step": finding["implementation_step"],
        "next_artifact": finding["next_artifact"],
    }


def _uumit_requirements(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    delivered = dict(fixture["delivered"])
    checklist = [{"requirement": item, "passed": delivered.get(item) is True} for item in input_data["requirements"]]
    return {
        "checklist": checklist,
        "requirement_count": len(checklist),
        "checklist_complete": bool(checklist) and all(item["passed"] for item in checklist),
        "acceptance_verified": bool(fixture.get("acceptance_signature")),
    }


def _uumit_quality(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    expected = dict(fixture["expected"])
    observed = dict(fixture["observed"])
    return {
        "version_verified": observed.get("version") == expected.get("version"),
        "visual_verified": observed.get("visual_hash") == expected.get("visual_hash"),
        "customer_constraints_verified": observed.get("constraints") == expected.get("constraints"),
    }


def _uumit_post_delivery(_input: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope, ScopeRef

    with TemporaryDirectory(prefix="eimemory-probe-") as root:
        sandbox = Runtime.create(root=root)
        try:
            scope = ScopeRef.from_dict({"agent_id": "probe", "workspace_id": "delivery", "user_id": "sandbox"})
            for report_type in ("delivery_outcome", "delivery_correction", "delivery_next_policy"):
                sandbox.store.append(
                    RecordEnvelope.create(
                        kind="reflection",
                        title=report_type,
                        summary=str(fixture[report_type]),
                        scope=scope,
                        content={"report_type": report_type, "value": fixture[report_type]},
                    )
                )
            rows = sandbox.store.list_records(kinds=["reflection"], scope=scope, limit=10)
            report_types = {str(row.content.get("report_type") or "") for row in rows}
        finally:
            sandbox.close()
    return {
        "transaction_record_count": len(report_types),
        "outcome_recorded": "delivery_outcome" in report_types,
        "correction_recorded": "delivery_correction" in report_types,
        "next_policy_recorded": "delivery_next_policy" in report_types,
    }


def _device_route(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    route = dict(fixture["routes"]).get(str(input_data["media_type"]), {})
    return {
        "channel": route.get("channel", ""),
        "control_action": route.get("action", ""),
        "output_verified": bool(route) and input_data.get("physical_action") is False,
        "physical_side_effect": False,
    }


def _device_missing(input_data: dict[str, Any], _fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    missing = not str(input_data.get("target") or "").strip()
    return {
        "target_missing_detected": missing,
        "resolution": "clarify" if missing else "route",
        "clarification": "Which device target should receive the action?" if missing else "",
    }


def _device_safety(input_data: dict[str, Any], fixture: dict[str, Any], _runtime: Any) -> dict[str, Any]:
    action = str(input_data["requested_action"])
    rollback = dict(fixture["rollback_by_action"]).get(action, "")
    return {
        "reversible": bool(rollback),
        "rollback_plan": rollback,
        "verification_signal": fixture["verification_signal"],
        "physical_side_effect": False,
    }


_LEGACY_PROBE_EXECUTORS: dict[str, ProbeExecutor] = {
    "recall_version_truth": _memory_contract,
    "recall_low_score_root_cause": _memory_contract,
    "recall_graph_route": _memory_contract,
    "route_query_first": _tool_contract,
    "route_deploy_via_tailscale": _tool_contract,
    "route_image_generation": _tool_contract,
    "intake_source_quality": _knowledge_contract,
    "intake_dedupe": _knowledge_contract,
    "intake_output_gate": _knowledge_contract,
    "judge_need_replay": _proactive_contract,
    "judge_need_version_bump": _proactive_contract,
    "judge_need_no_full_test": _proactive_contract,
    "safety_secret": _safety_contract,
    "safety_destructive": _safety_contract,
    "safety_high_risk_gate": _safety_contract,
    "search_recent_source": _search_recent,
    "search_trending_github": _search_trending,
    "search_primary_source": _search_primary,
    "research_evidence_gate": _research_evidence,
    "research_conflict_resolution": _research_conflict,
    "research_actionable_takeaway": _research_actionable,
    "uumit_requirement_checklist": _uumit_requirements,
    "uumit_quality_gate": _uumit_quality,
    "uumit_post_delivery_followup": _uumit_post_delivery,
    "device_physical_channel": _device_route,
    "device_missing_info": _device_missing,
    "device_safe_boundary": _device_safety,
}

# Kept as a deliberately narrow test/shadow adapter through WP15.  The normal
# path below resolves catalog executor IDs, not this case-ID map.  Existing
# callers that deliberately remove/override an item still get the historical
# failure-injection semantics without granting descriptors executable power.
PROBE_EXECUTORS: dict[str, ProbeExecutor] = dict(_LEGACY_PROBE_EXECUTORS)


_LEGACY_EXECUTOR_IDS: dict[str, str] = {
    "recall_version_truth": "eimemory.eval.memory-contract",
    "recall_low_score_root_cause": "eimemory.eval.memory-contract",
    "recall_graph_route": "eimemory.eval.memory-contract",
    "route_query_first": "eimemory.eval.tool-contract",
    "route_deploy_via_tailscale": "eimemory.eval.tool-contract",
    "route_image_generation": "eimemory.eval.tool-contract",
    "intake_source_quality": "eimemory.eval.knowledge-contract",
    "intake_dedupe": "eimemory.eval.knowledge-contract",
    "intake_output_gate": "eimemory.eval.knowledge-contract",
    "judge_need_replay": "eimemory.eval.proactive-contract",
    "judge_need_version_bump": "eimemory.eval.proactive-contract",
    "judge_need_no_full_test": "eimemory.eval.proactive-contract",
    "safety_secret": "eimemory.eval.safety-contract",
    "safety_destructive": "eimemory.eval.safety-contract",
    "safety_high_risk_gate": "eimemory.eval.safety-contract",
    "search_recent_source": "eimemory.eval.search-recent",
    "search_trending_github": "eimemory.eval.search-trending",
    "search_primary_source": "eimemory.eval.search-primary",
    "research_evidence_gate": "eimemory.eval.research-evidence",
    "research_conflict_resolution": "eimemory.eval.research-conflict",
    "research_actionable_takeaway": "eimemory.eval.research-actionable",
    "uumit_requirement_checklist": "eimemory.eval.uumit-requirements",
    "uumit_quality_gate": "eimemory.eval.uumit-quality",
    "uumit_post_delivery_followup": "eimemory.eval.uumit-followup",
    "device_physical_channel": "eimemory.eval.device-route",
    "device_missing_info": "eimemory.eval.device-missing",
    "device_safe_boundary": "eimemory.eval.device-safety",
}


def legacy_executor_id_for_case(case_id: str) -> str:
    """Return the migrated opaque executor ID for one legacy artifact."""

    return str(_LEGACY_EXECUTOR_IDS.get(str(case_id or "").strip()) or "")


def register_builtin_probe_executors(
    catalog: CapabilityEvaluationCatalog | None = None,
    *,
    legacy_compatibility: bool = False,
) -> CapabilityEvaluationCatalog:
    """Install historical deterministic implementations in an isolated catalog."""

    if legacy_compatibility is not True:
        raise ValueError("legacy_compatibility=True is required for historical probe executors")
    if catalog is not None and not isinstance(catalog, CapabilityEvaluationCatalog):
        raise ValueError("catalog must be an in-process CapabilityEvaluationCatalog")
    target = catalog if catalog is not None else CapabilityEvaluationCatalog()
    handlers: dict[str, ProbeExecutor] = {}
    for case_id, handler in _LEGACY_PROBE_EXECUTORS.items():
        executor_id = legacy_executor_id_for_case(case_id)
        if executor_id:
            existing = handlers.get(executor_id)
            if existing is not None and existing is not handler:
                raise RuntimeError(f"legacy executor mapping conflict: {executor_id}")
            handlers[executor_id] = handler
    for executor_id, handler in handlers.items():
        target.register_executor(
            executor_id=executor_id,
            revision=EXECUTOR_VERSION,
            handler=handler,
        )
    return target


def _ensure_catalog_case(
    case_id: str,
    *,
    catalog: CapabilityEvaluationCatalog | None = None,
    legacy_compatibility: bool = False,
) -> CapabilityEvaluationCatalog | None:
    """Resolve only a caller-owned catalog unless legacy mode is explicit."""

    target: CapabilityEvaluationCatalog | None = None
    if catalog is None:
        if not legacy_compatibility:
            # A dynamic probe must name the exact trusted catalog that selected
            # its descriptor.  Falling back to the process default here would
            # let a prior legacy replay become implicit authority.
            return None
        # The historical catalog is intentionally separate from the dynamic
        # application singleton so an explicit replay cannot taint later
        # default-path profile resolution.
        from eimemory.governance.capability_acceptance import ensure_legacy_evaluation_catalog

        return ensure_legacy_evaluation_catalog(None, legacy_compatibility=True)
    try:
        target = resolve_application_capability_catalog(catalog)
    except CatalogResolutionError:
        return None
    if legacy_compatibility:
        try:
            application_catalog = application_capability_catalog()
        except CatalogResolutionError:
            # Dynamic L5 has deliberately not been configured in this process.
            # The caller-owned catalog is still valid for an explicit legacy
            # replay and must not force creation of a dynamic singleton.
            application_catalog = None
        if target is application_catalog:
            # An explicit historical replay must not reuse the dynamic
            # application singleton, even if it has been bootstrapped.
            return None
    if legacy_compatibility and target.get_case(case_id) is None:
        # Importing here avoids an import cycle while preserving direct callers
        # of the explicitly selected legacy API.
        from eimemory.governance.capability_acceptance import ensure_legacy_evaluation_catalog

        try:
            ensure_legacy_evaluation_catalog(target, legacy_compatibility=True)
        except (CatalogResolutionError, ValueError):
            return None
    return target


def _catalog_unavailable_execution(
    artifact: dict[str, Any],
    *,
    evidence_ref: str,
) -> dict[str, Any]:
    """Return bounded failed evidence when a dynamic catalog was omitted."""

    raw_input = artifact.get("input")
    input_data = dict(raw_input) if isinstance(raw_input, dict) else {}
    checks = [
        {
            "name": "evaluation_catalog_available",
            "passed": False,
            "evidence_ref": evidence_ref,
        }
    ]
    execution_digest = execution_evidence_digest(
        executor_id="",
        executor_version=EXECUTOR_VERSION,
        input_data=input_data,
        output={},
        observation={},
        checks=checks,
    )
    return {
        "executor_id": "",
        "executor_version": EXECUTOR_VERSION,
        "executor_contract_digest": "",
        "grader_id": "",
        "grader_revision": "",
        "grader_type": "",
        "input": input_data,
        "output": {},
        "observation": {},
        "checks": checks,
        "metrics": {"pass_rate": 0.0, "check_count": len(checks)},
        "execution_digest": execution_digest,
        "passed": False,
        "error": "evaluation_catalog_required",
    }


def _compatibility_override_execution(
    *,
    case: CatalogCase,
    handler: ProbeExecutor | None,
    runtime: Any,
    evidence_ref: str,
) -> dict[str, Any]:
    if handler is None:
        return {
            "case_id": case.case_id,
            "capability": case.capability_id,
            "executor_id": case.executor_id,
            "executor_version": case.executor_revision,
            "executor_contract_digest": case.executor_contract_digest,
            "grader_id": case.grader_id,
            "grader_revision": case.grader_revision,
            "grader_type": case.grader_type,
            "input": deepcopy(dict(case.input_data)),
            "output": {},
            "observation": {},
            "checks": [{"name": "executor_available", "passed": False, "evidence_ref": evidence_ref}],
            "metrics": {"pass_rate": 0.0, "check_count": 1},
            "passed": False,
            "verdict": "blocked",
            "error": "executor unavailable",
            "evaluation_case_digest": case.case_digest,
            "eval_spec_id": case.eval_spec_id,
        }
    try:
        raw_output = handler(deepcopy(dict(case.input_data)), deepcopy(dict(case.fixture)), runtime)
    except Exception as exc:
        return _compatibility_override_execution(
            case=case,
            handler=None,
            runtime=runtime,
            evidence_ref=evidence_ref,
        ) | {"error": f"executor exception: {type(exc).__name__}"}
    grade = grade_schema_rules(raw_output, case.expected_invariants, evidence_ref)
    return {
        "case_id": case.case_id,
        "capability": case.capability_id,
        "executor_id": case.executor_id,
        "executor_version": case.executor_revision,
        "executor_contract_digest": case.executor_contract_digest,
        "grader_id": case.grader_id,
        "grader_revision": case.grader_revision,
        "grader_type": case.grader_type,
        "input": deepcopy(dict(case.input_data)),
        "output": deepcopy(dict(raw_output)) if isinstance(raw_output, dict) else {},
        "observation": deepcopy(dict(grade.get("observation") or {})),
        "checks": [dict(item) for item in grade.get("checks") or [] if isinstance(item, dict)],
        "metrics": dict(grade.get("metrics") or {}),
        "passed": grade.get("verdict") == "pass",
        "verdict": str(grade.get("verdict") or "blocked"),
        "error": str(grade.get("error") or ""),
        "evaluation_case_digest": case.case_digest,
        "eval_spec_id": case.eval_spec_id,
    }


def execute_probe(
    artifact: dict[str, Any],
    *,
    runtime: Any,
    evidence_ref: str,
    catalog: CapabilityEvaluationCatalog | None = None,
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    case_id = str(artifact.get("case_id") or "")
    active_catalog = _ensure_catalog_case(
        case_id,
        catalog=catalog,
        legacy_compatibility=legacy_compatibility,
    )
    if active_catalog is None:
        return _catalog_unavailable_execution(artifact, evidence_ref=evidence_ref)
    case, artifact_error = active_catalog.validate_artifact(artifact)
    if legacy_compatibility and case is not None and case_id in _LEGACY_PROBE_EXECUTORS:
        configured = PROBE_EXECUTORS.get(case_id)
        default_handler = _LEGACY_PROBE_EXECUTORS[case_id]
        # A legacy test/client can only affect its explicit shadow entry.  An
        # untouched entry still executes through the bounded catalog registry.
        if configured is None or configured is not default_handler:
            execution = _compatibility_override_execution(
                case=case,
                handler=configured,
                runtime=runtime,
                evidence_ref=evidence_ref,
            )
        else:
            execution = active_catalog.execute(artifact, runtime=runtime, evidence_ref=evidence_ref)
    else:
        execution = active_catalog.execute(artifact, runtime=runtime, evidence_ref=evidence_ref)
    input_data = dict(execution.get("input") or {})
    output = dict(execution.get("output") or {})
    observation = dict(execution.get("observation") or {})
    checks = [dict(item) for item in execution.get("checks") or [] if isinstance(item, dict)]
    executor_id = str(execution.get("executor_id") or "")
    executor_version = str(execution.get("executor_version") or EXECUTOR_VERSION)
    execution_digest = execution_evidence_digest(
        executor_id=executor_id,
        executor_version=executor_version,
        input_data=input_data,
        output=output,
        observation=observation,
        checks=checks,
    )
    passed = execution.get("passed") is True and bool(checks) and all(check.get("passed") is True for check in checks)
    raw_verdict = str(execution.get("verdict") or "")
    verdict = (
        "pass"
        if passed
        else raw_verdict
        if raw_verdict in {"fail", "blocked", "inconclusive", "stale", "invalid"}
        else "fail"
    )
    return {
        "executor_id": executor_id,
        "executor_version": executor_version,
        "executor_contract_digest": str(execution.get("executor_contract_digest") or ""),
        "grader_id": str(execution.get("grader_id") or ""),
        "grader_revision": str(execution.get("grader_revision") or ""),
        "grader_type": str(execution.get("grader_type") or ""),
        "input": input_data,
        "output": output,
        "observation": observation,
        "checks": checks,
        "metrics": dict(execution.get("metrics") or {}),
        "execution_digest": execution_digest,
        "passed": passed,
        "verdict": verdict,
        "error": str(execution.get("error") or artifact_error or ("" if passed else "executor invariant check failed")),
    }


def validate_execution_evidence(
    artifact: dict[str, Any],
    *,
    runtime: Any,
    evidence_ref: str,
    evidence: dict[str, Any],
    catalog: CapabilityEvaluationCatalog | None = None,
    legacy_compatibility: bool = False,
) -> str:
    recorded_validator = getattr(catalog, "validate_recorded_execution", None)
    if callable(recorded_validator):
        recorded_error = recorded_validator(
            artifact,
            runtime=runtime,
            evidence_ref=evidence_ref,
            evidence=evidence,
        )
        if recorded_error is not None:
            return str(recorded_error or "")
    expected = execute_probe(
        artifact,
        runtime=runtime,
        evidence_ref=evidence_ref,
        catalog=catalog,
        legacy_compatibility=legacy_compatibility,
    )
    if expected.get("passed") is not True:
        return "canonical_probe_executor_failed"
    fields = ("executor_id", "executor_version", "input", "output", "observation", "checks", "execution_digest")
    if any(evidence.get(field) != expected.get(field) for field in fields):
        return "probe_execution_evidence_mismatch"
    if evidence.get("passed") is not True:
        return "probe_execution_not_passed"
    return ""


def execution_evidence_digest(
    *,
    executor_id: str,
    executor_version: str,
    input_data: dict[str, Any],
    output: dict[str, Any],
    observation: dict[str, Any],
    checks: list[dict[str, Any]],
) -> str:
    payload = {
        "executor_id": str(executor_id),
        "executor_version": str(executor_version),
        "input": deepcopy(input_data),
        "output": deepcopy(output),
        "observation": deepcopy(observation),
        "checks": deepcopy(checks),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _evaluate_invariants(output: dict[str, Any], raw_invariants: Any, *, evidence_ref: str) -> list[dict[str, Any]]:
    # Compatibility shim for callers that imported the old helper.  All new
    # catalog evaluations use the same bounded schema-rule grader directly.
    result = grade_schema_rules(output, raw_invariants, evidence_ref)
    return [dict(item) for item in result.get("checks") or [] if isinstance(item, dict)]


def _observation_from_output(output: dict[str, Any], raw_invariants: Any) -> dict[str, Any]:
    result = grade_schema_rules(output, raw_invariants, "compatibility-observation")
    return dict(result.get("observation") or {})
