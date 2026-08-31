from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_SCHEMA_V2,
    load_code_automation_policy,
    consume_code_automation_policy,
)


@pytest.fixture(autouse=True)
def _isolate_code_evolution_kill_switch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "EIMEMORY_CODE_EVOLUTION_KILL_SWITCH",
        str(tmp_path / "absent-code-evolution.disabled"),
    )


def _policy() -> dict:
    return {
        "schema_version": CODE_AUTOMATION_POLICY_SCHEMA_V2,
        "policy_id": "bounded-one-shot-test",
        "not_before": "2026-08-22T00:00:00Z",
        "expires_at": "2099-08-24T00:00:00Z",
        "max_transactions": 1,
        "incident": {
            "class": "l5.product_completion_semantic_misreport",
            "detector_id": "detector.test",
        },
        "capability": {
            "profile_key": "l5.default:v1",
            "capability_id": "code.implementation",
            "revision_id": "code.implementation:v9",
            "binding_id": "binding.hermes.code-implementation:v9",
            "implementation_digest": "a" * 64,
            "operation": "propose_patch_v2",
        },
        "repository": {
            "root": "/dev-project/eimemory",
            "remote": "origin",
            "remote_url_digest": "b" * 64,
            "branch": "master",
            "base_commit": "c" * 40,
            "base_tree_digest": "d" * 64,
        },
        "patch": {
            "allowed_files": ["eimemory/governance/l5_reader.py"],
            "max_files": 1,
            "max_file_bytes": 49_152,
            "max_total_bytes": 49_152,
            "max_changed_lines": 80,
            "max_diff_bytes": 262_144,
        },
        "verification": {
            "test_plan_id": "l5.product-completion-reporting.v1",
            "test_plan_digest": "e" * 64,
            "full_suite_required": True,
        },
        "effects": {
            "commit": False,
            "push": False,
            "deployment": False,
            "rollback": False,
            "sedimentation": False,
        },
        "deployment": {
            "installer_digest": "f" * 64,
            "current_link": "/opt/eimemory/current",
            "health_url": "http://127.0.0.1:8091/health",
            "observation_seconds": 172_800,
        },
    }


def test_v2_policy_requires_regular_file_and_defaults_effects_disabled(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy()), encoding="utf-8")
    os.chmod(path, 0o600)

    loaded = load_code_automation_policy(path=path, checked_at="2026-08-23T00:00:00Z")

    assert loaded["ok"] is True
    assert loaded["schema_version"] == CODE_AUTOMATION_POLICY_SCHEMA_V2
    assert loaded["effects"] == {
        "commit": False,
        "push": False,
        "deployment": False,
        "rollback": False,
        "sedimentation": False,
    }
    assert len(loaded["policy_digest"]) == 64


def test_v2_policy_fails_closed_when_kill_switch_is_present(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy()), encoding="utf-8")
    os.chmod(path, 0o600)
    kill_switch = tmp_path / "code-evolution.disabled"
    kill_switch.touch()

    loaded = load_code_automation_policy(
        path=path,
        checked_at="2026-08-23T00:00:00Z",
        kill_switch_path=kill_switch,
    )

    assert loaded["ok"] is False
    assert loaded["reason"] == "kill_switch_present"


def test_v2_policy_rejects_unknown_fields_changed_digest_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    payload = _policy()
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    assert load_code_automation_policy(path=path, checked_at="2026-08-23T00:00:00Z")["reason"] == "policy_fields_unknown"

    path.write_text(json.dumps(_policy()), encoding="utf-8")
    os.chmod(path, 0o644)
    assert load_code_automation_policy(path=path, checked_at="2026-08-23T00:00:00Z")["reason"] == "policy_permissions_invalid"

    link = tmp_path / "policy-link.json"
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert load_code_automation_policy(path=link, checked_at="2026-08-23T00:00:00Z")["reason"] == "policy_symlink_rejected"


def test_v2_policy_rejects_symlinked_ancestor_and_wrong_owner(tmp_path: Path, monkeypatch) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    path = actual / "policy.json"
    path.write_text(json.dumps(_policy()), encoding="utf-8")
    os.chmod(path, 0o600)
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert load_code_automation_policy(
        path=linked_parent / "policy.json",
        checked_at="2026-08-23T00:00:00Z",
    )["reason"] == "policy_symlink_rejected"

    monkeypatch.setattr(os, "geteuid", lambda: path.stat().st_uid + 1)
    assert load_code_automation_policy(
        path=path,
        checked_at="2026-08-23T00:00:00Z",
    )["reason"] == "policy_owner_invalid"


def test_v2_policy_consumption_rejects_transaction_coordinate_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy()), encoding="utf-8")
    os.chmod(path, 0o600)
    loaded = load_code_automation_policy(path=path, checked_at="2026-08-23T00:00:00Z")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        from eimemory.storage.code_evolution_store import CodeEvolutionStore

        CodeEvolutionStore(runtime.store).create_transaction(
            {
                "transaction_id": "tx-mismatch",
                "idempotency_key": "idem-tx-mismatch",
                "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
                "incident": {"incident_id": "incident-mismatch", "incident_class": "different.incident"},
                "detector": "detector.test",
                "repository": {"root": "/dev-project/eimemory", "remote": "origin", "ref": "master", "base_commit": "c" * 40, "base_tree_digest": "d" * 64},
                "provider": {"capability_id": "code.implementation", "revision_id": "code.implementation:v9", "binding_id": "binding.hermes.code-implementation:v9", "provider_kind": "hermes", "provider_instance_id": "hermes.eimemory.code-implementation.production", "implementation_digest": "a" * 64},
            }
        )
        result = consume_code_automation_policy(
            path=path,
            transaction_id="tx-mismatch",
            expected_digest=loaded["policy_digest"],
            store=runtime.store,
        )
    finally:
        runtime.close()

    assert result["ok"] is False
    assert result["reason"] == "policy_transaction_incident_class_mismatch"


def test_v2_policy_one_shot_consumption_is_idempotent_but_conflicting_transaction_blocks(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(_policy()), encoding="utf-8")
    os.chmod(path, 0o600)
    loaded = load_code_automation_policy(path=path, checked_at="2026-08-23T00:00:00Z")
    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        from eimemory.storage.code_evolution_store import CodeEvolutionStore
        from eimemory.governance.code_evolution_transaction import CodeEvolutionTransactionManager

        ledger = CodeEvolutionStore(runtime.store)
        def add_ready_transaction(transaction_id: str) -> None:
            ledger.create_transaction(
                {
                    "transaction_id": transaction_id,
                    "idempotency_key": f"idem-{transaction_id}",
                    "scope": {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"},
                    "incident": {"incident_id": f"incident-{transaction_id}", "incident_class": "l5.product_completion_semantic_misreport"},
                    "detector": "detector.test",
                    "profile_key": "l5.default:v1",
                    "repository": {
                        "root": "/dev-project/eimemory",
                        "remote": "origin",
                        "remote_url_digest": "b" * 64,
                        "ref": "master",
                        "base_commit": "c" * 40,
                        "base_tree_digest": "d" * 64,
                    },
                    "provider": {
                        "capability_id": "code.implementation",
                        "revision_id": "code.implementation:v9",
                        "binding_id": "binding.hermes.code-implementation:v9",
                        "provider_kind": "hermes",
                        "provider_instance_id": "hermes.eimemory.code-implementation.production",
                        "implementation_digest": "a" * 64,
                        "operation": "propose_patch_v2",
                    },
                    "file_updates": [
                        {"path": "eimemory/governance/l5_reader.py", "content": "bounded"}
                    ],
                    "proposal_digest": "1" * 64,
                    "patch_digest": "2" * 64,
                        "candidate_tree_digest": "3" * 64,
                        "advertisement_id": "advertisement.code.v2",
                        "advertisement_digest": "4" * 64,
                        "catalog_case_id": "hongtu_code_implementation_v2",
                        "catalog_snapshot_digest": "5" * 64,
                    }
            )
            for kind in ("focused", "regression", "full_suite"):
                ledger.add_verification_receipt(
                    transaction_id,
                    {
                        "verification_kind": kind,
                        "base_commit": "c" * 40,
                        "patch_digest": "2" * 64,
                        "candidate_tree_digest": "3" * 64,
                        "test_plan_id": "l5.product-completion-reporting.v1",
                        "test_plan_digest": "e" * 64,
                        "exit_status": 0,
                        "result": "pass",
                    },
                )

        add_ready_transaction("tx-1")
        first = consume_code_automation_policy(path=path, transaction_id="tx-1", expected_digest=loaded["policy_digest"], store=runtime.store)
        retry = consume_code_automation_policy(path=path, transaction_id="tx-1", expected_digest=loaded["policy_digest"], store=runtime.store)
        CodeEvolutionTransactionManager(runtime).effect_disabled("tx-1", step="test_complete")
        add_ready_transaction("tx-2")
        other = consume_code_automation_policy(path=path, transaction_id="tx-2", expected_digest=loaded["policy_digest"], store=runtime.store)
    finally:
        runtime.close()

    assert first["ok"] is True
    assert retry["idempotent"] is True
    assert other["ok"] is False
    assert other["reason"] == "policy_already_consumed"
