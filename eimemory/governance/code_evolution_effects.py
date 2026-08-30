"""Protected external-effect owner for strict code-evolution transactions.

Only this module constructs Git, verification, and immutable-deployment
effects. Proposal/provider payloads remain data: they can select a registered
test plan and provide bounded file contents, but cannot supply commands,
environment variables, repository coordinates, or secrets.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import time
from typing import Any, Protocol

from eimemory.governance.code_automation_policy import (
    CODE_AUTOMATION_POLICY_DEFAULT_PATH,
    CODE_AUTOMATION_POLICY_PATH_ENV,
    consume_code_automation_policy,
    load_code_automation_policy,
)
from eimemory.governance.code_evolution_test_plans import (
    allowed_files_for_incident,
    build_test_plan_argv,
    protected_test_plan,
    protected_test_plan_digest,
)
from eimemory.governance.code_evolution_repository import (
    protected_paths_digest_at_commit,
    remote_url_digest,
)
from eimemory.governance.deployment_receipt import verify_and_record_deployment
from eimemory.models.records import ScopeRef
from eimemory.storage.code_evolution_store import CodeEvolutionConflict, digest_json, utc_now


TRUSTED_REPOSITORY_ROOT = Path("/dev-project/eimemory")
TRUSTED_REMOTE = "origin"
TRUSTED_BRANCH = "master"
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TRANSACTION = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


@dataclass(frozen=True, slots=True)
class CandidateMaterialization:
    root: Path
    tree_digest: str
    allowed_files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VerificationResult:
    exit_status: int
    passed_count: int
    failed_count: int
    skipped_count: int
    output: bytes


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    ok: bool
    receipt_digest: str
    deployed_commit: str
    version: str
    evidence: Mapping[str, Any]


class EffectAdapter(Protocol):
    def materialize(self, transaction: Mapping[str, Any], policy: Mapping[str, Any], updates: Sequence[Mapping[str, str]]) -> CandidateMaterialization: ...
    def verify(self, candidate: CandidateMaterialization, *, phase: str, argv: Sequence[str], heartbeat: Callable[[], Any]) -> VerificationResult: ...
    def commit(self, candidate: CandidateMaterialization, *, transaction_id: str, base_commit: str, allowed_files: Sequence[str]) -> str: ...
    def push(self, *, transaction: Mapping[str, Any], policy: Mapping[str, Any], candidate_commit: str, base_commit: str, remote: str, branch: str) -> Mapping[str, Any]: ...
    def deploy(self, runtime: Any, *, transaction: Mapping[str, Any], policy: Mapping[str, Any], verification_receipt_digests: Sequence[str], observation_deadline: str, heartbeat: Callable[[], Any]) -> DeploymentResult: ...
    def rollback(self, runtime: Any, *, transaction: Mapping[str, Any], policy: Mapping[str, Any], heartbeat: Callable[[], Any]) -> Mapping[str, Any]: ...
    def cleanup(self, candidate: CandidateMaterialization) -> None: ...


def validated_file_updates(transaction: Mapping[str, Any], policy: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return normalized file updates after strict plan and size validation."""

    proposal = _proposal(transaction)
    updates = proposal.get("file_updates")
    patch = policy.get("patch") if isinstance(policy.get("patch"), Mapping) else {}
    verification = policy.get("verification") if isinstance(policy.get("verification"), Mapping) else {}
    test_plan = proposal.get("test_plan") if isinstance(proposal.get("test_plan"), Mapping) else {}
    plan_id = str(test_plan.get("id") or "")
    plan = protected_test_plan(plan_id)
    if (
        plan is None
        or str(test_plan.get("digest") or "") != plan.digest
        or str(verification.get("test_plan_id") or "") != plan_id
        or str(verification.get("test_plan_digest") or "") != plan.digest
    ):
        raise ValueError("protected_test_plan_mismatch")
    if plan.allowed_files != allowed_files_for_incident(
        str(transaction.get("incident_class") or ""),
        test_plan_id=plan_id,
    ):
        raise ValueError("incident_test_plan_mismatch")
    if not isinstance(updates, list) or not updates:
        raise ValueError("file_updates_required")
    allowed = tuple(str(item) for item in patch.get("allowed_files") or ())
    if allowed != plan.allowed_files:
        raise ValueError("policy_allowed_files_mismatch")
    max_files = int(patch.get("max_files") or 0)
    max_file_bytes = int(patch.get("max_file_bytes") or 0)
    max_total_bytes = int(patch.get("max_total_bytes") or 0)
    if not 1 <= len(updates) <= max_files <= len(allowed):
        raise ValueError("file_update_count_invalid")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for raw in updates:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "prior_sha256", "content"}:
            raise ValueError("file_update_fields_invalid")
        relative = str(raw.get("path") or "").replace("\\", "/")
        pure = PurePosixPath(relative)
        if not relative or pure.is_absolute() or ".." in pure.parts or "." in pure.parts or relative not in allowed or relative in seen:
            raise ValueError("file_update_path_not_allowed")
        prior = str(raw.get("prior_sha256") or "").strip().lower()
        content = raw.get("content")
        if _HEX64.fullmatch(prior) is None or not isinstance(content, str):
            raise ValueError("file_update_identity_invalid")
        encoded = content.replace("\r\n", "\n").encode("utf-8")
        if not encoded or len(encoded) > max_file_bytes:
            raise ValueError("file_update_size_invalid")
        total_bytes += len(encoded)
        if total_bytes > max_total_bytes:
            raise ValueError("file_update_total_size_invalid")
        seen.add(relative)
        normalized.append({"path": relative, "prior_sha256": prior, "content": encoded.decode("utf-8")})
    return normalized


class CodeEvolutionEffectOwner:
    """Drive one ledger transaction through protected external effects."""

    def __init__(self, runtime: Any, *, owner_id: str, adapter: EffectAdapter, policy_loader: Callable[[], dict[str, Any]], policy_consumer: Callable[..., dict[str, Any]]) -> None:
        from eimemory.governance.code_evolution_transaction import CodeEvolutionTransactionManager

        self.runtime = runtime
        self.manager = CodeEvolutionTransactionManager(runtime, owner_id=owner_id)
        self.adapter = adapter
        self.policy_loader = policy_loader
        self.policy_consumer = policy_consumer

    def execute(self, transaction_id: str) -> dict[str, Any]:
        from eimemory.governance.code_evolution_transaction import (
            FORWARD_EFFECT_STATES,
            ReconciliationDecision,
            effect_execution_authorized,
        )

        self.manager.acquire_lease(transaction_id)
        candidate: CandidateMaterialization | None = None
        candidate_cleaned = False
        preserve_candidate_for_recovery = False

        def cleanup_candidate() -> None:
            nonlocal candidate_cleaned
            if candidate is not None and not candidate_cleaned:
                self.adapter.cleanup(candidate)
                candidate_cleaned = True

        def heartbeat() -> None:
            self.manager.renew_lease(transaction_id)
        try:
            transaction = self.manager.store.get_transaction(transaction_id)
            if transaction is None:
                return _blocked(transaction_id, "code_evolution_transaction_not_found")
            if not effect_execution_authorized(transaction):
                return _blocked(transaction_id, "code_evolution_effect_execution_not_authorized", transaction)
            post_commit_states = {"COMMITTED", "PUSH_INTENT", "PUSHED", "DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY"}
            if transaction.get("current_state") not in FORWARD_EFFECT_STATES:
                return _blocked(transaction_id, "code_evolution_effect_state_invalid", transaction)
            payload = transaction.get("payload") if isinstance(transaction.get("payload"), Mapping) else {}
            authorized_policy = payload.get("authorized_policy") if isinstance(payload.get("authorized_policy"), Mapping) else {}
            policy = dict(authorized_policy) if str(transaction.get("authorization_digest") or "") and str(authorized_policy.get("policy_digest") or "") == str(transaction.get("policy_digest") or "") else self.policy_loader()
            if policy.get("ok") is not True:
                if transaction.get("current_state") in post_commit_states:
                    current = transaction
                elif transaction.get("current_state") == "PATCH_VALIDATED":
                    current = self.manager.effect_disabled(transaction_id, step=str(policy.get("reason") or "machine_policy_blocked"))
                else:
                    current = self._abort_candidate(transaction_id, reason=str(policy.get("reason") or "machine_policy_blocked"), evidence_digest=digest_json(policy))
                return _blocked(transaction_id, str(policy.get("reason") or "machine_policy_blocked"), current)
            effects = policy.get("effects") if isinstance(policy.get("effects"), Mapping) else {}
            if not all(effects.get(name) is True for name in ("commit", "push", "deployment", "rollback", "sedimentation")):
                if transaction.get("current_state") in post_commit_states:
                    current = transaction
                elif transaction.get("current_state") == "PATCH_VALIDATED":
                    current = self.manager.effect_disabled(transaction_id, step="policy_forward_effects_incomplete")
                else:
                    current = self._abort_candidate(transaction_id, reason="policy_forward_effects_incomplete", evidence_digest=digest_json(effects))
                return _blocked(transaction_id, "code_evolution_policy_forward_effects_incomplete", current)
            if transaction.get("current_state") in post_commit_states:
                preserve_candidate_for_recovery = True
                return self._continue_post_commit(transaction_id, transaction, policy, heartbeat)
            if isinstance(self.adapter, ProductionEffectAdapter):
                authority_error = _live_proposal_authority_error(self.runtime, transaction)
                if authority_error:
                    if transaction.get("current_state") == "PATCH_VALIDATED":
                        current = self.manager.effect_disabled(transaction_id, step=authority_error)
                    else:
                        current = self._abort_candidate(transaction_id, reason=authority_error, evidence_digest=digest_json({"reason": authority_error}))
                    return _blocked(transaction_id, authority_error, current)
            try:
                updates = validated_file_updates(transaction, policy)
                candidate = self.adapter.materialize(transaction, policy, updates)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                if transaction.get("current_state") == "PATCH_VALIDATED":
                    current = self.manager.effect_disabled(transaction_id, step=f"candidate_materialization:{type(exc).__name__}")
                else:
                    current = self._abort_candidate(transaction_id, reason="candidate_materialization_failed", evidence_digest=digest_json({"error": type(exc).__name__}))
                return _blocked(transaction_id, "code_evolution_candidate_materialization_failed", current)
            if _HEX64.fullmatch(candidate.tree_digest) is None:
                cleanup_candidate()
                if transaction.get("current_state") == "PATCH_VALIDATED":
                    current = self.manager.effect_disabled(transaction_id, step="candidate_tree_digest_invalid")
                else:
                    current = self._abort_candidate(transaction_id, reason="candidate_tree_digest_invalid", evidence_digest=digest_json({"candidate_tree_digest": candidate.tree_digest}))
                return _blocked(transaction_id, "code_evolution_candidate_tree_digest_invalid", current)
            if transaction.get("current_state") == "PATCH_VALIDATED":
                transaction = self.manager.record_result(
                    transaction_id,
                    step="candidate_materialization",
                    result_state="CANDIDATE_MATERIALIZED",
                    output_data={"candidate_tree_digest": candidate.tree_digest, "allowed_files": list(candidate.allowed_files)},
                    updates={"candidate_tree_digest": candidate.tree_digest},
                )
            elif str(transaction.get("candidate_tree_digest") or "") != candidate.tree_digest:
                cleanup_candidate()
                return _blocked(transaction_id, "code_evolution_candidate_tree_identity_conflict", transaction)
            proposal = _proposal(transaction)
            test_plan = proposal.get("test_plan") if isinstance(proposal.get("test_plan"), Mapping) else {}
            plan_id = str(test_plan.get("id") or "")
            receipt_digests: list[str] = []
            state_order = {
                "CANDIDATE_MATERIALIZED": 0,
                "FOCUSED_VERIFIED": 1,
                "REGRESSION_VERIFIED": 2,
                "FULL_SUITE_VERIFIED": 3,
                "POLICY_AUTHORIZED": 4,
                "COMMIT_INTENT": 5,
            }
            existing_receipts = {
                str(item.get("verification_kind") or ""): item
                for item in self.manager.store.list_verification_receipts(transaction_id)
            }
            for phase, state in (("focused", "FOCUSED_VERIFIED"), ("regression", "REGRESSION_VERIFIED"), ("full_suite", "FULL_SUITE_VERIFIED")):
                existing = existing_receipts.get(phase)
                if existing is not None:
                    if existing.get("result") != "pass" or int(existing.get("exit_status", 1)) != 0:
                        cleanup_candidate()
                        terminal = self._abort_candidate(transaction_id, reason=f"{phase}_verification_failed", evidence_digest=str(existing.get("receipt_digest") or ""))
                        return _blocked(transaction_id, f"code_evolution_{phase}_verification_failed", terminal)
                    receipt_digests.append(str(existing.get("receipt_digest") or ""))
                    if state_order.get(str(transaction.get("current_state") or ""), -1) < state_order[state]:
                        transaction = self.manager.record_result(transaction_id, step=f"verification:{phase}", result_state=state, output_data={"receipt_digest": existing.get("receipt_digest")})
                    continue
                argv = build_test_plan_argv(plan_id, phase, candidate_python=_trusted_python())
                started = utc_now()
                heartbeat()
                verification = self.adapter.verify(candidate, phase=phase, argv=argv, heartbeat=heartbeat)
                heartbeat()
                artifact = self.manager.store.store_artifact(
                    transaction_id,
                    artifact_kind=f"verification_log_{phase}",
                    artifact_schema="code_evolution_verification_log.v1",
                    data=bytes(verification.output[: 1024 * 1024]),
                )
                receipt = self.manager.store.add_verification_receipt(
                    transaction_id,
                    {
                        "verification_kind": phase,
                        "base_commit": str(transaction.get("base_commit") or ""),
                        "patch_digest": str(transaction.get("patch_digest") or ""),
                        "candidate_tree_digest": candidate.tree_digest,
                        "test_plan_id": plan_id,
                        "test_plan_digest": protected_test_plan_digest(plan_id),
                        "command_digest": digest_json(list(argv)),
                        "environment_digest": digest_json({"python": str(_trusted_python()), "cwd": str(candidate.root)}),
                        "verifier_id": "eimemory.protected-test-plan",
                        "verifier_revision": "v1",
                        "started_at": started,
                        "finished_at": utc_now(),
                        "exit_status": int(verification.exit_status),
                        "test_count": int(verification.passed_count + verification.failed_count + verification.skipped_count),
                        "passed_count": int(verification.passed_count),
                        "failed_count": int(verification.failed_count),
                        "skipped_count": int(verification.skipped_count),
                        "result": "pass" if verification.exit_status == 0 else "fail",
                        "log_artifact_digest": str(artifact.get("sha256") or ""),
                    },
                )
                if verification.exit_status != 0:
                    cleanup_candidate()
                    terminal = self._abort_candidate(transaction_id, reason=f"{phase}_verification_failed", evidence_digest=str(receipt.get("receipt_digest") or ""))
                    return _blocked(transaction_id, f"code_evolution_{phase}_verification_failed", terminal)
                receipt_digests.append(str(receipt["receipt_digest"]))
                transaction = self.manager.record_result(transaction_id, step=f"verification:{phase}", result_state=state, output_data={"receipt_digest": receipt["receipt_digest"]})

            if not str(transaction.get("authorization_digest") or ""):
                consumed = self.policy_consumer(
                    path=str(policy.get("policy_path") or ""),
                    transaction_id=transaction_id,
                    expected_digest=str(policy.get("policy_digest") or ""),
                    store=getattr(self.runtime, "store", self.runtime),
                )
                if consumed.get("ok") is not True:
                    cleanup_candidate()
                    terminal = self._abort_candidate(transaction_id, reason=str(consumed.get("reason") or "policy_consumption_failed"), evidence_digest=digest_json(consumed))
                    return _blocked(transaction_id, str(consumed.get("reason") or "policy_consumption_failed"), terminal)
                transaction = self.manager.store.get_transaction(transaction_id) or transaction
                persisted_payload = transaction.get("payload") if isinstance(transaction.get("payload"), Mapping) else {}
                persisted_policy = persisted_payload.get("authorized_policy") if isinstance(persisted_payload.get("authorized_policy"), Mapping) else {}
                if str(persisted_policy.get("policy_digest") or "") != str(policy.get("policy_digest") or ""):
                    cleanup_candidate()
                    return _blocked(transaction_id, "code_evolution_authorized_policy_snapshot_missing", transaction)
            if transaction.get("current_state") == "FULL_SUITE_VERIFIED":
                transaction = self.manager.record_result(transaction_id, step="policy_authorization", result_state="POLICY_AUTHORIZED", output_data={"policy_digest": policy.get("policy_digest")})

            heartbeat()
            if isinstance(self.adapter, ProductionEffectAdapter) and _complete_worktree_digest(candidate.root) != candidate.tree_digest:
                cleanup_candidate()
                terminal = self._abort_candidate(
                    transaction_id,
                    reason="candidate_tree_changed_during_verification",
                    evidence_digest=digest_json({"candidate_tree_digest": candidate.tree_digest}),
                )
                return _blocked(transaction_id, "code_evolution_candidate_tree_changed_during_verification", terminal)
            if transaction.get("current_state") != "COMMIT_INTENT":
                self.manager.begin_intent(transaction_id, step="commit", intent_state="COMMIT_INTENT", input_data={"base_commit": transaction.get("base_commit"), "candidate_tree_digest": candidate.tree_digest})
            preserve_candidate_for_recovery = True
            try:
                candidate_commit = self.adapter.commit(candidate, transaction_id=transaction_id, base_commit=str(transaction.get("base_commit") or ""), allowed_files=candidate.allowed_files)
                if _HEX40.fullmatch(candidate_commit) is None:
                    raise ValueError("candidate_commit_invalid")
            except (OSError, RuntimeError, TypeError, ValueError):
                current = self.manager.store.get_transaction(transaction_id) or {}
                return _blocked(transaction_id, "code_evolution_commit_awaiting_reconciliation", current)
            heartbeat()
            transaction = self.manager.record_result(transaction_id, step="commit", result_state="COMMITTED", output_data={"candidate_commit": candidate_commit}, updates={"candidate_commit": candidate_commit, "prior_commit": str(transaction.get("base_commit") or "")})

            self.manager.begin_intent(transaction_id, step="push", intent_state="PUSH_INTENT", input_data={"base_commit": transaction.get("base_commit"), "candidate_commit": candidate_commit})
            heartbeat()
            try:
                push = self.adapter.push(transaction=transaction, policy=policy, candidate_commit=candidate_commit, base_commit=str(transaction.get("base_commit") or ""), remote=TRUSTED_REMOTE, branch=TRUSTED_BRANCH)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                current = self.manager.store.get_transaction(transaction_id) or {}
                return _blocked(transaction_id, f"code_evolution_push_awaiting_reconciliation:{type(exc).__name__}", current)
            heartbeat()
            if str(push.get("remote_sha") or "") != candidate_commit:
                quarantined = self.manager.reconcile(transaction_id, step="push", decision=ReconciliationDecision("quarantine", "push_effect_state_unknown", quarantine_required=True, evidence_digest=digest_json(push)))
                return _blocked(transaction_id, "code_evolution_push_state_unknown", quarantined)
            transaction = self.manager.record_result(transaction_id, step="push", result_state="PUSHED", output_data={"remote_sha": candidate_commit})

            observation_seconds = int((policy.get("deployment") or {}).get("observation_seconds") or 0)
            started_at = datetime.now(timezone.utc)
            deadline = (started_at + timedelta(seconds=observation_seconds)).isoformat(timespec="seconds")
            transaction = self.manager.update_metadata(
                transaction_id,
                payload_updates={"verification_receipt_digests": receipt_digests},
                updates={"observation_started_at": started_at.isoformat(timespec="seconds"), "observation_deadline": deadline},
            )
            transaction = self.manager.begin_intent(transaction_id, step="deployment", intent_state="DEPLOY_INTENT", input_data={"candidate_commit": candidate_commit, "observation_deadline": deadline})
            deployment = self.adapter.deploy(self.runtime, transaction=transaction, policy=policy, verification_receipt_digests=receipt_digests, observation_deadline=deadline, heartbeat=heartbeat)
            heartbeat()
            if not deployment.ok or deployment.deployed_commit != candidate_commit or _HEX64.fullmatch(deployment.receipt_digest) is None:
                result = self._rollback_after_deploy_failure(transaction_id, policy, deployment.evidence)
                cleanup_candidate()
                return result
            deployment_payload = dict(transaction.get("payload") or {})
            deployment_payload.update(
                {
                    "deployment_receipt_digest": deployment.receipt_digest,
                    "candidate_pushed_and_deployed": True,
                    "deployment_version": deployment.version,
                }
            )
            transaction = self.manager.record_result(
                transaction_id,
                step="deployment",
                result_state="DEPLOYED_VERIFIED",
                output_data={"deployment_receipt_digest": deployment.receipt_digest, "version": deployment.version},
                updates={"deployed_commit": candidate_commit, "payload_json": deployment_payload},
            )
            transaction = self.manager.record_result(transaction_id, step="health", result_state="HEALTHY", output_data={"deployment_receipt_digest": deployment.receipt_digest})
            transaction = self._start_observing(transaction_id, transaction, policy)
            cleanup_candidate()
            return {"ok": True, "applied": True, "blocked_reason": "", "transaction_id": transaction_id, "transaction": transaction, "deployment_receipt_digest": deployment.receipt_digest, "observation_deadline": transaction["payload"]["observation_effective_deadline"]}
        finally:
            if not preserve_candidate_for_recovery:
                cleanup_candidate()
            try:
                self.manager.release_lease(transaction_id)
            except CodeEvolutionConflict:
                pass

    def _continue_post_commit(
        self,
        transaction_id: str,
        transaction: Mapping[str, Any],
        policy: Mapping[str, Any],
        heartbeat: Callable[[], Any],
    ) -> dict[str, Any]:
        """Continue a reconciled forward effect without replaying prior phases."""

        from eimemory.governance.code_evolution_transaction import ReconciliationDecision

        current = dict(transaction)
        candidate_commit = str(current.get("candidate_commit") or "")
        if _HEX40.fullmatch(candidate_commit) is None:
            return _blocked(transaction_id, "code_evolution_recovered_candidate_commit_invalid", current)
        receipts = self.manager.store.list_verification_receipts(transaction_id)
        receipt_digests = [
            str(item.get("receipt_digest") or "")
            for item in receipts
            if item.get("result") == "pass" and int(item.get("exit_status", 1)) == 0
        ]
        if len(receipt_digests) != 3 or len(set(receipt_digests)) != 3:
            return _blocked(transaction_id, "code_evolution_recovered_verification_receipts_invalid", current)
        state = str(current.get("current_state") or "")
        if state in {"COMMITTED", "PUSH_INTENT"} and isinstance(self.adapter, ProductionEffectAdapter):
            authority_error = _live_proposal_authority_error(self.runtime, current)
            if authority_error:
                return _blocked(transaction_id, authority_error, current)
        if state == "COMMITTED":
            current = self.manager.begin_intent(
                transaction_id,
                step="push",
                intent_state="PUSH_INTENT",
                input_data={"base_commit": current.get("base_commit"), "candidate_commit": candidate_commit},
            )
            state = "PUSH_INTENT"
        if state == "PUSH_INTENT":
            heartbeat()
            try:
                push = self.adapter.push(
                    transaction=current,
                    policy=policy,
                    candidate_commit=candidate_commit,
                    base_commit=str(current.get("base_commit") or ""),
                    remote=TRUSTED_REMOTE,
                    branch=TRUSTED_BRANCH,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                current = self.manager.store.get_transaction(transaction_id) or {}
                return _blocked(transaction_id, f"code_evolution_push_awaiting_reconciliation:{type(exc).__name__}", current)
            heartbeat()
            if str(push.get("remote_sha") or "") != candidate_commit:
                quarantined = self.manager.reconcile(
                    transaction_id,
                    step="push",
                    decision=ReconciliationDecision(
                        "quarantine",
                        "push_effect_state_unknown",
                        quarantine_required=True,
                        evidence_digest=digest_json(push),
                    ),
                )
                return _blocked(transaction_id, "code_evolution_push_state_unknown", quarantined)
            current = self.manager.record_result(
                transaction_id,
                step="push",
                result_state="PUSHED",
                output_data={"remote_sha": candidate_commit},
            )
            state = "PUSHED"
        if state == "PUSHED":
            started_at = datetime.now(timezone.utc)
            observation_seconds = int((policy.get("deployment") or {}).get("observation_seconds") or 0)
            deadline = (started_at + timedelta(seconds=observation_seconds)).isoformat(timespec="seconds")
            current = self.manager.update_metadata(
                transaction_id,
                payload_updates={"verification_receipt_digests": receipt_digests},
                updates={
                    "observation_started_at": started_at.isoformat(timespec="seconds"),
                    "observation_deadline": deadline,
                },
            )
            current = self.manager.begin_intent(
                transaction_id,
                step="deployment",
                intent_state="DEPLOY_INTENT",
                input_data={"candidate_commit": candidate_commit, "observation_deadline": deadline},
            )
            state = "DEPLOY_INTENT"
        if state == "DEPLOY_INTENT":
            deadline = str(current.get("observation_deadline") or "")
            heartbeat()
            deployment = self.adapter.deploy(
                self.runtime,
                transaction=current,
                policy=policy,
                verification_receipt_digests=receipt_digests,
                observation_deadline=deadline,
                heartbeat=heartbeat,
            )
            heartbeat()
            if not deployment.ok or deployment.deployed_commit != candidate_commit or _HEX64.fullmatch(deployment.receipt_digest) is None:
                return self._rollback_after_deploy_failure(transaction_id, policy, deployment.evidence)
            deployment_payload = dict(current.get("payload") or {})
            deployment_payload.update(
                {
                    "deployment_receipt_digest": deployment.receipt_digest,
                    "candidate_pushed_and_deployed": True,
                    "deployment_version": deployment.version,
                }
            )
            current = self.manager.record_result(
                transaction_id,
                step="deployment",
                result_state="DEPLOYED_VERIFIED",
                output_data={"deployment_receipt_digest": deployment.receipt_digest, "version": deployment.version},
                updates={"deployed_commit": candidate_commit, "payload_json": deployment_payload},
            )
            state = "DEPLOYED_VERIFIED"
        payload = current.get("payload") if isinstance(current.get("payload"), Mapping) else {}
        receipt_digest = str(payload.get("deployment_receipt_digest") or "")
        if state == "DEPLOYED_VERIFIED":
            if str(current.get("deployed_commit") or "") != candidate_commit or _HEX64.fullmatch(receipt_digest) is None:
                return _blocked(transaction_id, "code_evolution_recovered_deployment_evidence_invalid", current)
            current = self.manager.record_result(
                transaction_id,
                step="health",
                result_state="HEALTHY",
                output_data={"deployment_receipt_digest": receipt_digest},
            )
            state = "HEALTHY"
        if state == "HEALTHY":
            current = self._start_observing(transaction_id, current, policy)
            _cleanup_recovered_worktree(current)
            return {
                "ok": True,
                "applied": True,
                "blocked_reason": "",
                "transaction_id": transaction_id,
                "transaction": current,
                "deployment_receipt_digest": receipt_digest,
                "observation_deadline": current["payload"]["observation_effective_deadline"],
                "resumed": True,
            }
        return _blocked(transaction_id, "code_evolution_forward_resume_state_invalid", current)

    def _start_observing(self, transaction_id, transaction, policy):
        """Start the real clock after verified deployment, including recovery.

        The installer-bound deadline remains immutable receipt evidence.  The
        effective observation deadline cannot precede 48 hours after HEALTHY.
        """
        from eimemory.governance.code_evolution_observation import OBSERVATION_HOURS, parse_observation_time

        started_at = datetime.now(timezone.utc)
        duration = max(OBSERVATION_HOURS * 3600, int((policy.get("deployment") or {}).get("observation_seconds") or 0))
        deadline = started_at + timedelta(seconds=duration)
        receipt_deadline = parse_observation_time(str(transaction.get("observation_deadline") or ""))
        if receipt_deadline is not None:
            deadline = max(deadline, receipt_deadline)
        window = {
            "observation_started_at": started_at.isoformat(timespec="seconds"),
            "observation_effective_deadline": deadline.isoformat(timespec="seconds"),
        }
        payload = {**dict(transaction.get("payload") or {}), **window}
        return self.manager.record_result(
            transaction_id, step="observation", result_state="OBSERVING", output_data=window,
            updates={"observation_started_at": window["observation_started_at"], "payload_json": payload},
        )

    def _abort_candidate(self, transaction_id: str, *, reason: str, evidence_digest: str) -> dict[str, Any]:
        current = self.manager.store.get_transaction(transaction_id) or {}
        self.manager.terminalize(
            transaction_id,
            {
                "outcome": "aborted_candidate_restored",
                "incident_digest": str(current.get("incident_digest") or ""),
                "provider_digest": str(current.get("implementation_digest") or ""),
                "policy_digest": str(current.get("policy_digest") or ""),
                "authorization_digest": str(current.get("authorization_digest") or ""),
                "base_commit": str(current.get("base_commit") or ""),
                "candidate_commit": str(current.get("candidate_commit") or ""),
                "evidence_digest": evidence_digest or digest_json({"reason": reason}),
                "reason": reason,
                "created_at": utc_now(),
            },
            terminal_state="ABORTED_CANDIDATE_RESTORED",
        )
        return self.manager.store.get_transaction(transaction_id) or {}

    def execute_rollback(self, transaction_id: str) -> dict[str, Any]:
        """Execute a durable rollback intent with the protected adapter."""

        from eimemory.governance.code_evolution_transaction import (
            effect_execution_authorized,
            reconcile_rollback,
        )

        self.manager.acquire_lease(transaction_id)
        try:
            transaction = self.manager.store.get_transaction(transaction_id)
            if transaction is None:
                return _blocked(transaction_id, "code_evolution_transaction_not_found")
            if not effect_execution_authorized(transaction) or transaction.get("current_state") != "ROLLBACK_INTENT":
                return _blocked(transaction_id, "code_evolution_rollback_not_authorized", transaction)
            payload = transaction.get("payload") if isinstance(transaction.get("payload"), Mapping) else {}
            policy = payload.get("authorized_policy") if isinstance(payload.get("authorized_policy"), Mapping) else {}
            if (
                policy.get("ok") is not True
                or str(policy.get("policy_digest") or "") != str(transaction.get("policy_digest") or "")
                or not isinstance(policy.get("effects"), Mapping)
                or policy["effects"].get("rollback") is not True
            ):
                return _blocked(transaction_id, "code_evolution_rollback_policy_invalid", transaction)
            rollback = self.adapter.rollback(
                self.runtime,
                transaction=transaction,
                policy=policy,
                heartbeat=lambda: self.manager.renew_lease(transaction_id),
            )
            decision = reconcile_rollback(
                {
                    "current_commit": str(rollback.get("commit") or ""),
                    "prior_commit": str(transaction.get("prior_commit") or transaction.get("base_commit") or ""),
                    "receipt_valid": rollback.get("ok") is True and _HEX64.fullmatch(str(rollback.get("receipt_digest") or "")) is not None,
                    "health_ok": rollback.get("ok") is True,
                    "storage_clean": _storage_release_state(self.runtime) == "clean",
                }
            )
            terminal = self.manager.reconcile(
                transaction_id,
                step="rollback",
                decision=decision,
                success_state="ROLLED_BACK_HEALTHY" if decision.status == "rolled_back_healthy" else None,
            )
            if decision.status == "rolled_back_healthy":
                _cleanup_recovered_worktree(transaction)
                return {"ok": True, "applied": True, "status": "rolled_back_healthy", "transaction_id": transaction_id, "transaction": terminal}
            return _blocked(transaction_id, "code_evolution_rollback_state_unknown", terminal)
        finally:
            try:
                self.manager.release_lease(transaction_id)
            except CodeEvolutionConflict:
                pass

    def _rollback_after_deploy_failure(self, transaction_id: str, policy: Mapping[str, Any], deployment_evidence: Mapping[str, Any]) -> dict[str, Any]:
        from eimemory.governance.code_evolution_transaction import reconcile_rollback

        transaction = self.manager.begin_intent(transaction_id, step="rollback", intent_state="ROLLBACK_INTENT", input_data={"deployment_evidence_digest": digest_json(deployment_evidence)})
        rollback = self.adapter.rollback(self.runtime, transaction=transaction, policy=policy, heartbeat=lambda: self.manager.renew_lease(transaction_id))
        if not _candidate_release_landed(transaction, deployment_evidence):
            current = self.manager.store.get_transaction(transaction_id) or transaction
            terminal = self.manager.terminalize(
                transaction_id,
                {
                    "outcome": "aborted_candidate_restored",
                    "incident_digest": str(current.get("incident_digest") or ""),
                    "provider_digest": str(current.get("implementation_digest") or ""),
                    "policy_digest": str(current.get("policy_digest") or ""),
                    "authorization_digest": str(current.get("authorization_digest") or ""),
                    "base_commit": str(current.get("base_commit") or ""),
                    "candidate_commit": str(current.get("candidate_commit") or ""),
                    "evidence_digest": digest_json(deployment_evidence),
                    "reason": "deploy_unlanded",
                    "created_at": utc_now(),
                },
                terminal_state="ABORTED_CANDIDATE_RESTORED",
            )
            return _blocked(transaction_id, "code_evolution_deploy_unlanded", terminal)
        candidate = str(transaction.get("candidate_commit") or "")
        if str(transaction.get("deployed_commit") or "") != candidate:
            self.manager.update_metadata(
                transaction_id,
                payload_updates={"candidate_pushed_and_deployed": True},
                updates={"deployed_commit": candidate},
            )
        decision = reconcile_rollback(
            {
                "current_commit": str(rollback.get("commit") or ""),
                "prior_commit": str(transaction.get("prior_commit") or transaction.get("base_commit") or ""),
                "receipt_valid": rollback.get("ok") is True and _HEX64.fullmatch(str(rollback.get("receipt_digest") or "")) is not None,
                "health_ok": rollback.get("ok") is True,
                "storage_clean": _storage_release_state(self.runtime) == "clean",
            }
        )
        terminal = self.manager.reconcile(transaction_id, step="rollback", decision=decision, success_state="ROLLED_BACK_HEALTHY" if decision.status == "rolled_back_healthy" else None)
        reason = "code_evolution_deployment_rolled_back" if decision.status == "rolled_back_healthy" else "code_evolution_rollback_state_unknown"
        return _blocked(transaction_id, reason, terminal)


class ProductionEffectAdapter:
    """Fixed production implementation; no caller-provided argv or env."""

    def materialize(self, transaction, policy, updates) -> CandidateMaterialization:
        transaction_id = str(transaction.get("transaction_id") or "")
        if _SAFE_TRANSACTION.fullmatch(transaction_id) is None:
            raise ValueError("transaction_id_path_unsafe")
        repository_root = Path(str(transaction.get("repository_root") or ""))
        if repository_root.resolve() != TRUSTED_REPOSITORY_ROOT.resolve():
            raise ValueError("repository_root_untrusted")
        if str(transaction.get("repository_remote") or "") != TRUSTED_REMOTE:
            raise ValueError("repository_remote_untrusted")
        if str(transaction.get("repository_ref") or "").removeprefix("refs/heads/") != TRUSTED_BRANCH:
            raise ValueError("repository_ref_untrusted")
        actual_remote_url = _git(TRUSTED_REPOSITORY_ROOT, "remote", "get-url", TRUSTED_REMOTE)
        policy_remote_digest = str((policy.get("repository") or {}).get("remote_url_digest") or "")
        if remote_url_digest(actual_remote_url) != policy_remote_digest:
            raise ValueError("repository_remote_url_digest_mismatch")
        base_commit = str(transaction.get("base_commit") or "")
        if _HEX40.fullmatch(base_commit) is None or _git(TRUSTED_REPOSITORY_ROOT, "rev-parse", f"{base_commit}^{{commit}}") != base_commit:
            raise ValueError("base_commit_unavailable")
        protected_files = tuple(
            str(item) for item in (policy.get("patch") or {}).get("allowed_files") or ()
        )
        if protected_paths_digest_at_commit(
            TRUSTED_REPOSITORY_ROOT,
            base_commit,
            protected_files,
            git_blob_reader=lambda root, commit, relative: _git_bytes(root, "show", f"{commit}:{relative}"),
        ) != str(transaction.get("base_tree_digest") or ""):
            raise ValueError("base_tree_digest_mismatch")
        worktree_root = TRUSTED_REPOSITORY_ROOT / ".worktrees"
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_root_metadata = worktree_root.lstat()
        if not stat.S_ISDIR(worktree_root_metadata.st_mode) or stat.S_ISLNK(worktree_root_metadata.st_mode):
            raise ValueError("candidate_worktree_root_untrusted")
        worktree = worktree_root / f"code-evolution-{transaction_id}"
        if worktree.exists():
            if worktree.is_symlink():
                raise ValueError("candidate_worktree_symlink_rejected")
            if str(worktree.resolve()) not in _registered_worktrees():
                raise ValueError("candidate_worktree_not_registered")
            if _git(worktree, "rev-parse", "HEAD") != base_commit:
                raise ValueError("candidate_worktree_identity_conflict")
        else:
            _run_git(TRUSTED_REPOSITORY_ROOT, "worktree", "add", "--detach", str(worktree), base_commit)
        try:
            for update in updates:
                target = worktree / update["path"]
                _validate_regular_path_chain(worktree, target)
                metadata = target.lstat()
                if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("candidate_target_not_regular")
                prior_bytes = target.read_bytes().replace(b"\r\n", b"\n")
                candidate_bytes = update["content"].encode("utf-8")
                if sha256(prior_bytes).hexdigest() != update["prior_sha256"] and prior_bytes != candidate_bytes:
                    raise ValueError("candidate_prior_digest_mismatch")
                if prior_bytes != candidate_bytes:
                    descriptor = os.open(target, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
                    try:
                        with os.fdopen(descriptor, "wb", closefd=False) as handle:
                            handle.write(candidate_bytes)
                            handle.flush()
                            os.fsync(handle.fileno())
                    finally:
                        os.close(descriptor)
            status_output = _git_bytes(
                worktree,
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            )
            changed_paths = _porcelain_changed_paths(status_output)
            if changed_paths != {item["path"] for item in updates}:
                raise ValueError("candidate_worktree_dirty_scope_mismatch")
            diff = _git_bytes(worktree, "diff", "--binary", "--", *[item["path"] for item in updates])
            patch = policy["patch"]
            if not diff:
                raise ValueError("candidate_patch_empty")
            if len(diff) > int(patch["max_diff_bytes"]):
                raise ValueError("candidate_diff_too_large")
            changed_lines = sum(1 for line in diff.splitlines() if line[:1] in {b"+", b"-"} and not line.startswith((b"+++", b"---")))
            if changed_lines > int(patch["max_changed_lines"]):
                raise ValueError("candidate_changed_lines_exceeded")
            return CandidateMaterialization(worktree, _complete_worktree_digest(worktree), tuple(item["path"] for item in updates))
        except Exception:
            if worktree.exists() and not worktree.is_symlink():
                _run_git(TRUSTED_REPOSITORY_ROOT, "worktree", "remove", "--force", str(worktree))
            raise

    def verify(self, candidate, *, phase, argv, heartbeat) -> VerificationResult:
        sandbox = Path("/usr/bin/bwrap")
        if not sandbox.is_file():
            return VerificationResult(126, 0, 1, 0, b"protected verification sandbox unavailable")
        sandbox_argv = [
            str(sandbox),
            "--die-with-parent",
            "--unshare-net",
            "--new-session",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
            "--dir",
            "/tmp/home",
            "--tmpfs",
            "/home/darrow",
            "--tmpfs",
            "/etc/eimemory",
            "--tmpfs",
            "/var/lib/eimemory",
            "--chdir",
            str(candidate.root),
            "--",
            *[str(item) for item in argv],
        ]
        exit_status, output = _run_bounded_process(
            sandbox_argv,
            cwd=candidate.root,
            env=_verification_environment(),
            heartbeat=heartbeat,
        )
        return VerificationResult(exit_status, int(exit_status == 0), int(exit_status != 0), 0, output)

    def commit(self, candidate, *, transaction_id, base_commit, allowed_files) -> str:
        head = _git(candidate.root, "rev-parse", "HEAD")
        if head != base_commit:
            message = _git(candidate.root, "show", "-s", "--format=%B", head)
            parent = _git(candidate.root, "rev-parse", f"{head}^")
            if parent == base_commit and f"Code-Evolution-Transaction: {transaction_id}" in message:
                return head
            raise ValueError("candidate_commit_identity_conflict")
        _run_git(candidate.root, "add", "--", *allowed_files)
        _run_git(candidate.root, "commit", "-m", f"fix: autonomous bounded code evolution\n\nCode-Evolution-Transaction: {transaction_id}")
        candidate_commit = _git(candidate.root, "rev-parse", "HEAD")
        if _git(candidate.root, "rev-parse", f"{candidate_commit}^") != base_commit:
            raise ValueError("candidate_commit_parent_mismatch")
        changed = tuple(filter(None, _git(candidate.root, "diff-tree", "--no-commit-id", "--name-only", "-r", candidate_commit).splitlines()))
        if set(changed) != set(allowed_files):
            raise ValueError("candidate_commit_scope_mismatch")
        return candidate_commit

    def push(self, *, transaction, policy, candidate_commit, base_commit, remote, branch):
        if remote != TRUSTED_REMOTE or branch != TRUSTED_BRANCH:
            raise ValueError("push_coordinates_untrusted")
        actual_remote_url = _git(TRUSTED_REPOSITORY_ROOT, "remote", "get-url", TRUSTED_REMOTE)
        expected_remote_digest = str((policy.get("repository") or {}).get("remote_url_digest") or transaction.get("remote_url_digest") or "")
        if remote_url_digest(actual_remote_url) != expected_remote_digest:
            raise ValueError("push_remote_url_digest_mismatch")
        remote_line = _git(TRUSTED_REPOSITORY_ROOT, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
        remote_sha = remote_line.split()[0] if remote_line else ""
        if _HEX40.fullmatch(remote_sha) is None:
            raise ValueError("push_remote_head_invalid")
        try:
            _run_git(TRUSTED_REPOSITORY_ROOT, "merge-base", "--is-ancestor", remote_sha, base_commit)
        except subprocess.CalledProcessError as exc:
            raise ValueError("push_remote_not_ancestor") from exc
        try:
            _run_git(
                TRUSTED_REPOSITORY_ROOT,
                "push",
                f"--force-with-lease=refs/heads/{branch}:{remote_sha}",
                remote,
                f"{candidate_commit}:refs/heads/{branch}",
            )
        except subprocess.CalledProcessError:
            # The command result is not authoritative: transport can fail after
            # the remote accepted the update.  Re-read the branch and let the
            # exact candidate SHA decide whether the effect landed.
            pass
        remote_line = _git(TRUSTED_REPOSITORY_ROOT, "ls-remote", "--heads", remote, f"refs/heads/{branch}")
        return {"remote_sha": remote_line.split()[0] if remote_line else ""}

    def deploy(self, runtime, *, transaction, policy, verification_receipt_digests, observation_deadline, heartbeat):
        installer = TRUSTED_REPOSITORY_ROOT / "deploy" / "install_immutable_release.sh"
        if sha256(installer.read_bytes()).hexdigest() != str(policy["deployment"]["installer_digest"]):
            return DeploymentResult(False, "", "", "", {"reason": "installer_digest_mismatch"})
        lineage = _code_evolution_lineage(transaction)
        exit_status, output = _run_bounded_process(
            ["bash", str(installer), str(transaction.get("candidate_commit") or "")],
            cwd=TRUSTED_REPOSITORY_ROOT,
            env=_deployment_environment(runtime, transaction=transaction, verification_receipt_digests=verification_receipt_digests, observation_deadline=observation_deadline, lineage=lineage),
            heartbeat=heartbeat,
        )
        if exit_status != 0:
            return DeploymentResult(False, "", "", "", {"reason": "immutable_installer_failed", "output_digest": sha256(output).hexdigest()})
        receipt = verify_and_record_deployment(
            runtime,
            scope=_transaction_scope(transaction),
            repo_root=TRUSTED_REPOSITORY_ROOT,
            current_link=policy["deployment"]["current_link"],
            health_url=policy["deployment"]["health_url"],
            prior_commit=str(transaction.get("prior_commit") or transaction.get("base_commit") or ""),
            deployed_commit=str(transaction.get("candidate_commit") or ""),
            transaction_id=str(transaction.get("transaction_id") or ""),
            authorization_digest=str(transaction.get("authorization_digest") or ""),
            policy_digest=str(transaction.get("policy_digest") or ""),
            patch_digest=str(transaction.get("patch_digest") or ""),
            candidate_tree_digest=str(transaction.get("candidate_tree_digest") or ""),
            verification_receipt_digests=list(verification_receipt_digests),
            observation_deadline=observation_deadline,
            provider_implementation_digest=str(transaction.get("implementation_digest") or ""),
            code_evolution_lineage=lineage,
            strict_transaction=True,
        )
        return DeploymentResult(receipt.get("ok") is True, digest_json(receipt) if receipt.get("ok") is True else "", str(receipt.get("commit") or ""), str(receipt.get("version") or ""), receipt)

    def rollback(self, runtime, *, transaction, policy, heartbeat):
        prior = str(transaction.get("prior_commit") or transaction.get("base_commit") or "")
        installer = TRUSTED_REPOSITORY_ROOT / "deploy" / "install_immutable_release.sh"
        if sha256(installer.read_bytes()).hexdigest() != str(policy["deployment"]["installer_digest"]):
            return {"ok": False, "commit": "", "receipt_digest": "", "reason": "installer_digest_mismatch"}
        exit_status, _output = _run_bounded_process(
            ["bash", str(installer), prior],
            cwd=TRUSTED_REPOSITORY_ROOT,
            env=_base_effect_environment(runtime),
            heartbeat=heartbeat,
        )
        if exit_status != 0:
            return {"ok": False, "commit": "", "receipt_digest": ""}
        receipt = verify_and_record_deployment(runtime, scope=_transaction_scope(transaction), repo_root=TRUSTED_REPOSITORY_ROOT, current_link=policy["deployment"]["current_link"], health_url=policy["deployment"]["health_url"], prior_commit=str(transaction.get("candidate_commit") or ""), deployed_commit=prior)
        return {"ok": receipt.get("ok") is True, "commit": str(receipt.get("commit") or ""), "receipt_digest": digest_json(receipt) if receipt.get("ok") is True else ""}

    def cleanup(self, candidate):
        if candidate.root.exists():
            _run_git(TRUSTED_REPOSITORY_ROOT, "worktree", "remove", "--force", str(candidate.root))


def execute_code_evolution_effects(runtime: Any, *, transaction_id: str, owner_id: str) -> dict[str, Any]:
    policy_path = os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or str(CODE_AUTOMATION_POLICY_DEFAULT_PATH)
    return CodeEvolutionEffectOwner(
        runtime,
        owner_id=owner_id,
        adapter=ProductionEffectAdapter(),
        policy_loader=lambda: load_code_automation_policy(path=policy_path),
        policy_consumer=consume_code_automation_policy,
    ).execute(transaction_id)


def resolve_code_evolution_quarantine(
    runtime: Any,
    *,
    transaction_id: str,
    expected_current_commit: str,
) -> dict[str, Any]:
    """Resolve only the repository lock after proving a quarantined effect absent."""

    from eimemory.governance.code_evolution_transaction import CodeEvolutionTransactionManager
    from eimemory.governance.deployment_receipt import inspect_immutable_deployment
    from eimemory.storage.code_evolution_store import canonical_json

    store = CodeEvolutionTransactionManager(runtime).store
    transaction = store.get_transaction(transaction_id)
    if transaction is None or transaction.get("current_state") != "RECOVERY_QUARANTINED":
        raise ValueError("quarantine_transaction_required")
    candidate_commit = str(transaction.get("candidate_commit") or "")
    base_commit = str(transaction.get("base_commit") or "")
    current_commit = str(expected_current_commit or "")
    if any(_HEX40.fullmatch(value) is None for value in (candidate_commit, base_commit, current_commit)):
        raise ValueError("quarantine_commit_identity_invalid")
    remote_line = _git(TRUSTED_REPOSITORY_ROOT, "ls-remote", "--heads", TRUSTED_REMOTE, f"refs/heads/{TRUSTED_BRANCH}")
    remote_sha = remote_line.split()[0] if remote_line else ""
    if _HEX40.fullmatch(remote_sha) is None:
        raise ValueError("quarantine_remote_head_invalid")
    if candidate_commit in {remote_sha, current_commit}:
        raise ValueError("quarantined_candidate_effect_present")
    try:
        _run_git(TRUSTED_REPOSITORY_ROOT, "merge-base", "--is-ancestor", remote_sha, current_commit)
        _run_git(TRUSTED_REPOSITORY_ROOT, "merge-base", "--is-ancestor", base_commit, current_commit)
    except subprocess.CalledProcessError as exc:
        raise ValueError("quarantine_current_lineage_invalid") from exc
    deployment = inspect_immutable_deployment(
        runtime,
        scope=_transaction_scope(transaction),
        repo=TRUSTED_REPOSITORY_ROOT,
        current_link="/opt/eimemory/current",
        health_url="http://127.0.0.1:8091/health",
        expected_commit=current_commit,
    )
    if deployment.get("ok") is not True:
        raise ValueError("quarantine_current_deployment_unhealthy")
    evidence = {
        "schema": "code_evolution_quarantine_resolution.v1",
        "transaction_id": transaction_id,
        "repository_root": str(TRUSTED_REPOSITORY_ROOT),
        "repository_ref": TRUSTED_BRANCH,
        "remote_sha": remote_sha,
        "candidate_commit": candidate_commit,
        "base_commit": base_commit,
        "current_commit": current_commit,
        "candidate_absent_from_remote": True,
        "candidate_absent_from_deployment": True,
        "current_deployment": deployment,
    }
    encoded = canonical_json(evidence).encode("utf-8")
    artifact = store.store_artifact(
        transaction_id,
        artifact_kind="quarantine_resolution_evidence",
        artifact_schema="code_evolution_quarantine_resolution.v1",
        data=encoded,
        max_bytes=16 * 1024,
    )
    event = store.append_step_event(
        transaction_id,
        {
            "step": "quarantine_resolution",
            "phase": "reconcile",
            "attempt": 1,
            "from_state": "RECOVERY_QUARANTINED",
            "to_state": "RECOVERY_QUARANTINED",
            "artifact_digest": artifact["sha256"],
            "evidence_digest": artifact["sha256"],
            "summary": "reconcile:quarantine:no_external_effect_verified",
        },
    )
    resolution = store.record_quarantine_resolution(
        transaction_id,
        evidence_digest=artifact["sha256"],
        event_digest=event["event_digest"],
    )
    return {
        "ok": True,
        "status": "quarantine_resolved_no_external_effect",
        "transaction_id": transaction_id,
        "artifact_digest": artifact["sha256"],
        "event_digest": event["event_digest"],
        "resolution_recorded": resolution.get("transaction_id") == transaction_id,
        "evidence": evidence,
    }


def execute_code_evolution_rollback(runtime: Any, *, transaction_id: str, owner_id: str) -> dict[str, Any]:
    policy_path = os.environ.get(CODE_AUTOMATION_POLICY_PATH_ENV) or str(CODE_AUTOMATION_POLICY_DEFAULT_PATH)
    return CodeEvolutionEffectOwner(
        runtime,
        owner_id=owner_id,
        adapter=ProductionEffectAdapter(),
        policy_loader=lambda: load_code_automation_policy(path=policy_path),
        policy_consumer=consume_code_automation_policy,
    ).execute_rollback(transaction_id)


def sample_code_evolution_observation(
    runtime: Any,
    *,
    transaction: Mapping[str, Any],
    observed_at: str = "",
) -> dict[str, Any]:
    """Build one bounded sample from live receipt and L5 reader authorities."""

    from eimemory.governance.evidence_contract import current_release_identity, release_identity_payload
    from eimemory.governance.l5_reader import build_l5_effective_report
    from eimemory.governance.code_evolution_observation import observation_phase, parse_observation_time
    from eimemory.adapters.hermes.code_implementation import resolve_code_implementation_provider
    from eimemory.governance.deployment_receipt import inspect_immutable_deployment

    checked_at = str(observed_at or utc_now())
    scope = _transaction_scope(transaction)
    profile_key = str(transaction.get("profile_key") or "").strip()
    if not profile_key:
        return {"ok": False, "reason": "observation_profile_required"}
    expected_commit = str(transaction.get("deployed_commit") or transaction.get("candidate_commit") or "")
    phase = max(
        0,
        observation_phase(
            parse_observation_time(str(transaction.get("observation_started_at") or checked_at)),
            parse_observation_time(checked_at),
        ),
    )
    sample_key = f"phase-{phase}"
    try:
        release = current_release_identity(runtime, scope)
        report = build_l5_effective_report(
            runtime,
            scope=scope,
            runtime_scope=scope,
            profile_key=profile_key,
            reader_mode="v3",
            persist=False,
            repo_root=str(TRUSTED_REPOSITORY_ROOT),
            limit=500,
        )
        provider = resolve_code_implementation_provider(
            runtime,
            runtime_scope=scope,
            capability_scope="global",
            checked_at=checked_at,
            probe=True,
        )
        deployment = inspect_immutable_deployment(
            runtime,
            scope=scope,
            repo=TRUSTED_REPOSITORY_ROOT,
            current_link="/opt/eimemory/current",
            health_url="http://127.0.0.1:8091/health",
            expected_commit=expected_commit,
            transaction_id=str(transaction.get("transaction_id") or ""),
        )
    except Exception as exc:
        return {"ok": False, "reason": f"observation_authority_unavailable:{type(exc).__name__}"}
    if release is None:
        return {"ok": False, "reason": "observation_release_identity_unavailable"}
    release_payload = release_identity_payload(release)
    semantic_correct, report_measure = _l5_observation_semantics(report, transaction)
    live_advertisement_digest = str(provider.get("advertisement_digest") or "")
    expected_advertisement_digest = str(transaction.get("advertisement_digest") or "")
    provider_error = _live_provider_authority_error(runtime, transaction, provider)
    provider_ok = not provider_error
    receipt_digest = str(deployment.get("deployment_receipt_digest") or "")
    health_ok = release.commit == expected_commit and semantic_correct and provider_ok and deployment.get("ok") is True
    return {
        "sample_key": sample_key,
        "observed_at": checked_at,
        "commit": release.commit,
        "release_identity": digest_json(release_payload),
        "service_health": {"release_commit_matches": release.commit == expected_commit, "l5_semantics_correct": semantic_correct, "provider_live": provider_ok, "deployment_live": deployment.get("ok") is True},
        # Preserve the immutable proposal coordinate; record refreshed liveness
        # separately so the observation writer does not confuse TTL with drift.
        "provider_advertisement_digest": expected_advertisement_digest,
        "live_provider_advertisement_digest": live_advertisement_digest,
        "provider_authority_error": provider_error,
        "deployment_receipt_digest": receipt_digest,
        "incident_measure": report_measure,
        "health_ok": health_ok,
        "incident_regressed": not semantic_correct,
        "hard_failure": release.commit != expected_commit or not provider_ok or deployment.get("ok") is not True,
    }


def _l5_observation_semantics(
    report: Mapping[str, Any],
    transaction: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    """Accept only the incident-specific, otherwise-ready OBSERVING envelope."""

    code = report.get("code_evolution") if isinstance(report.get("code_evolution"), Mapping) else {}
    axes = report.get("axes") if isinstance(report.get("axes"), Mapping) else {}
    evidence = report.get("transaction_evidence") if isinstance(report.get("transaction_evidence"), Mapping) else {}
    assessment = report.get("assessment") if isinstance(report.get("assessment"), Mapping) else {}
    deployment = assessment.get("deployment_assurance") if isinstance(assessment.get("deployment_assurance"), Mapping) else {}
    # Portable capability evidence may explicitly leave the deployment axis
    # neutral. The sampler independently requires the exact transaction's
    # strict receipt and live immutable deployment; neutral is not that proof.
    deployment_axis_acceptable = axes.get("deployment_assurance") == "ready" or (
        axes.get("deployment_assurance") == "neutral"
        and deployment.get("required") is False
        and deployment.get("blocking") is False
        and "ok" in deployment
        and deployment["ok"] is None
    )
    transaction_id = str(transaction.get("transaction_id") or "").strip()
    profile_key = str(transaction.get("profile_key") or "").strip()
    raw_gaps = report.get("gaps")
    gaps = [str(item) for item in raw_gaps] if isinstance(raw_gaps, list) else []
    allowed_gaps = {
        "terminal_receipt_unbound",
        "transaction_evidence_unverified",
        "no_qualifying_terminal_receipt",
        "nonterminal_transaction_exists",
        "observation_not_valid",
    }
    checks = {
        "schema": report.get("schema") == "l5.reader.v4"
        and report.get("schema_version") == "l5_readiness.v4"
        and report.get("report_type") == "l5_readiness_report"
        and report.get("reader_mode") == "v3",
        "profile": bool(profile_key) and str(report.get("profile_key") or "") == profile_key,
        "expected_incomplete": report.get("ok") is False
        and report.get("product_l5_complete") is False
        and report.get("completion_status") == "incomplete"
        and report.get("status") == "incomplete",
        "control_plane": report.get("control_plane_ok") is True
        and report.get("control_plane_status") == "ready"
        and axes.get("capability_ready") is True
        and axes.get("adapter_ready") is True
        and deployment_axis_acceptable,
        "provider_catalog": code.get("provider_ready") is True
        and code.get("catalog_ready") is True
        and code.get("advertisement_fresh") is True,
        "lineage": code.get("current_lineage_compatible") is True,
        "current_transaction_pending_only": bool(transaction_id)
        and str(evidence.get("transaction_id") or "") == transaction_id
        and evidence.get("nonterminal") is True
        and evidence.get("quarantined") is not True
        and code.get("transaction_verified") is False
        and "nonterminal_transaction_exists" in gaps,
        "gaps_incident_specific": bool(gaps)
        and not (set(gaps) - allowed_gaps)
        and list(code.get("gaps") or ()) == gaps,
    }
    correct = all(checks.values())
    return correct, {
        "schema": str(report.get("schema") or ""),
        "schema_version": str(report.get("schema_version") or ""),
        "status": str(report.get("status") or ""),
        "gaps": gaps,
        "checks": checks,
        "incident_specific_semantics": correct,
        "digest": digest_json(report),
    }


def read_code_evolution_external_state(
    runtime: Any,
    *,
    transaction: Mapping[str, Any],
) -> dict[str, Any]:
    """Read typed Git/deployment state for intent reconciliation only."""

    state = str(transaction.get("current_state") or "")
    root = Path(str(transaction.get("repository_root") or ""))
    if root.resolve() != TRUSTED_REPOSITORY_ROOT.resolve():
        return {}
    base = str(transaction.get("base_commit") or "")
    candidate_commit = str(transaction.get("candidate_commit") or "")
    if state == "COMMIT_INTENT":
        transaction_id = str(transaction.get("transaction_id") or "")
        if _SAFE_TRANSACTION.fullmatch(transaction_id) is None:
            return {}
        worktree = TRUSTED_REPOSITORY_ROOT / ".worktrees" / f"code-evolution-{transaction_id}"
        if not worktree.exists():
            return {
                "candidate_commit": "",
                "base_commit": base,
                "parent": "",
                "tree_matches": False,
                "transaction_trailer_matches": False,
                "detached_worktree_exact": False,
            }
        try:
            head = _git(worktree, "rev-parse", "HEAD")
            proposal = _proposal(transaction)
            test_plan = proposal.get("test_plan") if isinstance(proposal.get("test_plan"), Mapping) else {}
            plan = protected_test_plan(str(test_plan.get("id") or ""))
            paths = plan.allowed_files if plan is not None else ()
            tree_matches = bool(paths) and _complete_worktree_digest(worktree) == str(transaction.get("candidate_tree_digest") or "")
            if head == base:
                return {
                    "candidate_commit": "",
                    "base_commit": base,
                    "parent": "",
                    "tree_matches": tree_matches,
                    "transaction_trailer_matches": False,
                    "detached_worktree_exact": tree_matches,
                }
            message = _git(worktree, "show", "-s", "--format=%B", head)
            return {
                "candidate_commit": head,
                "base_commit": base,
                "parent": _git(worktree, "rev-parse", f"{head}^"),
                "tree_matches": tree_matches,
                "transaction_trailer_matches": f"Code-Evolution-Transaction: {transaction_id}" in message,
                "detached_worktree_exact": True,
            }
        except (OSError, subprocess.CalledProcessError, UnicodeError, ValueError):
            return {}
    if state == "PUSH_INTENT":
        try:
            remote_line = _git(TRUSTED_REPOSITORY_ROOT, "ls-remote", "--heads", TRUSTED_REMOTE, f"refs/heads/{TRUSTED_BRANCH}")
        except (OSError, subprocess.CalledProcessError, UnicodeError):
            return {}
        return {
            "remote_sha": remote_line.split()[0] if remote_line else "",
            "candidate_commit": candidate_commit,
            "base_commit": base,
        }
    if state in {"DEPLOY_INTENT", "ROLLBACK_INTENT"}:
        from eimemory.governance.deployment_receipt import inspect_immutable_deployment

        prior = str(transaction.get("prior_commit") or base)
        expected = candidate_commit if state == "DEPLOY_INTENT" else prior
        deployment = inspect_immutable_deployment(
            runtime,
            scope=_transaction_scope(transaction),
            repo=TRUSTED_REPOSITORY_ROOT,
            current_link="/opt/eimemory/current",
            health_url="http://127.0.0.1:8091/health",
            expected_commit=expected,
            transaction_id=str(transaction.get("transaction_id") or "") if state == "DEPLOY_INTENT" else "",
        )
        current_commit = str(deployment.get("current_commit") or "")
        storage_state = _storage_release_state(runtime)
        if state == "DEPLOY_INTENT":
            return {
                "current_commit": current_commit,
                "candidate_commit": candidate_commit,
                "prior_commit": prior,
                "deployment_receipt_valid": deployment.get("receipt_valid") is True,
                "deployment_receipt_digest": str(deployment.get("deployment_receipt_digest") or ""),
                "deployment_version": str(deployment.get("deployment_version") or ""),
                "storage_release_marker": (
                    "committed"
                    if storage_state == "clean" and current_commit == candidate_commit
                    else "clean_prior"
                    if storage_state == "clean" and current_commit == prior
                    else "unknown"
                ),
                "health_ok": deployment.get("health_ok") is True and deployment.get("tree_valid") is True,
            }
        return {
            "current_commit": current_commit,
            "prior_commit": prior,
            "receipt_valid": current_commit == prior and deployment.get("tree_valid") is True,
            "health_ok": deployment.get("health_ok") is True,
            "storage_clean": storage_state == "clean",
        }
    return {}


def _storage_release_state(runtime: Any) -> str:
    """Return clean only when every durable storage-transaction marker is absent."""

    root = getattr(getattr(runtime, "store", runtime), "root", None)
    if root is None:
        return "unknown"
    marker = Path(root) / "state" / "storage-release-transaction.json"
    candidates = (
        marker,
        marker.with_name(f".{marker.name}.clearing"),
        marker.with_name(f".{marker.name}.recovery"),
    )
    try:
        for path in candidates:
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode):
                return "unknown"
            return "in_progress"
    except OSError:
        return "unknown"
    return "clean"


def _strict_receipt_exists(runtime: Any, transaction: Mapping[str, Any]) -> bool:
    from eimemory.governance.deployment_receipt import strict_code_evolution_receipt_error

    record_store = getattr(runtime, "store", runtime)
    records = record_store.list_records_by_meta_value(
        kinds=["promotion_request"],
        scope=_transaction_scope(transaction),
        meta_key="transaction_id",
        meta_value=str(transaction.get("transaction_id") or ""),
        status="deployed",
        limit=10,
    ) or []
    runtime_authority = runtime if hasattr(runtime, "store") else type("RuntimeAuthority", (), {"store": record_store})()
    return any(
        strict_code_evolution_receipt_error(
            runtime_authority,
            scope=ScopeRef.from_dict(_transaction_scope(transaction)),
            record=record,
            deployed_commit=str(transaction.get("candidate_commit") or ""),
        )
        == ""
        for record in records
    )


def _proposal(transaction: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = transaction.get("payload") if isinstance(transaction.get("payload"), Mapping) else {}
    return payload.get("payload") if isinstance(payload.get("payload"), Mapping) else payload


def _blocked(transaction_id: str, reason: str, transaction: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {"ok": False, "applied": False, "blocked_reason": str(reason), "transaction_id": transaction_id, "transaction": dict(transaction or {})}


def _candidate_release_landed(transaction: Mapping[str, Any], deployment_evidence: Mapping[str, Any]) -> bool:
    """True only when the candidate became the live release, not merely pushed."""

    candidate = str(transaction.get("candidate_commit") or "")
    if _HEX40.fullmatch(candidate) is None:
        return False
    if str(transaction.get("deployed_commit") or "") == candidate:
        return True
    payload = transaction.get("payload") if isinstance(transaction.get("payload"), Mapping) else {}
    if payload.get("candidate_pushed_and_deployed") is True:
        return True
    evidence = deployment_evidence if isinstance(deployment_evidence, Mapping) else {}
    if evidence.get("ok") is True:
        return True
    for key in ("commit", "deployed_commit"):
        if str(evidence.get(key) or "") == candidate:
            return True
    return False


def _cleanup_recovered_worktree(transaction: Mapping[str, Any]) -> None:
    transaction_id = str(transaction.get("transaction_id") or "")
    if _SAFE_TRANSACTION.fullmatch(transaction_id) is None:
        return
    worktree = TRUSTED_REPOSITORY_ROOT / ".worktrees" / f"code-evolution-{transaction_id}"
    if worktree.exists() and not worktree.is_symlink():
        _run_git(TRUSTED_REPOSITORY_ROOT, "worktree", "remove", "--force", str(worktree))


def _live_proposal_authority_error(runtime: Any, transaction: Mapping[str, Any]) -> str:
    from eimemory.adapters.hermes.code_implementation import resolve_code_implementation_provider

    proposal = _proposal(transaction)
    response = proposal.get("response") if isinstance(proposal.get("response"), Mapping) else None
    if response is None or digest_json(response) != str(transaction.get("proposal_digest") or ""):
        return "code_evolution_proposal_digest_stale"
    try:
        live = resolve_code_implementation_provider(
            runtime,
            runtime_scope=_transaction_scope(transaction),
            capability_scope="global",
            checked_at=utc_now(),
            probe=True,
        )
    except Exception as exc:
        return f"code_evolution_provider_authority_unavailable:{type(exc).__name__}"
    return _live_provider_authority_error(runtime, transaction, live)


def _live_provider_authority_error(
    runtime: Any, transaction: Mapping[str, Any], live: Mapping[str, Any],
) -> str:
    """Require original authority and current liveness, not identical TTL ads."""

    from eimemory.governance.l5_reader import _historical_advertisement_evidence_error

    expected = {
        "implementation_digest": str(transaction.get("implementation_digest") or ""),
        "catalog_case_id": str(transaction.get("catalog_case_id") or ""),
        "catalog_snapshot_digest": str(transaction.get("catalog_snapshot_digest") or ""),
    }
    if (
        live.get("ok") is not True
        or live.get("advertisement_fresh") is not True
        or _HEX64.fullmatch(str(live.get("advertisement_digest") or "")) is None
        or any(not value or str(live.get(field) or "") != value for field, value in expected.items())
    ):
        return "code_evolution_live_proposal_authority_mismatch"
    return _historical_advertisement_evidence_error(
        getattr(runtime, "capabilities", None), transaction_row=transaction,
        runtime_scope=_transaction_scope(transaction), capability_scope="global",
    )


def _trusted_python() -> Path:
    preferred = TRUSTED_REPOSITORY_ROOT / ".venv" / "bin" / "python"
    candidate = preferred if preferred.is_file() else Path(sys.executable).resolve()
    if not candidate.is_absolute() or not candidate.name.startswith("python") or not os.access(candidate, os.X_OK):
        raise ValueError("trusted_python_unavailable")
    return candidate


def _verification_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp/home",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/tmp/pycache",
    }


def _git_environment() -> dict[str, str]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0"}
    if os.environ.get("HOME"):
        env["HOME"] = str(os.environ["HOME"])
    return env


def _base_effect_environment(runtime: Any) -> dict[str, str]:
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        if os.environ.get(key):
            env[key] = str(os.environ[key])
    runtime_store = getattr(runtime, "store", runtime)
    root = getattr(runtime_store, "root", None)
    if root:
        env["EIMEMORY_ROOT"] = str(root)
    return env


def _deployment_environment(runtime: Any, *, transaction: Mapping[str, Any], verification_receipt_digests: Sequence[str], observation_deadline: str, lineage: Mapping[str, Any]) -> dict[str, str]:
    env = _base_effect_environment(runtime)
    env.update(
        {
            "EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE": "1",
            "EIMEMORY_CODE_EVOLUTION_TRANSACTION_ID": str(transaction.get("transaction_id") or ""),
            "EIMEMORY_CODE_EVOLUTION_AUTHORIZATION_DIGEST": str(transaction.get("authorization_digest") or ""),
            "EIMEMORY_CODE_EVOLUTION_POLICY_DIGEST": str(transaction.get("policy_digest") or ""),
            "EIMEMORY_CODE_EVOLUTION_PATCH_DIGEST": str(transaction.get("patch_digest") or ""),
            "EIMEMORY_CODE_EVOLUTION_CANDIDATE_TREE_DIGEST": str(transaction.get("candidate_tree_digest") or ""),
            "EIMEMORY_CODE_EVOLUTION_VERIFICATION_RECEIPTS": ",".join(verification_receipt_digests),
            "EIMEMORY_CODE_EVOLUTION_OBSERVATION_DEADLINE": observation_deadline,
            "EIMEMORY_CODE_EVOLUTION_PROVIDER_DIGEST": str(transaction.get("implementation_digest") or ""),
            "EIMEMORY_CODE_EVOLUTION_LINEAGE_JSON": json.dumps(lineage, sort_keys=True, separators=(",", ":")),
        }
    )
    return env


def _code_evolution_lineage(transaction: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": True,
        "compatible": True,
        "transaction_id": str(transaction.get("transaction_id") or ""),
        "authorization_digest": str(transaction.get("authorization_digest") or ""),
        "policy_digest": str(transaction.get("policy_digest") or ""),
        "patch_digest": str(transaction.get("patch_digest") or ""),
        "candidate_tree_digest": str(transaction.get("candidate_tree_digest") or ""),
        "provider_implementation_digest": str(transaction.get("implementation_digest") or ""),
        "deployed_commit": str(transaction.get("candidate_commit") or ""),
    }


def _transaction_scope(transaction: Mapping[str, Any]) -> dict[str, str]:
    return {field: str(transaction.get(field) or ("default" if field == "tenant_id" else "")) for field in ("tenant_id", "agent_id", "workspace_id", "user_id")}


def _registered_worktrees() -> frozenset[str]:
    output = _git(TRUSTED_REPOSITORY_ROOT, "worktree", "list", "--porcelain")
    return frozenset(
        str(Path(line.removeprefix("worktree ")).resolve())
        for line in output.splitlines()
        if line.startswith("worktree ")
    )


def _validate_regular_path_chain(root: Path, target: Path) -> None:
    relative = target.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("candidate_parent_path_untrusted")


def _complete_worktree_digest(root: Path) -> str:
    raw_paths = _git_bytes(root, "ls-files", "-z")
    paths = [item.decode("utf-8", errors="strict") for item in raw_paths.split(b"\0") if item]
    entries: list[dict[str, str]] = []
    for relative in sorted(paths):
        target = root / relative
        _validate_regular_path_chain(root, target)
        metadata = target.lstat()
        if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("candidate_repository_tree_untrusted")
        entries.append(
            {
                "path": relative,
                "sha256": sha256(target.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            }
        )
    return sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _run_bounded_process(
    argv: Sequence[str | Path],
    *,
    cwd: Path,
    env: Mapping[str, str],
    heartbeat: Callable[[], Any],
    output_limit: int = 1024 * 1024,
    heartbeat_seconds: float = 30.0,
    max_seconds: float = 7200.0,
) -> tuple[int, bytes]:
    """Run fixed argv with disk-backed output and renew the transaction lease."""

    process = subprocess.Popen(
        [str(item) for item in argv],
        cwd=cwd,
        env=dict(env),
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )
    if process.stdout is None:
        process.kill()
        process.wait()
        raise RuntimeError("protected_process_output_pipe_unavailable")
    import selectors

    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    next_heartbeat = time.monotonic() + heartbeat_seconds
    deadline = time.monotonic() + max_seconds
    try:
        while process.poll() is None or selector.get_map():
            now = time.monotonic()
            if now >= deadline:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return 124, b"protected process timed out"
            if now >= next_heartbeat:
                heartbeat()
                next_heartbeat = now + heartbeat_seconds
            for key, _events in selector.select(timeout=0.25):
                chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = max(0, int(output_limit) - len(output))
                if remaining:
                    output.extend(chunk[:remaining])
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
    return int(process.returncode or 0), bytes(output)


def _git(root: Path, *argv: str) -> str:
    return _run_git(root, *argv).stdout.decode("utf-8", errors="strict").strip()


def _porcelain_changed_paths(output: bytes) -> set[str]:
    """Parse porcelain paths without stripping the first status column."""

    lines = output.decode("utf-8", errors="strict").splitlines()
    return {
        line[3:]
        for line in lines
        if len(line) > 3 and " -> " not in line
    }


def _git_bytes(root: Path, *argv: str) -> bytes:
    return _run_git(root, *argv).stdout


def _run_git(root: Path, *argv: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *argv], cwd=root, env=_git_environment(), shell=False, check=True, capture_output=True, timeout=120)


__all__ = [
    "CandidateMaterialization",
    "CodeEvolutionEffectOwner",
    "DeploymentResult",
    "ProductionEffectAdapter",
    "VerificationResult",
    "execute_code_evolution_effects",
    "execute_code_evolution_rollback",
    "read_code_evolution_external_state",
    "sample_code_evolution_observation",
    "validated_file_updates",
]
