from __future__ import annotations

import json

from eimemory.api.runtime import Runtime
from eimemory.cli.main import main as cli_main
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


def test_cli_l5_readiness_returns_json(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))

    exit_code = cli_main(["learn", "l5-readiness", "--limit", "25", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["report_type"] == "l5_readiness_report"
    assert payload["current_stage"] == "L3.5"
    assert payload["persisted_record_id"] == ""
