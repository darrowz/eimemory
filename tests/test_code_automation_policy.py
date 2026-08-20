from __future__ import annotations

import json

from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_ENV,
    CODE_AUTOMATION_POLICY_SOURCE,
    code_automation_policy_summary,
    load_code_automation_policy,
)


def _policy(*, local_apply: bool = True, commit: bool = False, deployment: bool = False) -> dict:
    return {
        "schema_version": "code_automation_policy.v1",
        "policy_id": "test-machine-policy-v1",
        "actions": {
            "local_apply": local_apply,
            "commit": commit,
            "deployment": deployment,
        },
    }


def test_machine_policy_accepts_only_complete_environment_action_map(monkeypatch) -> None:
    monkeypatch.setenv(CODE_AUTOMATION_POLICY_ENV, json.dumps(_policy()))

    loaded = load_code_automation_policy(
        profile_key="l5.default",
        capability_id="code.implementation",
        capability_revision_id="rev-1",
        capability_scope="global",
        provider_binding_id="binding-1",
    )

    assert loaded["ok"] is True
    assert loaded["source"] == CODE_AUTOMATION_POLICY_SOURCE
    assert loaded["actions"] == {
        "local_apply": True,
        "commit": False,
        "deployment": False,
    }
    summary = code_automation_policy_summary(loaded)
    assert summary["policy_digest"]
    assert len(summary["policy_digest"]) == 64
    assert "constraints" not in summary
    assert "context" not in summary


def test_machine_policy_rejects_missing_or_candidate_shaped_authority(monkeypatch) -> None:
    monkeypatch.delenv(CODE_AUTOMATION_POLICY_ENV, raising=False)

    missing = load_code_automation_policy()

    assert missing["ok"] is False
    assert missing["reason"] == "machine_policy_environment_missing"
    forged_summary = code_automation_policy_summary(
        {
            "ok": True,
            "status": "enabled",
            "source": "candidate",
            "policy_id": "candidate-claim",
            "actions": {"local_apply": True, "commit": True, "deployment": True},
        }
    )
    assert forged_summary["ok"] is False
    assert forged_summary["status"] == "blocked"
    assert forged_summary["source"] == ""
    assert forged_summary["reason"] == "machine_policy_summary_untrusted"


def test_machine_policy_rejects_unknown_or_incomplete_environment_schema(monkeypatch) -> None:
    invalid = _policy()
    invalid["proposer_grant"] = True
    monkeypatch.setenv(CODE_AUTOMATION_POLICY_ENV, json.dumps(invalid))

    unknown = load_code_automation_policy()

    assert unknown["ok"] is False
    assert unknown["reason"] == "machine_policy_fields_unknown"

    monkeypatch.setenv(
        CODE_AUTOMATION_POLICY_ENV,
        json.dumps(
            {
                "schema_version": "code_automation_policy.v1",
                "policy_id": "incomplete-actions",
                "actions": {"local_apply": True},
            }
        ),
    )

    incomplete = load_code_automation_policy()

    assert incomplete["ok"] is False
    assert incomplete["reason"] == "machine_policy_actions_incomplete"


def test_machine_policy_requires_exact_constraint_coordinate(monkeypatch) -> None:
    constrained = _policy()
    constrained["constraints"] = {"capability_ids": ["code.implementation"]}
    monkeypatch.setenv(CODE_AUTOMATION_POLICY_ENV, json.dumps(constrained))

    allowed = load_code_automation_policy(capability_id="code.implementation")
    rejected = load_code_automation_policy(capability_id="code.implementation-other")

    assert allowed["ok"] is True
    assert rejected["ok"] is False
    assert rejected["reason"] == "machine_policy_capability_id_not_allowed"
