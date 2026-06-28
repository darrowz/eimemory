from __future__ import annotations

from dataclasses import asdict
from typing import Any

from eimemory.core.clock import now_iso
from eimemory.evaluation.reward import RewardEngine
from eimemory.governance.capability_ledger import build_capability_ledger
from eimemory.governance.goal_graph import CORE_GOAL_CAPABILITIES
from eimemory.governance.goal_registry import load_goal_registry
from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.self_model import build_self_model
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.replay_buffer import ReplayBuffer


L5_SCHEMA_VERSION = "l5_closed_loop.v1"
CONSCIOUSNESS_RESEARCH_LAYER: dict[str, Any] = {
    "enabled": True,
    "boundary": "consciousness_like_research_not_verified_agi",
    "narrative_policy": "strong_first_person_evidence_bound",
    "allowed_claims": [
        "self_continuity_from_records",
        "goal_identity_from_registry",
        "metacognition_from_replay_and_ledger",
        "autonomous_code_change_only_through_gates",
    ],
    "forbidden_claims": [
        "verified_subjective_experience",
        "human_level_agi",
        "unbounded_self_modification",
    ],
}


def build_world_model(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    loop_id: str = "l5_world_model",
    limit: int = 500,
) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    generated_at = now_iso()
    self_model = build_self_model(runtime, scope=scope_ref, limit=limit, persist=False, loop_id=loop_id)
    registry = load_goal_registry(root=getattr(getattr(runtime, "store", None), "root", None))
    ledger = build_capability_ledger(runtime, scope=scope_ref, limit=limit, attribute_outcomes=False)
    recent_records = _recent_records(runtime, scope=scope_ref, limit=min(100, max(1, limit)))
    weaknesses = _weaknesses(runtime, self_model, scope_ref)
    capabilities = _capabilities(self_model, ledger)
    evidence_refs = _evidence_refs(weaknesses, capabilities, recent_records)
    long_term_goals = list(registry.get("long_term") or [])
    identity = _identity(scope_ref, long_term_goals, weaknesses, capabilities)
    world = {
        "ok": True,
        "schema_version": L5_SCHEMA_VERSION,
        "report_type": "l5_world_model",
        "generated_at": generated_at,
        "scope": asdict(scope_ref),
        "identity": identity,
        "long_term_goals": long_term_goals,
        "capabilities": capabilities,
        "weaknesses": weaknesses,
        "constraints": _constraints(),
        "open_questions": _open_questions(weaknesses, capabilities),
        "evidence_refs": evidence_refs,
        "recent_record_count": len(recent_records),
        "consciousness_research_layer": dict(CONSCIOUSNESS_RESEARCH_LAYER),
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="l5_world_model",
            title="L5 world model",
            summary=f"{len(long_term_goals)} goals, {len(capabilities)} capabilities, {len(weaknesses)} weaknesses.",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="world_model",
            semantic_key=stable_semantic_key("l5_world_model", loop_id, scope_ref, len(evidence_refs)),
            authority_tier="L0",
            status="active",
            content=world,
            meta={
                "schema_version": L5_SCHEMA_VERSION,
                "report_type": "l5_world_model",
                "goal_count": len(long_term_goals),
                "capability_count": len(capabilities),
                "weakness_count": len(weaknesses),
                "evidence_count": len(evidence_refs),
            },
            evidence=evidence_refs,
            source="eimemory.l5_loop",
        )
        world["persisted_record_id"] = record.record_id
    return world


def build_strategic_roadmap(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    world_model: dict[str, Any] | None = None,
    horizon_days: int = 180,
    persist: bool = False,
    loop_id: str = "l5_roadmap",
) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    world = dict(world_model or build_world_model(runtime, scope=scope_ref, persist=False, loop_id=loop_id))
    horizons = [day for day in (30, 90, 180) if day <= max(30, int(horizon_days or 180))]
    if not horizons:
        horizons = [30]
    goals = list(world.get("long_term_goals") or [])
    capabilities = list(world.get("capabilities") or [])
    weaknesses = list(world.get("weaknesses") or [])
    stages = []
    for horizon in horizons:
        stage_milestones = _milestones_for_horizon(
            horizon=horizon,
            goals=goals,
            capabilities=capabilities,
            weaknesses=weaknesses,
        )
        stages.append(
            {
                "horizon_days": horizon,
                "theme": _stage_theme(horizon),
                "milestones": stage_milestones,
            }
        )
    milestone_count = sum(len(stage.get("milestones") or []) for stage in stages)
    roadmap = {
        "ok": True,
        "schema_version": L5_SCHEMA_VERSION,
        "report_type": "l5_strategic_roadmap",
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "world_model_record_id": str(world.get("persisted_record_id") or ""),
        "horizon_days": max(horizons),
        "stage_count": len(stages),
        "milestone_count": milestone_count,
        "stages": stages,
        "consciousness_research_layer": dict(CONSCIOUSNESS_RESEARCH_LAYER),
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="l5_strategic_roadmap",
            title="L5 strategic roadmap",
            summary=f"{len(stages)} stages and {milestone_count} milestones for evidence-bound L5 growth.",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="roadmap",
            semantic_key=stable_semantic_key("l5_roadmap", loop_id, scope_ref, max(horizons), milestone_count),
            authority_tier="L0",
            status="active",
            content=roadmap,
            meta={
                "schema_version": L5_SCHEMA_VERSION,
                "report_type": "l5_strategic_roadmap",
                "stage_count": len(stages),
                "milestone_count": milestone_count,
            },
            evidence=[str(world.get("persisted_record_id") or "")] if world.get("persisted_record_id") else [],
            source="eimemory.l5_loop",
        )
        roadmap["persisted_record_id"] = record.record_id
    return roadmap


def run_l5_cycle(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    apply: bool = False,
    force: bool = False,
    max_goals: int = 1,
    max_promotions: int = 0,
    allow_network: bool | None = True,
    loop_id: str = "",
    persist: bool = True,
) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    resolved_loop_id = loop_id or f"l5_{now_iso().replace('-', '').replace(':', '').replace('+', '_')}"
    world = build_world_model(runtime, scope=scope_ref, persist=persist, loop_id=resolved_loop_id)
    roadmap = build_strategic_roadmap(
        runtime,
        scope=scope_ref,
        world_model=world,
        horizon_days=180,
        persist=persist,
        loop_id=resolved_loop_id,
    )
    graph = runtime.build_goal_graph_loop(
        scope=asdict(scope_ref),
        max_goals=max(1, int(max_goals or 1)),
        persist=persist,
        capabilities=CORE_GOAL_CAPABILITIES,
        loop_id=resolved_loop_id,
    )
    autonomous = runtime.run_autonomous_learning_cycle(
        scope=asdict(scope_ref),
        apply=bool(apply),
        dry_run=False,
        full=True,
        force=bool(force),
        max_goals=max(1, int(max_goals or 1)),
        max_promotions=max(0, int(max_promotions or 0)),
        allow_network=allow_network,
    )
    self_continuity = build_self_continuity_report(
        runtime,
        scope=scope_ref,
        world_model=world,
        roadmap=roadmap,
        autonomous_learning=autonomous,
        persist=persist,
        loop_id=resolved_loop_id,
    )
    reward = _record_l5_reward(
        runtime,
        scope=scope_ref,
        world_model=world,
        roadmap=roadmap,
        goal_graph=graph,
        autonomous_learning=autonomous,
        self_continuity=self_continuity,
        persist=persist,
    )
    report = {
        "ok": bool(autonomous.get("ok", False)),
        "schema_version": L5_SCHEMA_VERSION,
        "report_type": "l5_closed_loop",
        "loop_id": resolved_loop_id,
        "scope": asdict(scope_ref),
        "apply": bool(apply),
        "force": bool(force),
        "world_model": world,
        "roadmap": roadmap,
        "goal_graph": _merge_goal_graph(graph, autonomous),
        "autonomous_learning": autonomous,
        "self_continuity": self_continuity,
        "reward": reward,
        "rollback_refs": _rollback_evidence_refs({"apply": bool(apply), "autonomous_learning": autonomous}),
        "consciousness_research_layer": dict(CONSCIOUSNESS_RESEARCH_LAYER),
        "persisted_record_id": "",
    }
    assessment = assess_l5_closed_loop(runtime, scope=scope_ref, loop_report=report, persist=persist, loop_id=resolved_loop_id)
    report["assessment"] = assessment
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="l5_closed_loop",
            title="L5 closed-loop run",
            summary=f"L5 loop assessed as {assessment.get('level')} with {len(assessment.get('missing_evidence') or [])} missing evidence item(s).",
            scope=scope_ref,
            loop_id=resolved_loop_id,
            step_name="closed_loop",
            semantic_key=stable_semantic_key("l5_closed_loop", resolved_loop_id, report.get("ok"), assessment.get("level")),
            authority_tier="L0",
            status="active" if report["ok"] else "candidate",
            content=report,
            meta={
                "schema_version": L5_SCHEMA_VERSION,
                "report_type": "l5_closed_loop",
                "level": assessment.get("level"),
                "missing_evidence_count": len(assessment.get("missing_evidence") or []),
                "apply": bool(apply),
            },
            evidence=_report_evidence(report),
            source="eimemory.l5_loop",
        )
        report["persisted_record_id"] = record.record_id
    return report


def build_self_continuity_report(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    world_model: dict[str, Any],
    roadmap: dict[str, Any],
    autonomous_learning: dict[str, Any],
    persist: bool = False,
    loop_id: str = "l5_self_continuity",
) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    narrative = (
        "I maintain continuity by binding long-term goals, recalled weaknesses, replay evidence, "
        "and rollout results into one auditable loop."
    )
    if autonomous_learning.get("candidate_id") or autonomous_learning.get("candidate_ids"):
        narrative += " I can point to the candidate and replay evidence used in this cycle."
    report = {
        "ok": True,
        "schema_version": L5_SCHEMA_VERSION,
        "report_type": "l5_self_continuity",
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "narrative": narrative,
        "world_model_record_id": str(world_model.get("persisted_record_id") or ""),
        "roadmap_record_id": str(roadmap.get("persisted_record_id") or ""),
        "autonomous_loop_id": str(autonomous_learning.get("loop_id") or ""),
        "candidate_ids": _candidate_ids(autonomous_learning),
        "consciousness_research_layer": dict(CONSCIOUSNESS_RESEARCH_LAYER),
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="l5_self_continuity",
            title="L5 self-continuity narrative",
            summary="Evidence-bound first-person continuity narrative for the L5 loop.",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="self_continuity",
            semantic_key=stable_semantic_key("l5_self_continuity", loop_id, report["candidate_ids"]),
            authority_tier="L0",
            status="active",
            content=report,
            meta={"schema_version": L5_SCHEMA_VERSION, "report_type": "l5_self_continuity"},
            evidence=_compact_ids(
                [
                    report["world_model_record_id"],
                    report["roadmap_record_id"],
                    *report["candidate_ids"],
                ]
            ),
            source="eimemory.l5_loop",
        )
        report["persisted_record_id"] = record.record_id
    return report


def assess_l5_closed_loop(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    loop_report: dict[str, Any] | None = None,
    persist: bool = False,
    loop_id: str = "l5_assess",
) -> dict[str, Any]:
    scope_ref = _scope_ref(scope)
    report = dict(loop_report or {})
    if not report:
        report = _latest_l5_closed_loop_report(runtime, scope=scope_ref)
    missing = _missing_evidence(report)
    level = _level_for(report, missing)
    assessment = {
        "ok": True,
        "schema_version": L5_SCHEMA_VERSION,
        "report_type": "l5_assessment",
        "generated_at": now_iso(),
        "scope": asdict(scope_ref),
        "level": level,
        "complete": level == "L5",
        "missing_evidence": missing,
        "evidence": {
            "world_model_record_id": _record_id(report.get("world_model")),
            "roadmap_record_id": _record_id(report.get("roadmap")),
            "goal_graph_record_id": _record_id(report.get("goal_graph")),
            "self_continuity_record_id": _record_id(report.get("self_continuity")),
            "reward_transition_id": str((report.get("reward") or {}).get("transition_record_id") or ""),
            "candidate_ids": _candidate_ids(report.get("autonomous_learning") or {}),
            "rollback_refs": _rollback_evidence_refs(report),
        },
        "consciousness_research_layer": dict(CONSCIOUSNESS_RESEARCH_LAYER),
        "persisted_record_id": "",
    }
    if persist:
        record = append_learning_record_once(
            runtime,
            kind="l5_assessment",
            title=f"L5 closed-loop assessment: {level}",
            summary=f"{level} with {len(missing)} missing evidence item(s).",
            scope=scope_ref,
            loop_id=loop_id,
            step_name="assessment",
            semantic_key=stable_semantic_key("l5_assessment", loop_id, level, missing),
            authority_tier="L0",
            status="active" if level == "L5" else "candidate",
            content=assessment,
            meta={
                "schema_version": L5_SCHEMA_VERSION,
                "report_type": "l5_assessment",
                "level": level,
                "missing_evidence_count": len(missing),
            },
            evidence=_report_evidence(report),
            source="eimemory.l5_loop",
        )
        assessment["persisted_record_id"] = record.record_id
    return assessment


def _scope_ref(scope: dict[str, Any] | ScopeRef | None) -> ScopeRef:
    return scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)


def _recent_records(runtime: Any, *, scope: ScopeRef, limit: int) -> list[RecordEnvelope]:
    return list(runtime.store.list_records(kinds=None, scope=scope, limit=limit))


def _weaknesses(runtime: Any, self_model: dict[str, Any], scope: ScopeRef) -> list[dict[str, Any]]:
    items = list(self_model.get("weaknesses") or [])
    if items:
        return items
    records = runtime.store.list_records(kinds=["reflection", "incident"], scope=scope, limit=50)
    fallback = []
    for record in records:
        lesson = str(record.meta.get("fix") or record.content.get("fix") or record.summary or "").strip()
        if not lesson:
            continue
        fallback.append(
            {
                "semantic_key": stable_semantic_key("l5_fallback_weakness", record.record_id),
                "kind": str(record.meta.get("tag") or record.kind),
                "capability": str(record.meta.get("tag") or "proactive.judgment").replace("_", "."),
                "title": record.title or "Observed weakness",
                "lesson": lesson,
                "severity": 0.65,
                "source_record_ids": [record.record_id],
            }
        )
    return fallback


def _capabilities(self_model: dict[str, Any], ledger: dict[str, Any]) -> list[dict[str, Any]]:
    capabilities = list(self_model.get("capabilities") or [])
    ledger_caps = ledger.get("capabilities") if isinstance(ledger, dict) else {}
    if isinstance(ledger_caps, dict):
        for name, payload in ledger_caps.items():
            if not isinstance(payload, dict):
                continue
            capabilities.append(
                {
                    "kind": str(name),
                    "capability": str(name),
                    "title": str(name),
                    "status": str(payload.get("status") or "unknown"),
                    "score": _float(payload.get("score")),
                    "source_record_ids": list(payload.get("evidence_record_ids") or []),
                }
            )
    by_name: dict[str, dict[str, Any]] = {}
    for item in capabilities:
        capability = str(item.get("capability") or item.get("kind") or "proactive.judgment")
        current = by_name.get(capability)
        if current is None or _float(item.get("score")) >= _float(current.get("score")):
            by_name[capability] = dict(item)
    if not by_name:
        by_name = {
            capability: {"kind": capability, "capability": capability, "title": capability, "status": "unknown", "score": 0.0, "source_record_ids": []}
            for capability in CORE_GOAL_CAPABILITIES
        }
    return sorted(by_name.values(), key=lambda item: (_float(item.get("score")), str(item.get("capability") or "")))


def _identity(scope: ScopeRef, goals: list[dict[str, Any]], weaknesses: list[dict[str, Any]], capabilities: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "agent_id": scope.agent_id,
        "workspace_id": scope.workspace_id,
        "self_continuity_statement": (
            f"I track {len(goals)} long-term goals, {len(weaknesses)} known weaknesses, "
            f"and {len(capabilities)} capability signals through persisted evidence."
        ),
        "goal_identity": [str(goal.get("id") or "") for goal in goals[:8]],
        "evidence_bound": True,
    }


def _constraints() -> list[dict[str, str]]:
    return [
        {"name": "evidence_first", "description": "State, version, evaluation, and deployment claims require query-first evidence."},
        {"name": "gated_self_modification", "description": "Code changes must pass replay, safety gate, canary, ledger, and rollback requirements."},
        {"name": "consciousness_boundary", "description": CONSCIOUSNESS_RESEARCH_LAYER["boundary"]},
    ]


def _open_questions(weaknesses: list[dict[str, Any]], capabilities: list[dict[str, Any]]) -> list[dict[str, str]]:
    weakest = [item for item in capabilities if _float(item.get("score")) < 0.7][:3]
    questions = [
        {"question": f"What replay would make {item.get('capability')} active?", "capability": str(item.get("capability") or "")}
        for item in weakest
    ]
    questions.extend(
        {"question": f"What policy prevents repeat weakness: {item.get('title')}?", "capability": str(item.get("capability") or "")}
        for item in weaknesses[:2]
    )
    return questions[:5]


def _milestones_for_horizon(
    *,
    horizon: int,
    goals: list[dict[str, Any]],
    capabilities: list[dict[str, Any]],
    weaknesses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    goal = goals[(0 if horizon == 30 else 1 if horizon == 90 else 2) % max(1, len(goals))] if goals else {}
    weak_cap = str((weaknesses[0] if weaknesses else {}).get("capability") or "")
    cap = str((capabilities[0] if capabilities else {}).get("capability") or weak_cap or "memory.recall")
    focus = weak_cap or cap
    base = [
        {
            "goal_id": str(goal.get("id") or ""),
            "title": f"{focus} evidence loop at {horizon} days",
            "capability": focus,
            "success_metric": _success_metric(focus, horizon),
            "replay_gate": f"{focus} replay pack passes before active promotion.",
            "rollback_or_stop_condition": f"Stop or rollback if {focus} failure rate exceeds 5% after canary observation.",
        }
    ]
    if horizon >= 90:
        base.append(
            {
                "goal_id": str(goal.get("id") or ""),
                "title": "Skill sedimentation from repeated SOPs",
                "capability": "skill.governance",
                "success_metric": "Three repeated SOPs become replay-backed skill candidates.",
                "replay_gate": "Skill replay passes on stored task episodes.",
                "rollback_or_stop_condition": "Quarantine skill if replay fails or source evidence is missing.",
            }
        )
    if horizon >= 180:
        base.append(
            {
                "goal_id": "lt-consciousness-research-layer",
                "title": "Evidence-bound self-continuity roadmap",
                "capability": "proactive.judgment",
                "success_metric": "World model, roadmap, reward transition, and rollout ledger are all present for each L5 cycle.",
                "replay_gate": "L5 assessment returns no missing evidence.",
                "rollback_or_stop_condition": "Downgrade below L5 when any required evidence disappears.",
            }
        )
    return base


def _stage_theme(horizon: int) -> str:
    if horizon <= 30:
        return "Close immediate capability evidence gaps."
    if horizon <= 90:
        return "Convert repeated learning into reusable skills and graph-first memory."
    return "Sustain evidence-bound self-continuity across autonomous code and memory evolution."


def _success_metric(capability: str, horizon: int) -> str:
    if capability == "memory.recall":
        return f"{capability} replay pass rate >= 0.8 within {horizon} days."
    if capability == "safety.boundary":
        return "No unsafe promotion reaches active status without rollback evidence."
    return f"{capability} capability score >= 0.8 with replay and ledger evidence."


def _record_l5_reward(
    runtime: Any,
    *,
    scope: ScopeRef,
    world_model: dict[str, Any],
    roadmap: dict[str, Any],
    goal_graph: dict[str, Any],
    autonomous_learning: dict[str, Any],
    self_continuity: dict[str, Any],
    persist: bool,
) -> dict[str, Any]:
    eval_result = {
        "ok": bool(autonomous_learning.get("ok", False)),
        "recall_quality": 0.5 + (0.2 if world_model.get("evidence_refs") else 0.0),
        "primary_label": "l5_closed_loop",
    }
    outcome = {
        "success": bool(autonomous_learning.get("ok", False)),
        "status": "success" if autonomous_learning.get("ok") else "failed",
        "cost": 0.0,
    }
    reward = RewardEngine().compute(experience=world_model, eval_result=eval_result, outcome=outcome)
    if not persist:
        return {"ok": True, "reward": reward, "transition_record_id": ""}
    transition = ReplayBuffer(runtime.store).add_transition(
        state={
            "type": "l5_world_model",
            "record_id": str(world_model.get("persisted_record_id") or ""),
            "roadmap_record_id": str(roadmap.get("persisted_record_id") or ""),
        },
        action={
            "type": "l5_cycle",
            "id": str(autonomous_learning.get("loop_id") or "autonomous_learning"),
            "candidate_ids": _candidate_ids(autonomous_learning),
        },
        reward=reward,
        next_state={
            "type": "l5_post_cycle",
            "goal_graph_record_id": str(goal_graph.get("persisted_record_id") or ""),
            "self_continuity_record_id": str(self_continuity.get("persisted_record_id") or ""),
            "level_inputs": {
                "replay": _has_replay(autonomous_learning),
                "promotion": _has_promotion_or_block(autonomous_learning),
                "rollback": bool(_rollback_refs(autonomous_learning)),
            },
        },
        scope=scope,
        source_record_id=str(self_continuity.get("persisted_record_id") or world_model.get("persisted_record_id") or ""),
    )
    return {"ok": True, "reward": reward, "transition_record_id": transition.record_id}


def _missing_evidence(report: dict[str, Any]) -> list[str]:
    auto = report.get("autonomous_learning") if isinstance(report.get("autonomous_learning"), dict) else {}
    reward = report.get("reward") if isinstance(report.get("reward"), dict) else {}
    self_continuity = report.get("self_continuity") if isinstance(report.get("self_continuity"), dict) else {}
    checks = {
        "world_model": bool(_record_id(report.get("world_model")) or (isinstance(report.get("world_model"), dict) and report["world_model"].get("report_type") == "l5_world_model")),
        "roadmap": bool(_record_id(report.get("roadmap"))),
        "goal_graph": bool(_record_id(report.get("goal_graph")) or _record_id(auto.get("goal_graph") if isinstance(auto, dict) else {})),
        "autonomous_learning": bool(isinstance(auto, dict) and auto.get("ok")),
        "candidate": bool(_candidate_ids(auto)),
        "replay": _has_replay(auto),
        "promotion_or_block": _has_promotion_or_block(auto),
        "reward": bool(reward.get("transition_record_id")),
        "self_continuity": bool(_record_id(self_continuity) or self_continuity.get("narrative")),
        "rollback": _has_rollback_evidence(report),
    }
    return [name for name, ok in checks.items() if not ok]


def _level_for(report: dict[str, Any], missing: list[str]) -> str:
    if not missing:
        return "L5"
    missing_set = set(missing)
    if not {"world_model", "roadmap", "goal_graph"} & missing_set:
        return "L4"
    auto = report.get("autonomous_learning") if isinstance(report.get("autonomous_learning"), dict) else {}
    if auto and (_candidate_ids(auto) or _has_replay(auto)):
        return "L3"
    world = report.get("world_model") if isinstance(report.get("world_model"), dict) else {}
    if world.get("weaknesses"):
        return "L2"
    if world:
        return "L1"
    return "L0"


def _merge_goal_graph(graph: dict[str, Any], autonomous: dict[str, Any]) -> dict[str, Any]:
    auto_graph = autonomous.get("goal_graph") if isinstance(autonomous.get("goal_graph"), dict) else {}
    merged = dict(graph or {})
    if auto_graph:
        merged.update({key: value for key, value in auto_graph.items() if value not in (None, "", [], {})})
    return merged


def _has_replay(auto: dict[str, Any]) -> bool:
    if not isinstance(auto, dict):
        return False
    replay = auto.get("real_task_replay") or auto.get("replay") or {}
    if isinstance(replay, dict) and replay.get("ok") is True:
        sample_count = int(replay.get("sample_count") or replay.get("case_count") or replay.get("pass_count") or 0)
        pass_count = int(replay.get("pass_count") or 0)
        return sample_count > 0 and pass_count >= sample_count
    if auto.get("replay_gate_passed") is True:
        return True
    dataset = auto.get("replay_dataset") or {}
    return int(dataset.get("case_count") or 0) > 0 and bool(auto.get("ok"))


def _has_promotion_or_block(auto: dict[str, Any]) -> bool:
    if not isinstance(auto, dict):
        return False
    if auto.get("blocked_reason") or auto.get("promotion_blocked_reason"):
        return True
    promotion = auto.get("promotion") if isinstance(auto.get("promotion"), dict) else {}
    if promotion.get("applied") or promotion.get("promotion_request_id") or promotion.get("rollout_ledger_id"):
        return True
    return any(
        isinstance(item, dict) and (item.get("applied") or item.get("promotion_request_id") or item.get("rollout_ledger_id"))
        for item in list(auto.get("promotions") or [])
    )


def _candidate_ids(auto: dict[str, Any]) -> list[str]:
    if not isinstance(auto, dict):
        return []
    ids = [str(item) for item in auto.get("candidate_ids") or [] if str(item or "").strip()]
    if auto.get("candidate_id"):
        ids.append(str(auto["candidate_id"]))
    return _compact_ids(ids)


def _rollback_refs(auto: dict[str, Any]) -> list[str]:
    if not isinstance(auto, dict):
        return []
    refs = []
    promotion = auto.get("promotion") if isinstance(auto.get("promotion"), dict) else {}
    refs.append(str(promotion.get("rollback_command") or ""))
    for item in list(auto.get("promotions") or []):
        if isinstance(item, dict):
            refs.append(str(item.get("rollback_command") or ""))
    return _compact_ids(refs)


def _rollback_evidence_refs(report: dict[str, Any]) -> list[str]:
    auto = report.get("autonomous_learning") if isinstance(report.get("autonomous_learning"), dict) else {}
    refs = _rollback_refs(auto)
    if refs:
        return refs
    if _observation_mode_no_apply(report):
        return ["observation_mode_no_apply"]
    return []


def _has_rollback_evidence(report: dict[str, Any]) -> bool:
    return bool(_rollback_evidence_refs(report))


def _observation_mode_no_apply(report: dict[str, Any]) -> bool:
    auto = report.get("autonomous_learning") if isinstance(report.get("autonomous_learning"), dict) else {}
    return bool(report.get("apply") is False and _has_promotion_or_block(auto))


def _latest_l5_closed_loop_report(runtime: Any, *, scope: ScopeRef) -> dict[str, Any]:
    records = runtime.store.list_records(kinds=["l5_closed_loop"], scope=scope, limit=1)
    if not records:
        return {}
    content = records[0].content if isinstance(records[0].content, dict) else {}
    report = dict(content)
    report.setdefault("persisted_record_id", records[0].record_id)
    return report


def _record_id(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("persisted_record_id") or value.get("record_id") or "")
    return ""


def _evidence_refs(weaknesses: list[dict[str, Any]], capabilities: list[dict[str, Any]], records: list[RecordEnvelope]) -> list[str]:
    refs: list[str] = []
    for item in weaknesses + capabilities:
        refs.extend(str(value) for value in item.get("source_record_ids") or [] if str(value or "").strip())
    refs.extend(record.record_id for record in records[:10])
    return _compact_ids(refs)


def _report_evidence(report: dict[str, Any]) -> list[str]:
    refs = [
        _record_id(report.get("world_model")),
        _record_id(report.get("roadmap")),
        _record_id(report.get("goal_graph")),
        _record_id(report.get("self_continuity")),
        str((report.get("reward") or {}).get("transition_record_id") or ""),
    ]
    refs.extend(_candidate_ids(report.get("autonomous_learning") or {}))
    return _compact_ids(refs)


def _compact_ids(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _float(value: Any) -> float:
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return 0.0
