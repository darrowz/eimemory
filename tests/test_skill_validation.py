from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


def _candidate_payload(**overrides):
    payload = {
        "trigger_conditions": ["Use when a repeatable memory workflow is requested."],
        "steps": ["Inspect the request and scope.", "Apply the documented workflow."],
        "acceptance_criteria": ["The workflow outcome is verified locally."],
        "source_trust": 0.82,
        "risk_level": "low",
        "target_capability": "memory.workflow",
        "status": "sandbox_ready",
        "title": "Skill candidate: Memory workflow",
        "summary": "A conservative reusable workflow candidate.",
    }
    payload.update(overrides)
    return payload


def _append_candidate(runtime: Runtime, *, scope: dict, record_id: str = "skillcand_validation", **overrides) -> RecordEnvelope:
    payload = _candidate_payload(**overrides)
    record = RecordEnvelope.create(
        kind="skill_candidate",
        status=str(payload.get("status") or "candidate"),
        title=str(payload.get("title") or "Skill candidate"),
        summary=str(payload.get("summary") or ""),
        detail="\n".join(str(step) for step in payload.get("steps") or []),
        content=payload,
        tags=["skill-candidate", str(payload.get("risk_level") or "medium")],
        source="test.skill_validation",
        scope=ScopeRef.from_dict(scope),
        meta={
            "status": str(payload.get("status") or "candidate"),
            "risk_level": str(payload.get("risk_level") or "medium"),
            "source_trust": float(payload.get("source_trust") or 0.0),
        },
    )
    record.record_id = record_id
    return runtime.store.append(record)


def test_high_quality_candidate_validates_to_canary_not_active(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope)

        report = runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["candidate_id"] == candidate.record_id
        assert report["pass"] is True
        assert report["proposal_status"] == "canary"
        assert report["status_transition"] == {"from": "sandbox_ready", "to": "canary"}
        assert report["pass_rate"] == 1.0
        assert stored is not None
        assert stored.status == "canary"
        assert stored.status != "active"
        assert stored.meta["skill_validation"]["stage"] == "canary"
    finally:
        runtime.close()


def test_low_quality_high_risk_candidate_fails_and_records_quarantine(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(
            runtime,
            scope=scope,
            record_id="skillcand_risky",
            trigger_conditions=[],
            steps=["Deploy directly."],
            acceptance_criteria=[],
            source_trust=0.95,
            risk_level="high",
            status="sandbox_ready",
        )

        report = runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        results = runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=10)
        assert report["pass"] is False
        assert report["proposal_status"] == "quarantined"
        assert report["status_transition"]["to"] == "quarantined"
        assert "risk_level_high" in report["reasons"]
        assert stored is not None
        assert stored.status == "quarantined"
        assert stored.status != "active"
        assert any(item.meta.get("report_type") == "skill_candidate_validation" for item in results)
    finally:
        runtime.close()


def test_external_knowledge_candidate_requires_high_trust_for_canary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(
            runtime,
            scope=scope,
            record_id="skillcand_medium_trust_external",
            source_trust=0.65,
            risk_level="low",
            source_kind="blog",
            source_uri="https://example.test/blog",
            trust_tier="medium",
        )

        report = runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["pass"] is False
        assert report["proposal_status"] == "quarantined"
        assert "knowledge_safety_not_capability_allowed" in report["reasons"]
        assert any(check["name"] == "knowledge_safety" and check["pass"] is False for check in report["checks"])
        assert stored is not None
        assert stored.status == "quarantined"
    finally:
        runtime.close()


def test_replay_and_sandbox_good_observations_do_not_promote_canary_to_active(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope, record_id="skillcand_replay_only")
        runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)

        for index, observation_kind in enumerate(["replay", "sandbox", "replay"]):
            report = runtime.record_skill_candidate_observation(
                candidate_id=candidate.record_id,
                scope=scope,
                outcome="good",
                observation_id=f"obs-replay-good-{index}",
                observation_kind=observation_kind,
            )

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["proposal_status"] == "canary"
        assert report["pass"] is True
        assert report["pass_count"] == 3
        assert report["fail_count"] == 0
        assert report["real_good_count"] == 0
        assert report["real_bad_count"] == 0
        assert report["failure_rate"] == 0.0
        assert report["observation_kind"] == "replay"
        assert stored is not None
        assert stored.status == "canary"
        validation = stored.meta["skill_validation"]
        assert validation["good_observation_count"] == 3
        assert validation["real_good_count"] == 0
        assert validation["failure_rate"] == 0.0
        assert [item["observation_kind"] for item in validation["observations"]] == ["replay", "sandbox", "replay"]
    finally:
        runtime.close()


def test_three_good_real_or_operator_observations_promote_canary_to_active(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope, record_id="skillcand_observe")
        runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)

        for index, observation_kind in enumerate(["real", "operator", "real"]):
            report = runtime.record_skill_candidate_observation(
                candidate_id=candidate.record_id,
                scope=scope,
                outcome="good",
                observation_id=f"obs-good-{index}",
                observation_kind=observation_kind,
            )

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["proposal_status"] == "active"
        assert report["pass"] is True
        assert report["pass_rate"] == 1.0
        assert report["pass_count"] == 3
        assert report["fail_count"] == 0
        assert report["real_good_count"] == 3
        assert report["real_bad_count"] == 0
        assert report["failure_rate"] == 0.0
        assert stored is not None
        assert stored.status == "active"
        assert stored.meta["skill_validation"]["good_observation_count"] == 3
        assert stored.meta["skill_validation"]["bad_observation_count"] == 0
        assert stored.meta["skill_validation"]["real_good_count"] == 3
        assert stored.meta["skill_validation"]["real_bad_count"] == 0
    finally:
        runtime.close()


def test_one_bad_observation_quarantines_candidate(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope, record_id="skillcand_bad")
        runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)

        report = runtime.record_skill_candidate_observation(
            candidate_id=candidate.record_id,
            scope=scope,
            outcome="bad",
            observation_id="obs-bad-1",
            observation_kind="real",
            reason="The candidate produced an unsafe recommendation.",
        )

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["pass"] is False
        assert report["proposal_status"] == "quarantined"
        assert report["status_transition"] == {"from": "canary", "to": "quarantined"}
        assert stored is not None
        assert stored.status == "quarantined"
        assert stored.status != "active"
    finally:
        runtime.close()


def test_active_repeated_bad_real_observations_roll_back_with_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope, record_id="skillcand_active_bad")
        runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)
        for index in range(3):
            runtime.record_skill_candidate_observation(
                candidate_id=candidate.record_id,
                scope=scope,
                outcome="good",
                observation_id=f"obs-active-good-{index}",
                observation_kind="real",
            )

        first_bad = runtime.record_skill_candidate_observation(
            candidate_id=candidate.record_id,
            scope=scope,
            outcome="failed",
            observation_id="obs-active-bad-1",
            observation_kind="real",
            reason="Operator task failed after activation.",
        )
        assert first_bad["proposal_status"] == "active"
        assert first_bad["failure_rate"] == 0.25
        assert first_bad["real_bad_count"] == 1
        assert first_bad["last_bad_at"]

        second_bad = runtime.record_skill_candidate_observation(
            candidate_id=candidate.record_id,
            scope=scope,
            outcome="bad",
            observation_id="obs-active-bad-2",
            observation_kind="operator",
            reason="Second real task failed after activation.",
        )

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert second_bad["proposal_status"] == "rolled_back"
        assert second_bad["pass"] is False
        assert second_bad["failure_rate"] == 0.4
        assert second_bad["real_bad_count"] == 2
        assert second_bad["rollback_evidence_ids"] == ["obs-active-bad-1", "obs-active-bad-2"]
        assert "repeated_bad_real_observations" in second_bad["reasons"]
        assert stored is not None
        assert stored.status == "rolled_back"
        validation = stored.meta["skill_validation"]
        assert validation["last_bad_at"] == second_bad["last_bad_at"]
        assert validation["rollback_evidence_ids"] == ["obs-active-bad-1", "obs-active-bad-2"]
    finally:
        runtime.close()


def test_active_single_ordinary_real_failure_does_not_immediately_roll_back(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope, record_id="skillcand_active_sparse")
        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert stored is not None
        stored.status = "active"
        stored.content["status"] = "active"
        stored.meta["status"] = "active"
        runtime.store.rewrite(stored)

        report = runtime.record_skill_candidate_observation(
            candidate_id=candidate.record_id,
            scope=scope,
            outcome="failed",
            observation_id="obs-sparse-real-bad-1",
            observation_kind="real",
            reason="First ordinary real-task failure after migration.",
        )

        reloaded = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["proposal_status"] == "active"
        assert report["pass"] is False
        assert report["real_bad_count"] == 1
        assert report["rollback_evidence_ids"] == []
        assert "bad_observation_recorded" in report["reasons"]
        assert reloaded is not None
        assert reloaded.status == "active"
    finally:
        runtime.close()


def test_active_synthetic_failure_does_not_roll_back(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope, record_id="skillcand_active_synthetic")
        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert stored is not None
        stored.status = "active"
        stored.content["status"] = "active"
        stored.meta["status"] = "active"
        runtime.store.rewrite(stored)

        report = runtime.record_skill_candidate_observation(
            candidate_id=candidate.record_id,
            scope=scope,
            outcome="failed",
            observation_id="obs-sandbox-bad-1",
            observation_kind="sandbox",
            reason="Sandbox replay failed.",
        )

        reloaded = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["proposal_status"] == "active"
        assert report["pass"] is False
        assert report["real_bad_count"] == 0
        assert report["rollback_evidence_ids"] == []
        assert reloaded is not None
        assert reloaded.status == "active"
    finally:
        runtime.close()


def test_active_unsafe_real_observation_rolls_back_immediately(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "agent-skill", "workspace_id": "validation"}
    try:
        candidate = _append_candidate(runtime, scope=scope, record_id="skillcand_active_unsafe")
        runtime.validate_skill_candidate(candidate_id=candidate.record_id, scope=scope)
        for index in range(3):
            runtime.record_skill_candidate_observation(
                candidate_id=candidate.record_id,
                scope=scope,
                outcome="success",
                observation_id=f"obs-unsafe-good-{index}",
                observation_kind="operator",
            )

        report = runtime.record_skill_candidate_observation(
            candidate_id=candidate.record_id,
            scope=scope,
            outcome="unsafe",
            observation_id="obs-unsafe-bad-1",
            observation_kind="real",
            reason="The active skill produced an unsafe operator outcome.",
        )

        stored = runtime.store.get_by_id(candidate.record_id, scope=scope)
        assert report["proposal_status"] == "rolled_back"
        assert report["rollback_evidence_ids"] == ["obs-unsafe-bad-1"]
        assert report["last_bad_at"]
        assert "unsafe_outcome" in report["reasons"]
        assert stored is not None
        assert stored.status == "rolled_back"
    finally:
        runtime.close()


def test_dry_candidate_dict_validation_does_not_persist(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.validate_skill_candidate(candidate=_candidate_payload(), scope={"workspace_id": "dry"}, persist=False)

        assert report["pass"] is True
        assert report["proposal_status"] == "canary"
        assert report["candidate_id"].startswith("dry_skill_candidate_")
        assert runtime.store.list_records(kinds=["skill_candidate"], scope={"workspace_id": "dry"}, limit=10) == []
    finally:
        runtime.close()
