from dataclasses import asdict

from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef
from eimemory.governance import closure_rehearsal as rehearsal


def test_seeded_sops_do_not_reuse_another_capability_target(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "a", "workspace_id": "w", "user_id": "u"})
    try:
        first = rehearsal._seed_eiskill_playbooks(runtime, scope=scope, persist=True, target_capability="cap.alpha")
        second = rehearsal._seed_eiskill_playbooks(runtime, scope=scope, persist=True, target_capability="cap.beta")
        assert set(first).isdisjoint(second)
        assert rehearsal._seed_eiskill_playbooks(runtime, scope=scope, persist=True, target_capability="cap.alpha") == first
        promotion = runtime.promote_repeated_sops_to_skill_candidates(scope=asdict(scope), persist=True)
        assert {item["target_capability"] for item in promotion["skills"]} == {"cap.alpha", "cap.beta"}
        selected = rehearsal._rehearsal_skill_id(promotion, target_capability="cap.beta", playbook_ids=second)
        assert selected
        skill = next(item for item in promotion["skills"] if item["skill_id"] == selected)
        assert set(skill["source_record_ids"]) == set(second)
    finally:
        runtime.close()


def test_rehearsal_never_selects_unrelated_first_skill():
    promotion = {"skills": [
        {"skill_id": "unrelated", "target_capability": "cap.beta", "source_record_ids": ["old"]},
        {"skill_id": "matching", "target_capability": "cap.beta", "source_record_ids": ["one", "two", "three"]},
    ]}
    assert rehearsal._rehearsal_skill_id(promotion, target_capability="cap.beta", playbook_ids=["one", "two", "three"]) == "matching"
    assert rehearsal._rehearsal_skill_id(promotion, target_capability="cap.alpha", playbook_ids=["one", "two", "three"]) == ""
    assert rehearsal._rehearsal_skill_id(promotion, target_capability="cap.beta", playbook_ids=[]) == ""
