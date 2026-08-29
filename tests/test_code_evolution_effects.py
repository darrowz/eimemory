from __future__ import annotations

from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance import code_evolution_effects as effects_module
from eimemory.governance.code_evolution_effects import (
    CandidateMaterialization,
    CodeEvolutionEffectOwner,
    DeploymentResult,
    ProductionEffectAdapter,
    VerificationResult,
    _l5_observation_semantics,
    _porcelain_changed_paths,
    _verification_environment,
    validated_file_updates,
)
from eimemory.governance.code_evolution_test_plans import protected_test_plan_digest
from eimemory.governance.code_evolution_transaction import CodeEvolutionTransactionManager
from eimemory.storage.code_evolution_store import digest_json


TX_ID = "tx-protected-effects"
SCOPE = {"tenant_id": "tenant", "agent_id": "agent", "workspace_id": "workspace", "user_id": "user"}


def test_porcelain_changed_paths_preserves_first_line_status_column() -> None:
    output = (
        b" M deploy/runtime_identity_policy.py\n"
        b" M tests/test_runtime_identity_policy.py\n"
    )

    assert _porcelain_changed_paths(output) == {
        "deploy/runtime_identity_policy.py",
        "tests/test_runtime_identity_policy.py",
    }


def test_verification_environment_redirects_explicit_pycompile_outputs() -> None:
    environment = _verification_environment()

    assert environment["PYTHONPYCACHEPREFIX"] == "/tmp/pycache"


def test_materialization_binds_base_tree_to_all_policy_protected_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_commit = "a" * 40
    protected = (
        "eimemory/governance/release_closure.py",
        "eimemory/ops/release_closure_failure.py",
    )
    observed: dict[str, object] = {}

    def fake_git(_root, *args):
        if args[:2] == ("remote", "get-url"):
            return "https://example.invalid/eimemory.git"
        if args[0] == "rev-parse":
            return base_commit
        raise AssertionError(args)

    def fake_digest(_root, commit, paths, *, git_blob_reader):
        observed.update(commit=commit, paths=tuple(paths), reader=git_blob_reader)
        return "0" * 64

    monkeypatch.setattr(effects_module, "_git", fake_git)
    monkeypatch.setattr(effects_module, "remote_url_digest", lambda _url: "b" * 64)
    monkeypatch.setattr(effects_module, "protected_paths_digest_at_commit", fake_digest)
    transaction = {
        "transaction_id": "tx-policy-tree-scope",
        "repository_root": "/dev-project/eimemory",
        "repository_remote": "origin",
        "repository_ref": "master",
        "base_commit": base_commit,
        "base_tree_digest": "1" * 64,
    }
    policy = {
        "repository": {"remote_url_digest": "b" * 64},
        "patch": {"allowed_files": list(protected)},
    }
    updates = [
        {
            "path": protected[1],
            "prior_sha256": "c" * 64,
            "content": "changed\n",
        }
    ]

    with pytest.raises(ValueError, match="base_tree_digest_mismatch"):
        ProductionEffectAdapter().materialize(transaction, policy, updates)

    assert observed["commit"] == base_commit
    assert observed["paths"] == protected


def _proposal(*, updates: list[dict] | None = None) -> dict:
    return {
        "schema_version": "code_implementation_proposal.v2",
        "transaction_id": TX_ID,
        "proposal_only": True,
        "qualifying": True,
        "test_only_provider": False,
        "origin": "system_detector",
        "detector": "detector.l5",
        "known_before_detection": False,
        "prior_user_reported": False,
        "manual_bootstrap": False,
        "profile_key": "l5.default:v1",
        "incident": {
            "incident_id": "incident-protected-effects",
            "incident_class": "l5.product_completion_semantic_misreport",
            "incident_digest": "a" * 64,
        },
        "repository": {
            "repository_root": "/dev-project/eimemory",
            "repository_remote": "origin",
            "repository_ref": "master",
            "remote_url_digest": "b" * 64,
            "base_commit": "c" * 40,
            "base_tree_digest": "d" * 64,
        },
        "provider": {
            "capability_id": "code.implementation",
            "revision_id": "code.implementation:v8",
            "binding_id": "binding.hermes.code-implementation:v8",
            "provider_kind": "hermes",
            "provider_instance_id": "hermes.eimemory.code-implementation.production",
            "operation": "propose_patch_v2",
            "implementation_digest": "e" * 64,
        },
        "file_updates": updates
        if updates is not None
        else [
            {
                "path": "eimemory/governance/l5_reader.py",
                "prior_sha256": "f" * 64,
                "content": "bounded candidate\n",
            }
        ],
        "proposal_digest": "1" * 64,
        "patch_digest": "1" * 64,
        "advertisement": {"advertisement_id": "advertisement-v2", "advertisement_digest": "2" * 64},
        "catalog": {"catalog_case_id": "hongtu_code_implementation_v2", "catalog_snapshot_digest": "3" * 64},
        "test_plan": {
            "id": "l5.product-completion-reporting.v1",
            "digest": protected_test_plan_digest("l5.product-completion-reporting.v1"),
        },
    }


def _policy() -> dict:
    return {
        "ok": True,
        "policy_digest": "6" * 64,
        "policy_path": "/policy.json",
        "repository": {
            "root": "/dev-project/eimemory",
            "remote": "origin",
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
            "test_plan_digest": protected_test_plan_digest("l5.product-completion-reporting.v1"),
            "full_suite_required": True,
        },
        "effects": {"commit": True, "push": True, "deployment": True, "rollback": True, "sedimentation": True},
        "deployment": {
            "installer_digest": "7" * 64,
            "current_link": "/opt/eimemory/current",
            "health_url": "http://127.0.0.1:8091/health",
            "observation_seconds": 172_800,
        },
    }


def _ready_manager(runtime: Runtime) -> CodeEvolutionTransactionManager:
    proposal = _proposal()
    manager = CodeEvolutionTransactionManager(runtime, owner_id="effect-test")
    manager.create_detected(
        {
            "transaction_id": TX_ID,
            "idempotency_key": f"code-evolution:{TX_ID}",
            "scope": SCOPE,
            "incident": proposal["incident"],
            "origin": proposal["origin"],
            "detector": proposal["detector"],
            "known_before_detection": False,
            "prior_user_reported": False,
            "manual_bootstrap": False,
            "repository": proposal["repository"],
            "provider": proposal["provider"],
            "profile_key": proposal["profile_key"],
            "advertisement_id": proposal["advertisement"]["advertisement_id"],
            "advertisement_digest": proposal["advertisement"]["advertisement_digest"],
            "catalog_case_id": proposal["catalog"]["catalog_case_id"],
            "catalog_snapshot_digest": proposal["catalog"]["catalog_snapshot_digest"],
            "proposal_digest": proposal["proposal_digest"],
            "patch_digest": proposal["patch_digest"],
            "candidate_tree_digest": "",
            "payload": proposal,
        }
    )
    for state in ("DIAGNOSED", "PROVIDER_RESOLVED", "PATCH_PROPOSED", "PATCH_VALIDATED"):
        manager.transition(TX_ID, state)
    execution_material = {
        "transaction_id": TX_ID,
        "proposal_digest": proposal["proposal_digest"],
        "patch_digest": proposal["patch_digest"],
        "production_eligible": True,
        "apply": True,
        "effects_enabled": True,
    }
    manager.update_metadata(
        TX_ID,
        payload_updates={
            "effect_execution_authorized": True,
            "candidate_materialization_intent": True,
            "effect_execution_authorization_digest": digest_json(execution_material),
        },
    )
    return manager


class _RecordingAdapter:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []
        self.fail_phase = ""
        self.fail_deploy = False
        self.landed_on_failed_deploy = False

    def materialize(self, transaction, policy, updates):
        self.calls.append("materialize")
        return CandidateMaterialization(self.root, "8" * 64, tuple(item["path"] for item in updates))

    def verify(self, candidate, *, phase, argv, heartbeat):
        heartbeat()
        self.calls.append(f"verify:{phase}")
        status = 1 if phase == self.fail_phase else 0
        return VerificationResult(status, 1 if status == 0 else 0, 1 if status else 0, 0, f"{phase} output".encode())

    def commit(self, candidate, *, transaction_id, base_commit, allowed_files):
        self.calls.append("commit")
        return "9" * 40

    def push(self, *, transaction, policy, candidate_commit, base_commit, remote, branch):
        self.calls.append("push")
        return {"remote_sha": candidate_commit}

    def deploy(self, runtime, *, transaction, policy, verification_receipt_digests, observation_deadline, heartbeat):
        heartbeat()
        self.calls.append("deploy")
        evidence = {"ok": not self.fail_deploy}
        if self.fail_deploy and self.landed_on_failed_deploy:
            evidence["commit"] = "9" * 40
        return DeploymentResult(not self.fail_deploy, "a" * 64, "9" * 40, "1.11.25", evidence)

    def rollback(self, runtime, *, transaction, policy, heartbeat):
        heartbeat()
        self.calls.append("rollback")
        return {"ok": True, "commit": transaction["base_commit"], "receipt_digest": "b" * 64}

    def cleanup(self, candidate):
        self.calls.append("cleanup")


@pytest.mark.parametrize(
    "updates",
    [
        [{"path": "../escape.py", "prior_sha256": "f" * 64, "content": "x"}],
        [{"path": "/absolute.py", "prior_sha256": "f" * 64, "content": "x"}],
        [{"path": "not-allowed.py", "prior_sha256": "f" * 64, "content": "x"}],
        [
            {"path": "eimemory/governance/l5_reader.py", "prior_sha256": "f" * 64, "content": "x"},
            {"path": "eimemory/governance/l5_reader.py", "prior_sha256": "f" * 64, "content": "y"},
        ],
    ],
)
def test_validated_file_updates_reject_untrusted_or_duplicate_paths(updates) -> None:
    transaction = {"payload": {**_proposal(), "file_updates": updates}}
    with pytest.raises(ValueError):
        validated_file_updates(transaction, _policy())


def test_effect_owner_progresses_exact_success_path_to_observation(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    manager = _ready_manager(runtime)
    adapter = _RecordingAdapter(tmp_path / "candidate")

    def consume(*, path, transaction_id, expected_digest, store):
        material = {"transaction_id": transaction_id, "policy_digest": expected_digest}
        authorization_digest = digest_json(material)
        return {
            "ok": True,
            **manager.store.consume_policy(
                transaction_id=transaction_id,
                policy_digest=expected_digest,
                authorization_receipt_digest=authorization_digest,
                payload={"authorization_material": material, "authorized_policy": _policy()},
            ),
        }

    result = CodeEvolutionEffectOwner(
        runtime,
        owner_id="effect-test",
        adapter=adapter,
        policy_loader=lambda: _policy(),
        policy_consumer=consume,
    ).execute(TX_ID)

    transaction = manager.store.get_transaction(TX_ID)
    assert result["ok"] is True
    assert result["applied"] is True
    assert transaction["current_state"] == "OBSERVING"
    assert transaction["candidate_commit"] == "9" * 40
    assert transaction["deployed_commit"] == "9" * 40
    assert transaction["payload"]["deployment_receipt_digest"] == "a" * 64
    assert transaction["payload"]["candidate_pushed_and_deployed"] is True
    assert len(manager.store.list_verification_receipts(TX_ID)) == 3
    assert adapter.calls == [
        "materialize",
        "verify:focused",
        "verify:regression",
        "verify:full_suite",
        "commit",
        "push",
        "deploy",
        "cleanup",
    ]
    runtime.close()


def test_effect_owner_restores_candidate_after_verification_failure(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    manager = _ready_manager(runtime)
    adapter = _RecordingAdapter(tmp_path / "candidate")
    adapter.fail_phase = "regression"

    result = CodeEvolutionEffectOwner(
        runtime,
        owner_id="effect-test",
        adapter=adapter,
        policy_loader=lambda: _policy(),
        policy_consumer=lambda **_kwargs: pytest.fail("policy must not be consumed"),
    ).execute(TX_ID)

    assert result["ok"] is False
    assert result["blocked_reason"] == "code_evolution_regression_verification_failed"
    assert manager.store.get_transaction(TX_ID)["current_state"] == "ABORTED_CANDIDATE_RESTORED"
    assert adapter.calls[-1] == "cleanup"
    runtime.close()


def test_effect_owner_resumes_after_focused_receipt_without_reexecuting_phase(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    manager = _ready_manager(runtime)
    manager.record_result(
        TX_ID,
        step="candidate_materialization",
        result_state="CANDIDATE_MATERIALIZED",
        updates={"candidate_tree_digest": "8" * 64},
    )
    manager.store.add_verification_receipt(
        TX_ID,
        {
            "verification_kind": "focused",
            "base_commit": "c" * 40,
            "patch_digest": "1" * 64,
            "candidate_tree_digest": "8" * 64,
            "test_plan_id": "l5.product-completion-reporting.v1",
            "test_plan_digest": protected_test_plan_digest("l5.product-completion-reporting.v1"),
            "exit_status": 0,
            "result": "pass",
        },
    )
    manager.transition(TX_ID, "FOCUSED_VERIFIED")
    adapter = _RecordingAdapter(tmp_path / "candidate")

    def consume(*, transaction_id, expected_digest, **_kwargs):
        material = {"transaction_id": transaction_id, "policy_digest": expected_digest}
        authorization_digest = digest_json(material)
        return {
            "ok": True,
            **manager.store.consume_policy(
                transaction_id=transaction_id,
                policy_digest=expected_digest,
                authorization_receipt_digest=authorization_digest,
                payload={"authorization_material": material, "authorized_policy": _policy()},
            ),
        }

    result = CodeEvolutionEffectOwner(
        runtime,
        owner_id="effect-test",
        adapter=adapter,
        policy_loader=lambda: _policy(),
        policy_consumer=consume,
    ).execute(TX_ID)

    assert result["ok"] is True
    assert "verify:focused" not in adapter.calls
    assert adapter.calls[:3] == ["materialize", "verify:regression", "verify:full_suite"]
    runtime.close()


def test_effect_owner_continues_recovered_committed_state_to_observation(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    manager = _ready_manager(runtime)
    manager.record_result(
        TX_ID,
        step="candidate_materialization",
        result_state="CANDIDATE_MATERIALIZED",
        updates={"candidate_tree_digest": "8" * 64},
    )
    for phase, state in (
        ("focused", "FOCUSED_VERIFIED"),
        ("regression", "REGRESSION_VERIFIED"),
        ("full_suite", "FULL_SUITE_VERIFIED"),
    ):
        manager.store.add_verification_receipt(
            TX_ID,
            {
                "verification_kind": phase,
                "base_commit": "c" * 40,
                "patch_digest": "1" * 64,
                "candidate_tree_digest": "8" * 64,
                "test_plan_id": "l5.product-completion-reporting.v1",
                "test_plan_digest": protected_test_plan_digest("l5.product-completion-reporting.v1"),
                "exit_status": 0,
                "result": "pass",
            },
        )
        manager.record_result(TX_ID, step=f"verification:{phase}", result_state=state)
    material = {"transaction_id": TX_ID, "policy_digest": _policy()["policy_digest"]}
    authorization_digest = digest_json(material)
    manager.store.consume_policy(
        transaction_id=TX_ID,
        policy_digest=_policy()["policy_digest"],
        authorization_receipt_digest=authorization_digest,
        payload={"authorization_material": material, "authorized_policy": _policy()},
    )
    manager.record_result(TX_ID, step="policy_authorization", result_state="POLICY_AUTHORIZED")
    manager.begin_intent(TX_ID, step="commit", intent_state="COMMIT_INTENT")
    manager.record_result(
        TX_ID,
        step="commit",
        result_state="COMMITTED",
        updates={"candidate_commit": "9" * 40, "prior_commit": "c" * 40},
    )
    adapter = _RecordingAdapter(tmp_path / "candidate")

    result = CodeEvolutionEffectOwner(
        runtime,
        owner_id="effect-test",
        adapter=adapter,
        policy_loader=lambda: pytest.fail("authorized policy snapshot must be reused"),
        policy_consumer=lambda **_kwargs: pytest.fail("policy must not be consumed twice"),
    ).execute(TX_ID)

    assert result["ok"] is True
    assert manager.store.get_transaction(TX_ID)["current_state"] == "OBSERVING"
    assert adapter.calls == ["push", "deploy"]
    runtime.close()


def test_effect_owner_resumes_after_policy_consumption_crash_from_atomic_snapshot(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    manager = _ready_manager(runtime)
    first_adapter = _RecordingAdapter(tmp_path / "candidate-first")

    def consume_then_crash(*, transaction_id, expected_digest, **_kwargs):
        material = {"transaction_id": transaction_id, "policy_digest": expected_digest}
        manager.store.consume_policy(
            transaction_id=transaction_id,
            policy_digest=expected_digest,
            authorization_receipt_digest=digest_json(material),
            payload={"authorization_material": material, "authorized_policy": _policy()},
        )
        raise RuntimeError("simulated_process_crash")

    with pytest.raises(RuntimeError, match="simulated_process_crash"):
        CodeEvolutionEffectOwner(
            runtime,
            owner_id="effect-test",
            adapter=first_adapter,
            policy_loader=lambda: _policy(),
            policy_consumer=consume_then_crash,
        ).execute(TX_ID)

    interrupted = manager.store.get_transaction(TX_ID)
    assert interrupted["current_state"] == "FULL_SUITE_VERIFIED"
    assert interrupted["authorization_digest"]
    assert interrupted["payload"]["authorized_policy"]["policy_digest"] == _policy()["policy_digest"]

    second_adapter = _RecordingAdapter(tmp_path / "candidate-second")
    resumed = CodeEvolutionEffectOwner(
        runtime,
        owner_id="effect-test",
        adapter=second_adapter,
        policy_loader=lambda: pytest.fail("durable authorized policy must be reused"),
        policy_consumer=lambda **_kwargs: pytest.fail("policy must not be consumed twice"),
    ).execute(TX_ID)

    assert resumed["ok"] is True
    assert manager.store.get_transaction(TX_ID)["current_state"] == "OBSERVING"
    runtime.close()


def test_deploy_failure_cannot_claim_clean_rollback_while_storage_marker_exists(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    manager = _ready_manager(runtime)
    marker = tmp_path / "runtime" / "state" / "storage-release-transaction.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text('{"phase":"rollback_validated"}', encoding="utf-8")
    adapter = _RecordingAdapter(tmp_path / "candidate")
    adapter.fail_deploy = True
    adapter.landed_on_failed_deploy = True

    def consume(*, transaction_id, expected_digest, **_kwargs):
        material = {"transaction_id": transaction_id, "policy_digest": expected_digest}
        return {
            "ok": True,
            **manager.store.consume_policy(
                transaction_id=transaction_id,
                policy_digest=expected_digest,
                authorization_receipt_digest=digest_json(material),
                payload={"authorization_material": material, "authorized_policy": _policy()},
            ),
        }

    result = CodeEvolutionEffectOwner(
        runtime,
        owner_id="effect-test",
        adapter=adapter,
        policy_loader=lambda: _policy(),
        policy_consumer=consume,
    ).execute(TX_ID)

    assert result["blocked_reason"] == "code_evolution_rollback_state_unknown"
    assert manager.store.get_transaction(TX_ID)["current_state"] == "RECOVERY_QUARANTINED"
    runtime.close()


def test_deploy_failure_without_landing_does_not_claim_healthy_rollback(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    manager = _ready_manager(runtime)
    adapter = _RecordingAdapter(tmp_path / "candidate")
    adapter.fail_deploy = True

    def consume(*, transaction_id, expected_digest, **_kwargs):
        material = {"transaction_id": transaction_id, "policy_digest": expected_digest}
        return {
            "ok": True,
            **manager.store.consume_policy(
                transaction_id=transaction_id,
                policy_digest=expected_digest,
                authorization_receipt_digest=digest_json(material),
                payload={"authorization_material": material, "authorized_policy": _policy()},
            ),
        }

    result = CodeEvolutionEffectOwner(
        runtime,
        owner_id="effect-test",
        adapter=adapter,
        policy_loader=lambda: _policy(),
        policy_consumer=consume,
    ).execute(TX_ID)

    transaction = manager.store.get_transaction(TX_ID)
    assert result["blocked_reason"] == "code_evolution_deploy_unlanded"
    assert transaction["current_state"] == "ABORTED_CANDIDATE_RESTORED"
    assert transaction.get("deployed_commit") in {"", None}
    receipt = manager.store.get_terminal_receipt(TX_ID) or {}
    assert receipt.get("outcome") == "aborted_candidate_restored"
    assert "rollback" in adapter.calls
    runtime.close()


def test_l5_observation_semantics_accept_only_incident_specific_pending_gap() -> None:
    gaps = [
        "terminal_receipt_unbound",
        "transaction_evidence_unverified",
        "no_qualifying_terminal_receipt",
        "nonterminal_transaction_exists",
        "observation_not_valid",
    ]
    report = {
        "schema": "l5.reader.v4",
        "schema_version": "l5_readiness.v4",
        "report_type": "l5_readiness_report",
        "reader_mode": "v3",
        "profile_key": "l5.default:v1",
        "ok": False,
        "product_l5_complete": False,
        "completion_status": "incomplete",
        "status": "incomplete",
        "control_plane_ok": True,
        "control_plane_status": "ready",
        "axes": {
            "capability_ready": True,
            "adapter_ready": True,
            "deployment_assurance": "ready",
        },
        "code_evolution": {
            "provider_ready": True,
            "catalog_ready": True,
            "advertisement_fresh": True,
            "transaction_verified": False,
            "current_lineage_compatible": True,
            "gaps": gaps,
        },
        "transaction_evidence": {"nonterminal": True, "quarantined": False},
        "gaps": gaps,
    }

    accepted, accepted_measure = _l5_observation_semantics(
        report,
        {"profile_key": "l5.default:v1"},
    )
    broken = {
        **report,
        "control_plane_ok": False,
        "gaps": [*gaps, "raw_control_plane_not_ready"],
        "code_evolution": {
            **report["code_evolution"],
            "gaps": [*gaps, "raw_control_plane_not_ready"],
        },
    }
    rejected, rejected_measure = _l5_observation_semantics(
        broken,
        {"profile_key": "l5.default:v1"},
    )

    assert accepted is True
    assert all(accepted_measure["checks"].values())
    assert rejected is False
    assert rejected_measure["checks"]["control_plane"] is False
    assert rejected_measure["checks"]["gaps_incident_specific"] is False
