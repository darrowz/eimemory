from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


SCOPE = {"agent_id": "agent-skills", "workspace_id": "skill-sedimentation"}


def test_repeated_sops_become_queryable_callable_eiskill_candidates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        for index in range(3):
            runtime.store.append(
                RecordEnvelope.create(
                    kind="learning_playbook",
                    title="Recall with evidence refs",
                    summary="When answering memory status, cite source record id, commit, ledger id, and timeline.",
                    detail="Steps: recall graph route, collect evidence refs, answer with ids, run replay.",
                    scope=scope_ref,
                    source="test.skill_sedimentation",
                    status="active",
                    content={
                        "sop_key": "recall-evidence-refs",
                        "steps": ["route recall", "collect evidence refs", "answer with ids", "run replay"],
                        "target_capability": "memory.recall",
                        "replay_passed": True,
                        "source_repeat": index + 1,
                    },
                    meta={
                        "sop_key": "recall-evidence-refs",
                        "target_capability": "memory.recall",
                        "replay_passed": True,
                    },
                )
            )

        report = runtime.promote_repeated_sops_to_skill_candidates(scope=SCOPE, min_repeats=3, persist=True)

        assert report["ok"] is True
        assert report["skill_candidate_count"] == 1
        skill_id = report["skills"][0]["skill_id"]
        registry = runtime.list_eiskills(scope=SCOPE)
        assert registry["ok"] is True
        assert registry["skill_count"] == 1
        assert registry["skills"][0]["skill_id"] == skill_id
        assert registry["skills"][0]["callable"] is True

        invoked = runtime.call_eiskill(skill_id=skill_id, scope=SCOPE, context={"query": "why recall was low"})
        assert invoked["ok"] is True
        assert invoked["skill_id"] == skill_id
        assert invoked["steps"]
        assert invoked["record_id"]
    finally:
        runtime.close()


def test_wechat_and_douyin_playbooks_sediment_into_executable_skills(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        scope_ref = ScopeRef.from_dict(SCOPE)
        for index, channel in enumerate(["wechat", "wechat", "douyin"]):
            runtime.store.append(
                RecordEnvelope.create(
                    kind="learning_playbook",
                    title="Publish social content through standard toolchain",
                    summary=f"{channel} standard publishing workflow.",
                    detail="Trigger: user asks to publish. Action: use standard connector. Verification: check platform draft/post id. Rollback: keep draft inactive or delete failed draft.",
                    scope=scope_ref,
                    source="test.skill_sedimentation",
                    status="active",
                    content={
                        "sop_key": "social-publish-standard-toolchain",
                        "target_capability": "operations.social_publish",
                        "steps": ["open standard connector", "prepare payload", "publish or draft", "verify platform id"],
                        "trigger_conditions": ["wechat publish request", "douyin publish request"],
                        "action": "use standard connector toolchain",
                        "verification": "platform draft/post id exists",
                        "rollback": "keep draft inactive or delete failed draft",
                        "replay_passed": True,
                        "source_repeat": index + 1,
                    },
                    meta={
                        "sop_key": "social-publish-standard-toolchain",
                        "target_capability": "operations.social_publish",
                        "replay_passed": True,
                    },
                )
            )

        report = runtime.promote_repeated_sops_to_skill_candidates(scope=SCOPE, min_repeats=3, persist=True)

        skill = report["skills"][0]
        assert skill["trigger_conditions"] == ["wechat publish request", "douyin publish request"]
        assert skill["action"] == "use standard connector toolchain"
        assert skill["verification"] == "platform draft/post id exists"
        assert skill["rollback"] == "keep draft inactive or delete failed draft"
        registry = runtime.list_eiskills(scope=SCOPE)
        assert registry["skills"][0]["verification"] == "platform draft/post id exists"
    finally:
        runtime.close()
