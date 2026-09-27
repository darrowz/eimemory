from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eimemory.governance.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.models.records import ScopeRef


def create_sandbox_experiment(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    loop_id: str,
    learning_goal_id: str,
    research_note_id: str,
    candidate_kind: str,
    candidate_patch: dict[str, Any],
    expected_gain: str = "",
    authority_tier: str | None = None,
) -> str:
    tier = authority_tier or _tier_for_candidate(candidate_kind)
    semantic_key = stable_semantic_key("experiment", learning_goal_id, research_note_id, candidate_kind, candidate_patch)
    artifact_path = _write_candidate_artifact(runtime, semantic_key=semantic_key, payload=candidate_patch)
    record = append_learning_record_once(
        runtime,
        kind="learning_experiment",
        title=f"Sandbox experiment: {candidate_kind}",
        summary=expected_gain or str(candidate_patch.get("summary") or candidate_patch.get("rule") or candidate_kind),
        scope=scope,
        loop_id=loop_id,
        step_name="experiment",
        semantic_key=semantic_key,
        authority_tier=tier,
        status="candidate",
        content={
            "learning_goal_id": learning_goal_id,
            "research_note_id": research_note_id,
            "candidate_kind": candidate_kind,
            "candidate_patch": candidate_patch,
            "artifact_path": str(artifact_path),
            "expected_gain": expected_gain,
            "rollback": "Delete or disable the generated candidate artifact; no production state was changed by sandbox creation.",
        },
        meta={
            "learning_goal_id": learning_goal_id,
            "research_note_id": research_note_id,
            "candidate_kind": candidate_kind,
            "authority_tier": tier,
            "artifact_path": str(artifact_path),
        },
    )
    return record.record_id


def _write_candidate_artifact(runtime: Any, *, semantic_key: str, payload: dict[str, Any]) -> Path:
    root = Path(runtime.store.root) / "state" / "autonomous_learning" / "sandbox"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{semantic_key}.json"
    if not path.exists():
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _tier_for_candidate(candidate_kind: str) -> str:
    kind = str(candidate_kind or "").strip().lower()
    if kind in {"memory_rule", "tool_route", "eval_case", "skill_draft", "sop_draft"}:
        return "L1"
    if kind in {"source_policy", "prompt_policy", "system_prompt_patch", "scheduler_policy", "code_patch", "deployment_rollout"}:
        return "L2"
    return "L0"
