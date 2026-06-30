from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
from eimemory.experience import record_outcome_trace
from eimemory.governance.capability_ledger import record_capability_score
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "agent-l5-readiness", "workspace_id": "l5-readiness", "user_id": "darrow"}


def test_l5_readiness_report_is_read_only_by_default_and_surfaces_gaps(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        before = len(runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=100))
        report = runtime.build_l5_readiness_report(scope=SCOPE)
        after = len(runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=100))
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["report_type"] == "l5_readiness_report"
    assert report["current_stage"] == "L3.5"
    assert report["persisted_record_id"] == ""
    assert before == after
    assert report["capability_gaps"]
    assert any(gap["capability"] == "search.discovery" for gap in report["capability_gaps"])
    assert "deployment" in report["risk_boundary"]
    assert report["next_actions"]


def test_l5_readiness_report_uses_existing_evidence_without_running_learning(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_ref = ScopeRef.from_dict(SCOPE)
    try:
        for capability in ("memory.recall", "tool.routing", "knowledge.intake", "safety.boundary"):
            record_capability_score(
                runtime,
                scope=SCOPE,
                loop_id="readiness-test",
                capability=capability,
                score=0.82,
                evidence_record_ids=[f"{capability}-e1", f"{capability}-e2", f"{capability}-e3"],
            )
        for index in range(3):
            runtime.store.append(
                RecordEnvelope.create(
                    kind="replay_result",
                    title=f"Replay {index}",
                    summary="pass",
                    scope=scope_ref,
                    content={"verdict": "pass", "capability": "memory.recall", "hit": True},
                    meta={"verdict": "pass", "capability": "memory.recall", "hit": True},
                )
            )
        runtime.store.append(
            RecordEnvelope.create(
                kind="promotion_request",
                title="Readiness promotion",
                summary="promoted",
                scope=scope_ref,
                status="promoted",
                content={"action": "promote", "target_capability": "memory.recall"},
                meta={"action": "promote", "target_capability": "memory.recall"},
            )
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE, persist=True)
        stored = runtime.store.get_by_id(report["persisted_record_id"], scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] == "L4"
    assert report["evidence_counts"]["replay_result"] == 3
    assert report["evidence_counts"]["promotion_applied"] == 1
    assert stored.kind == "reflection"
    assert stored.meta["report_type"] == "l5_readiness_report"


def test_l5_readiness_counts_policy_rollout_rollback_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        pattern_id = "readiness-policy-rollback"
        runtime.upsert_intent_pattern(
            {
                "id": pattern_id,
                "pattern": "readiness rollback rehearsal",
                "default_event_type": "repair",
                "interpreted_intent": "non-destructive readiness rollback",
                "confidence": 0.9,
            },
            scope=SCOPE,
        )
        rollback = runtime.rollback_intent_pattern(
            pattern_id,
            scope=SCOPE,
            reason="readiness should count policy rollback ledger",
        )

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert rollback["ok"] is True
    assert report["hard_metrics"]["rollback_count"] == 1
    assert report["evidence_counts"]["rollback_or_quarantine"] == 1


def test_l5_readiness_does_not_treat_replay_only_weak_capabilities_as_l5(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(runtime, scope=SCOPE, weak_outcomes=False, patch_samples=10)

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] != "L5"
    assert report["readiness_score"] < 1.0
    weak_gaps = {gap["capability"] for gap in report["capability_gaps"] if gap["capability"] in WEAK_CAPABILITIES}
    assert weak_gaps == WEAK_CAPABILITIES


def test_l5_readiness_requires_sufficient_auto_patch_success_samples(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(runtime, scope=SCOPE, weak_outcomes=True, patch_samples=2)

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] != "L5"
    assert report["hard_metric_quality"]["auto_patch_success_rate"]["sufficient"] is False


def test_l5_readiness_reaches_l5_only_with_attributed_weak_outcomes_and_patch_samples(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_l5_prerequisites(runtime, scope=SCOPE, weak_outcomes=True, patch_samples=10)

        report = runtime.build_l5_readiness_report(scope=SCOPE)
    finally:
        runtime.close()

    assert report["current_stage"] == "L5"
    assert report["readiness_score"] == 1.0
    assert report["hard_metric_quality"]["auto_patch_success_rate"]["sufficient"] is True
    assert report["weak_outcome_evidence"]["missing"] == []


def test_cli_l5_readiness_returns_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    exit_code = cli_main(["learn", "l5-readiness", "--limit", "25", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["report_type"] == "l5_readiness_report"
    assert payload["current_stage"] == "L3.5"
    assert payload["persisted_record_id"] == ""


WEAK_CAPABILITIES = {"search.discovery", "research.synthesis", "operations.uumit", "device.control"}
READINESS_CAPABILITIES = {
    "memory.recall",
    "tool.routing",
    "knowledge.intake",
    "proactive.judgment",
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
    "safety.boundary",
}


def _seed_l5_prerequisites(runtime: Runtime, *, scope: dict, weak_outcomes: bool, patch_samples: int) -> None:
    scope_ref = ScopeRef.from_dict(scope)
    for capability in READINESS_CAPABILITIES - WEAK_CAPABILITIES:
        record_capability_score(
            runtime,
            scope=scope,
            loop_id="seed_l5",
            capability=capability,
            score=0.84,
            evidence_record_ids=[f"{capability}-e1", f"{capability}-e2", f"{capability}-e3"],
        )
    runtime.build_capability_replay_packs(scope=scope, capabilities=sorted(WEAK_CAPABILITIES), persist=True, loop_id="seed_weak_replay")
    if weak_outcomes:
        for capability, task_type, summary in (
            ("search.discovery", "搜索最近 GitHub 热门项目", "搜索 GitHub created range stars sort source verification"),
            ("research.synthesis", "research_synthesis", "Synthesized research papers and claim evidence into a brief"),
            ("operations.uumit", "uumit_delivery", "UUMit delivery checklist and customer acceptance verified"),
            ("device.control", "media_playback", "Device speaker playback controlled and physical audio output verified"),
        ):
            for index in range(3):
                record_outcome_trace(
                    runtime,
                    {
                        "trace_id": f"{capability}-{index}",
                        "idempotency_key": f"idem-{capability}-{index}",
                        "task_type": task_type,
                        "input_summary": summary,
                        "outcome": {"status": "success"},
                        "verifier": {"passed": True},
                        "feedback": {"summary": "verified"},
                    },
                    scope=scope,
                )
    for index in range(10):
        runtime.store.append(
            RecordEnvelope.create(
                kind="replay_result",
                title=f"L5 replay {index}",
                summary="pass",
                scope=scope_ref,
                content={"verdict": "pass", "capability": "memory.recall", "hit": True},
                meta={"verdict": "pass", "capability": "memory.recall", "hit": True},
            )
        )
    for kind in ("l5_world_model", "l5_strategic_roadmap", "l5_assessment", "l5_closed_loop"):
        runtime.store.append(
            RecordEnvelope.create(
                kind=kind,
                title=kind,
                summary="present",
                scope=scope_ref,
                content={"report_type": kind},
                meta={"report_type": kind},
            )
        )
    for index in range(3):
        runtime.store.append(
            RecordEnvelope.create(
                kind="promotion_request",
                title=f"Promotion {index}",
                summary="promoted",
                scope=scope_ref,
                status="promoted",
                content={"action": "promote", "target_capability": "memory.recall"},
                meta={"action": "promote", "target_capability": "memory.recall"},
            )
        )
    runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Rollback rehearsal",
            summary="rolled back",
            scope=scope_ref,
            status="rolled_back",
            content={"action": "rollback"},
            meta={"action": "rollback"},
        )
    )
    for index in range(patch_samples):
        runtime.store.append(
            RecordEnvelope.create(
                kind="promotion_request",
                title=f"Patch promotion {index}",
                summary="deployed",
                scope=scope_ref,
                status="deployed",
                content={"action": "code_patch", "promotion_target": "code_patch"},
                meta={"action": "code_patch", "promotion_target": "code_patch"},
            )
        )
