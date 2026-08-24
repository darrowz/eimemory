"""Durable code-evolution transaction state machine and recovery rules.

The module coordinates the existing promotion/effect owner; it does not run
Git, deployment, shell, or model commands.  Every external-effect boundary is
represented by an intent event and resumed through a typed reconciliation
decision after a restart.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Callable

from eimemory.governance.l5_product_completion import QUALIFYING_OUTCOMES
from eimemory.storage.code_evolution_store import (
    CodeEvolutionConflict,
    CodeEvolutionStore,
    CodeEvolutionStoreError,
    digest_json,
    utc_now,
)


CODE_EVOLUTION_TRANSACTION_SCHEMA = "code_evolution_transaction.v1"
LEASE_OWNER_PREFIX = "eimemory-code-evolution"
_V2_PROPOSAL_SCHEMA = "code_implementation_proposal.v2"
_V2_PROVIDER = {
    "capability_id": "code.implementation",
    "revision_id": "code.implementation:v5",
    "binding_id": "binding.hermes.code-implementation:v5",
    "provider_kind": "hermes",
    "provider_instance_id": "hermes.eimemory.code-implementation.production",
    "operation": "propose_patch_v2",
}
_FORBIDDEN_PROPOSAL_KEYS = {
    "argv",
    "command",
    "commands",
    "cwd",
    "env",
    "environment",
    "git",
    "shell",
    "secret",
    "secrets",
    "token",
    "tokens",
}

STATES = (
    "DETECTED",
    "DIAGNOSED",
    "PROVIDER_RESOLVED",
    "PATCH_PROPOSED",
    "PATCH_VALIDATED",
    "CANDIDATE_MATERIALIZED",
    "FOCUSED_VERIFIED",
    "REGRESSION_VERIFIED",
    "FULL_SUITE_VERIFIED",
    "POLICY_AUTHORIZED",
    "COMMIT_INTENT",
    "COMMITTED",
    "PUSH_INTENT",
    "PUSHED",
    "DEPLOY_INTENT",
    "DEPLOYED_VERIFIED",
    "HEALTHY",
    "OBSERVING",
    "SUCCEEDED_SEDIMENTED",
    "ABORTED_NO_EXTERNAL_EFFECT",
    "ABORTED_CANDIDATE_RESTORED",
    "ROLLBACK_INTENT",
    "ROLLED_BACK_HEALTHY",
    "RECOVERY_QUARANTINED",
)
TERMINAL_STATES = frozenset(
    {
        "SUCCEEDED_SEDIMENTED",
        "ABORTED_NO_EXTERNAL_EFFECT",
        "ABORTED_CANDIDATE_RESTORED",
        "ROLLED_BACK_HEALTHY",
        "RECOVERY_QUARANTINED",
    }
)
TERMINAL_OUTCOMES = frozenset(
    {
        "succeeded_sedimented",
        "rolled_back_healthy",
        "aborted_no_external_effect",
        "aborted_candidate_restored",
        "recovery_quarantined",
    }
)

TRANSITIONS: dict[str, frozenset[str]] = {
    "DETECTED": frozenset({"DIAGNOSED", "ABORTED_NO_EXTERNAL_EFFECT", "RECOVERY_QUARANTINED"}),
    "DIAGNOSED": frozenset({"PROVIDER_RESOLVED", "ABORTED_NO_EXTERNAL_EFFECT", "RECOVERY_QUARANTINED"}),
    "PROVIDER_RESOLVED": frozenset({"PATCH_PROPOSED", "ABORTED_NO_EXTERNAL_EFFECT", "RECOVERY_QUARANTINED"}),
    "PATCH_PROPOSED": frozenset({"PATCH_VALIDATED", "ABORTED_NO_EXTERNAL_EFFECT", "RECOVERY_QUARANTINED"}),
    "PATCH_VALIDATED": frozenset({"CANDIDATE_MATERIALIZED", "ABORTED_NO_EXTERNAL_EFFECT", "RECOVERY_QUARANTINED"}),
    "CANDIDATE_MATERIALIZED": frozenset({"FOCUSED_VERIFIED", "ABORTED_CANDIDATE_RESTORED", "ABORTED_NO_EXTERNAL_EFFECT", "RECOVERY_QUARANTINED"}),
    "FOCUSED_VERIFIED": frozenset({"REGRESSION_VERIFIED", "ABORTED_CANDIDATE_RESTORED", "RECOVERY_QUARANTINED"}),
    "REGRESSION_VERIFIED": frozenset({"FULL_SUITE_VERIFIED", "ABORTED_CANDIDATE_RESTORED", "RECOVERY_QUARANTINED"}),
    "FULL_SUITE_VERIFIED": frozenset({"POLICY_AUTHORIZED", "ABORTED_CANDIDATE_RESTORED", "RECOVERY_QUARANTINED"}),
    "POLICY_AUTHORIZED": frozenset({"COMMIT_INTENT", "ABORTED_CANDIDATE_RESTORED", "ABORTED_NO_EXTERNAL_EFFECT", "RECOVERY_QUARANTINED"}),
    "COMMIT_INTENT": frozenset({"COMMITTED", "ABORTED_CANDIDATE_RESTORED", "RECOVERY_QUARANTINED"}),
    "COMMITTED": frozenset({"PUSH_INTENT", "RECOVERY_QUARANTINED"}),
    "PUSH_INTENT": frozenset({"PUSHED", "RECOVERY_QUARANTINED"}),
    "PUSHED": frozenset({"DEPLOY_INTENT", "RECOVERY_QUARANTINED"}),
    "DEPLOY_INTENT": frozenset({"DEPLOYED_VERIFIED", "ROLLBACK_INTENT", "RECOVERY_QUARANTINED"}),
    "DEPLOYED_VERIFIED": frozenset({"HEALTHY", "ROLLBACK_INTENT", "RECOVERY_QUARANTINED"}),
    "HEALTHY": frozenset({"OBSERVING", "ROLLBACK_INTENT", "RECOVERY_QUARANTINED"}),
    "OBSERVING": frozenset({"SUCCEEDED_SEDIMENTED", "ROLLBACK_INTENT", "RECOVERY_QUARANTINED"}),
    "ROLLBACK_INTENT": frozenset({"ROLLED_BACK_HEALTHY", "RECOVERY_QUARANTINED"}),
}


class CodeEvolutionTransactionError(RuntimeError):
    """A state-machine or recovery operation failed closed."""


class InvalidCodeEvolutionTransition(CodeEvolutionTransactionError):
    """The requested edge is not in the protected state machine."""


class CodeEvolutionRecoveryRequired(CodeEvolutionTransactionError):
    """External state is unknown and the repository must be quarantined."""


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    status: str
    reason: str
    retry_allowed: bool = False
    rollback_required: bool = False
    quarantine_required: bool = False
    evidence_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "retry_allowed": self.retry_allowed,
            "rollback_required": self.rollback_required,
            "quarantine_required": self.quarantine_required,
            "evidence_digest": self.evidence_digest,
        }


def _decision(status: str, reason: str, *, retry: bool = False, rollback: bool = False, quarantine: bool = False, evidence: Any = None) -> ReconciliationDecision:
    return ReconciliationDecision(
        status=status,
        reason=reason,
        retry_allowed=retry,
        rollback_required=rollback,
        quarantine_required=quarantine,
        evidence_digest=digest_json(evidence if evidence is not None else {"status": status, "reason": reason}),
    )


def reconcile_commit(external: Mapping[str, Any]) -> ReconciliationDecision:
    """Reconcile a commit intent without accepting an unrelated commit."""

    value = external if isinstance(external, Mapping) else {}
    candidate = str(value.get("candidate_commit") or "")
    parent = str(value.get("parent") or "")
    base = str(value.get("base_commit") or "")
    tree_matches = value.get("tree_matches") is True
    trailer_matches = value.get("transaction_trailer_matches") is True
    worktree_exact = value.get("detached_worktree_exact") is True
    if candidate and parent == base and tree_matches and trailer_matches:
        return _decision("committed", "recorded_candidate_commit_matches", evidence=value)
    if not candidate and worktree_exact:
        return _decision("retry", "exact_detached_candidate_available", retry=True, evidence=value)
    return _decision("quarantine", "commit_external_state_unknown", quarantine=True, evidence=value)


def reconcile_push(external: Mapping[str, Any]) -> ReconciliationDecision:
    """Reconcile remote CAS push using only base/candidate/other SHA."""

    value = external if isinstance(external, Mapping) else {}
    remote = str(value.get("remote_sha") or "")
    candidate = str(value.get("candidate_commit") or "")
    base = str(value.get("base_commit") or "")
    if remote and remote == candidate:
        return _decision("pushed", "remote_already_at_candidate", evidence=value)
    if remote and remote == base:
        return _decision("retry", "remote_still_at_recorded_base", retry=True, evidence=value)
    return _decision("quarantine", "remote_ref_changed_unexpectedly", quarantine=True, evidence=value)


def reconcile_deployment(external: Mapping[str, Any]) -> ReconciliationDecision:
    """Reconcile immutable deployment state; never blindly replay an installer."""

    value = external if isinstance(external, Mapping) else {}
    current_commit = str(value.get("current_commit") or "")
    candidate = str(value.get("candidate_commit") or "")
    prior = str(value.get("prior_commit") or "")
    receipt_valid = value.get("deployment_receipt_valid") is True
    storage_marker = str(value.get("storage_release_marker") or "")
    healthy = value.get("health_ok") is True
    if current_commit == candidate and receipt_valid and storage_marker == "committed" and healthy:
        return _decision("deployed_verified", "candidate_release_receipt_and_health_valid", evidence=value)
    if current_commit == candidate and not receipt_valid:
        return _decision("repair_or_rollback", "candidate_current_without_valid_receipt", rollback=True, evidence=value)
    if current_commit == prior and storage_marker == "clean_prior":
        return _decision("retry", "prior_release_clean_after_interrupted_deploy", retry=True, evidence=value)
    return _decision("quarantine", "deployment_external_state_unknown", quarantine=True, evidence=value)


def reconcile_rollback(external: Mapping[str, Any]) -> ReconciliationDecision:
    value = external if isinstance(external, Mapping) else {}
    if (
        str(value.get("current_commit") or "") == str(value.get("prior_commit") or "")
        and value.get("receipt_valid") is True
        and value.get("health_ok") is True
        and value.get("storage_clean") is True
    ):
        return _decision("rolled_back_healthy", "prior_receipt_backed_release_healthy", evidence=value)
    return _decision("quarantine", "rollback_state_unknown", quarantine=True, evidence=value)


def reconcile_sedimentation(external: Mapping[str, Any]) -> ReconciliationDecision:
    value = external if isinstance(external, Mapping) else {}
    if value.get("matching_outcome_exists") is True:
        return _decision("succeeded", "matching_terminal_outcome_already_exists", evidence=value)
    if value.get("conflicting_outcome_exists") is True:
        return _decision("quarantine", "terminal_outcome_identity_conflict", quarantine=True, evidence=value)
    if value.get("append_once_available") is True:
        return _decision("retry", "terminal_outcome_not_yet_sedimented", retry=True, evidence=value)
    return _decision("quarantine", "sedimentation_state_unknown", quarantine=True, evidence=value)


def qualification_report(
    transaction: Mapping[str, Any] | None,
    *,
    terminal_receipt: Mapping[str, Any] | None = None,
    current_lineage: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return explicit qualification evidence and the first blocking reason."""

    tx = transaction if isinstance(transaction, Mapping) else {}
    receipt = terminal_receipt if isinstance(terminal_receipt, Mapping) else {}
    lineage = current_lineage if isinstance(current_lineage, Mapping) else {}
    outcome = str(receipt.get("outcome") or tx.get("qualifying_terminal_outcome") or tx.get("outcome") or "")
    checks = {
        "system_origin": str(tx.get("origin") or "") == "system_detector",
        "unknown_before_detection": tx.get("known_before_detection") is False,
        "not_prior_user_reported": tx.get("prior_user_reported") is False,
        "not_manual_bootstrap": tx.get("manual_bootstrap") is False,
        "qualifying_outcome": outcome in QUALIFYING_OUTCOMES,
        "not_quarantined": tx.get("quarantined") is not True and outcome != "recovery_quarantined",
        "observation_valid": tx.get("observation_valid") is True,
        "current_lineage": lineage.get("ok") is True and lineage.get("compatible") is True,
    }
    if outcome == "rolled_back_healthy":
        checks["rollback_executed"] = tx.get("rollback_executed") is True or receipt.get("rollback_executed") is True
        checks["candidate_pushed_and_deployed"] = tx.get("candidate_pushed_and_deployed") is True or receipt.get("candidate_pushed_and_deployed") is True
    reasons = [name for name, passed in checks.items() if not passed]
    return {
        "qualifies_for_product_completion": not reasons,
        "qualifying_terminal_outcome": outcome or None,
        "checks": checks,
        "reason": reasons[0] if reasons else "qualifying_production_transaction",
        "reasons": reasons,
    }


def qualifies_for_product_completion(transaction: Mapping[str, Any] | None, *, terminal_receipt: Mapping[str, Any] | None = None, current_lineage: Mapping[str, Any] | None = None) -> bool:
    return bool(qualification_report(transaction, terminal_receipt=terminal_receipt, current_lineage=current_lineage)["qualifies_for_product_completion"])


def _proposal_contains_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).strip().lower() in _FORBIDDEN_PROPOSAL_KEYS:
                return True
            if _proposal_contains_forbidden_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_proposal_contains_forbidden_key(child) for child in value)
    return False


class CodeEvolutionTransactionManager:
    """CAS/lease coordinator used by the existing promotion effect owner."""

    def __init__(self, runtime: Any, *, owner_id: str = "", now: Callable[[], str] | None = None) -> None:
        runtime_store = getattr(runtime, "store", runtime)
        self.store = CodeEvolutionStore(runtime_store)
        self.owner_id = owner_id or f"{LEASE_OWNER_PREFIX}:process"
        self._now = now or utc_now

    def create_detected(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized.setdefault("schema_version", CODE_EVOLUTION_TRANSACTION_SCHEMA)
        normalized.setdefault("current_state", "DETECTED")
        normalized.setdefault("manual_bootstrap", False)
        return self.store.create_transaction(normalized)

    def submit_proposal(
        self,
        proposal: Mapping[str, Any],
        *,
        scope: Mapping[str, Any],
        effects_enabled: bool = False,
        apply: bool = True,
    ) -> dict[str, Any]:
        """Materialize a strict v2 proposal without owning external effects.

        Promotion management remains the sole effect owner.  This method
        records the normalized proposal and validation milestones; it
        deliberately stops at a durable no-effect abort while the bootstrap
        policy keeps forward effects disabled. Candidate materialization is
        never claimed until a trusted detached-worktree executor exists.
        """

        if not isinstance(proposal, Mapping):
            raise CodeEvolutionTransactionError("code evolution proposal must be an object")
        if str(proposal.get("schema_version") or "") != _V2_PROPOSAL_SCHEMA:
            raise CodeEvolutionTransactionError("code evolution proposal schema mismatch")
        if proposal.get("proposal_only") is not True:
            raise CodeEvolutionTransactionError("code evolution proposal must be proposal-only")
        if _proposal_contains_forbidden_key(proposal):
            raise CodeEvolutionTransactionError("code evolution proposal contains execution authority")
        transaction_id = str(proposal.get("transaction_id") or "").strip()
        if not transaction_id:
            raise CodeEvolutionTransactionError("code evolution transaction_id is required")
        provenance_fields = ("origin", "known_before_detection", "prior_user_reported", "manual_bootstrap")
        if any(field not in proposal for field in provenance_fields):
            raise CodeEvolutionTransactionError("code evolution provenance is incomplete")
        if any(not isinstance(proposal.get(field), bool) for field in provenance_fields[1:]):
            raise CodeEvolutionTransactionError("code evolution provenance booleans invalid")
        origin = str(proposal.get("origin") or "").strip()
        if origin not in {"system_detector", "manual_bootstrap", "user_reported"}:
            raise CodeEvolutionTransactionError("code evolution origin invalid")
        incident = proposal.get("incident") if isinstance(proposal.get("incident"), Mapping) else {}
        repository = proposal.get("repository") if isinstance(proposal.get("repository"), Mapping) else {}
        provider = proposal.get("provider") if isinstance(proposal.get("provider"), Mapping) else {}
        for field, value in (
            ("incident_id", incident.get("incident_id")),
            ("incident_class", incident.get("incident_class")),
            ("repository_root", repository.get("repository_root") or repository.get("root")),
            ("repository_ref", repository.get("repository_ref") or repository.get("ref")),
            ("capability_id", provider.get("capability_id")),
            ("revision_id", provider.get("revision_id")),
            ("binding_id", provider.get("binding_id")),
            ("implementation_digest", provider.get("implementation_digest")),
        ):
            if not str(value or "").strip():
                raise CodeEvolutionTransactionError(f"code evolution proposal missing {field}")
        if set(provider) != set(_V2_PROVIDER) | {"implementation_digest"}:
            raise CodeEvolutionTransactionError("code evolution provider coordinates invalid")
        if any(str(provider.get(key) or "") != expected for key, expected in _V2_PROVIDER.items()):
            raise CodeEvolutionTransactionError("code evolution provider coordinates mismatch")
        implementation_digest = str(provider.get("implementation_digest") or "").strip().lower()
        if len(implementation_digest) != 64 or any(char not in "0123456789abcdef" for char in implementation_digest):
            raise CodeEvolutionTransactionError("code evolution implementation digest invalid")
        if not isinstance(proposal.get("file_updates"), list):
            raise CodeEvolutionTransactionError("code evolution proposal file_updates missing")
        advertisement = proposal.get("advertisement") if isinstance(proposal.get("advertisement"), Mapping) else {}
        catalog = proposal.get("catalog") if isinstance(proposal.get("catalog"), Mapping) else {}
        production_eligible = proposal.get("qualifying") is True and proposal.get("test_only_provider") is not True
        if production_eligible:
            if set(advertisement) != {"advertisement_id", "advertisement_digest"}:
                raise CodeEvolutionTransactionError("code evolution advertisement coordinates invalid")
            if set(catalog) != {"catalog_case_id", "catalog_snapshot_digest"}:
                raise CodeEvolutionTransactionError("code evolution catalog coordinates invalid")
            for field, value in (
                ("advertisement_id", advertisement.get("advertisement_id")),
                ("catalog_case_id", catalog.get("catalog_case_id")),
            ):
                if not str(value or "").strip():
                    raise CodeEvolutionTransactionError(f"code evolution {field} missing")
            for field, value in (
                ("advertisement_digest", advertisement.get("advertisement_digest")),
                ("catalog_snapshot_digest", catalog.get("catalog_snapshot_digest")),
            ):
                digest = str(value or "").strip().lower()
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    raise CodeEvolutionTransactionError(f"code evolution {field} invalid")
        if set(repository) != {"repository_root", "repository_ref", "base_commit", "base_tree_digest"}:
            raise CodeEvolutionTransactionError("code evolution repository coordinates invalid")
        base_commit = str(repository.get("base_commit") or "").strip().lower()
        if len(base_commit) != 40 or any(char not in "0123456789abcdef" for char in base_commit):
            raise CodeEvolutionTransactionError("code evolution base commit invalid")
        base_tree_digest = str(repository.get("base_tree_digest") or "").strip().lower()
        if len(base_tree_digest) != 64 or any(char not in "0123456789abcdef" for char in base_tree_digest):
            raise CodeEvolutionTransactionError("code evolution base tree digest invalid")
        payload = {
            "schema_version": CODE_EVOLUTION_TRANSACTION_SCHEMA,
            "transaction_id": transaction_id,
            "idempotency_key": str(proposal.get("idempotency_key") or f"code-evolution:{transaction_id}"),
            "scope": dict(scope),
            "incident": dict(incident),
            "origin": origin,
            "detector": str(proposal.get("detector") or ""),
            "known_before_detection": proposal.get("known_before_detection") is True,
            "prior_user_reported": proposal.get("prior_user_reported") is True,
            "manual_bootstrap": proposal.get("manual_bootstrap") is True,
            "repository": dict(repository),
            "provider": {**dict(provider), "implementation_digest": implementation_digest},
            "advertisement_id": str(advertisement.get("advertisement_id") or ""),
            "advertisement_digest": str(advertisement.get("advertisement_digest") or ""),
            "catalog_case_id": str(catalog.get("catalog_case_id") or ""),
            "catalog_snapshot_digest": str(catalog.get("catalog_snapshot_digest") or ""),
            "proposal_digest": str(proposal.get("proposal_digest") or ""),
            "patch_digest": str(proposal.get("patch_digest") or proposal.get("proposal_digest") or ""),
            # Candidate identity and machine authorization are produced only
            # by their trusted later stages. Provider/proposal data cannot
            # pre-populate either authority.
            "candidate_tree_digest": "",
            "policy_digest": "",
            "authorization_digest": "",
            "payload": dict(proposal),
        }
        current = self.create_detected(payload)
        milestones = (
            ("DIAGNOSED", "diagnosis"),
            ("PROVIDER_RESOLVED", "provider_resolution"),
            ("PATCH_PROPOSED", "proposal"),
            ("PATCH_VALIDATED", "patch_validation"),
        )
        for state, step in milestones:
            if str(current.get("current_state") or "") == state:
                continue
            if str(current.get("current_state") or "") in TERMINAL_STATES:
                break
            updates = {}
            if state == "PATCH_PROPOSED":
                updates["proposal_digest"] = payload["proposal_digest"]
                updates["patch_digest"] = payload["patch_digest"]
            current = self.record_result(
                transaction_id,
                step=step,
                result_state=state,
                output_data={"proposal_digest": payload["proposal_digest"]},
                updates=updates,
            )
        if not apply or not effects_enabled or not production_eligible:
            current = self.effect_disabled(transaction_id, step="effect_owner")
            return {
                "ok": False,
                "applied": False,
                "blocked_reason": (
                    "code_evolution_effects_disabled"
                    if not apply or not effects_enabled
                    else "code_evolution_proposal_nonqualifying"
                ),
                "transaction_id": transaction_id,
                "transaction": current,
            }
        # The bootstrap has no external executor.  Treat an enabled policy as
        # an explicit configuration error rather than falling back to the old
        # direct repository writer.
        current = self.effect_disabled(transaction_id, step="effect_executor_unavailable")
        return {
            "ok": False,
            "applied": False,
            "blocked_reason": "code_evolution_effect_executor_unavailable",
            "transaction_id": transaction_id,
            "transaction": current,
        }

    def acquire_lease(self, transaction_id: str) -> dict[str, Any]:
        return self.store.acquire_lease(transaction_id, owner=self.owner_id, now=self._now())

    def renew_lease(self, transaction_id: str) -> dict[str, Any]:
        return self.store.renew_lease(transaction_id, owner=self.owner_id, now=self._now())

    def release_lease(self, transaction_id: str) -> dict[str, Any]:
        return self.store.release_lease(transaction_id, owner=self.owner_id, now=self._now())

    def update_metadata(
        self,
        transaction_id: str,
        *,
        payload_updates: Mapping[str, Any] | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """CAS-update non-state transaction metadata without a new effect edge."""

        current = self.store.get_transaction(transaction_id)
        if current is None:
            raise CodeEvolutionTransactionError("transaction not found")
        payload = dict(current.get("payload") or {})
        payload.update(dict(payload_updates or {}))
        values = dict(updates or {})
        values["payload_json"] = payload
        return self.store.cas_transition(
            transaction_id,
            expected_state=str(current["current_state"]),
            expected_state_version=int(current["state_version"]),
            target_state=str(current["current_state"]),
            updates=values,
            terminal=bool(current.get("terminal")),
            now=self._now(),
        )

    def transition(
        self,
        transaction_id: str,
        target_state: str,
        *,
        updates: Mapping[str, Any] | None = None,
        expected_state: str | None = None,
        expected_state_version: int | None = None,
        terminal: bool | None = None,
    ) -> dict[str, Any]:
        target_state = str(target_state)
        if target_state not in STATES:
            raise InvalidCodeEvolutionTransition(f"unknown state {target_state}")
        if target_state in TERMINAL_STATES:
            raise InvalidCodeEvolutionTransition(
                f"{target_state} requires an append-only terminal receipt"
            )
        current = self.store.get_transaction(transaction_id)
        if current is None:
            raise CodeEvolutionTransactionError("transaction not found")
        source = expected_state or str(current["current_state"])
        version = int(current["state_version"] if expected_state_version is None else expected_state_version)
        if target_state not in TRANSITIONS.get(source, frozenset()):
            raise InvalidCodeEvolutionTransition(f"{source}->{target_state} is not allowed")
        return self.store.cas_transition(
            transaction_id,
            expected_state=source,
            expected_state_version=version,
            target_state=target_state,
            updates=updates,
            terminal=terminal if terminal is not None else target_state in TERMINAL_STATES,
            now=self._now(),
        )

    def begin_intent(self, transaction_id: str, *, step: str, intent_state: str, input_data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current = self.store.get_transaction(transaction_id)
        if current is None:
            raise CodeEvolutionTransactionError("transaction not found")
        source = str(current["current_state"])
        if source == intent_state:
            return current
        attempt = self._next_attempt(transaction_id, step=step, phase="intent")
        self.store.append_step_event(
            transaction_id,
            {
                "step": step,
                "phase": "intent",
                "attempt": attempt,
                "from_state": source,
                "to_state": intent_state,
                "input_digest": digest_json(dict(input_data or {})),
                "summary": f"intent:{step}",
                "created_at": self._now(),
            },
        )
        return self.transition(transaction_id, intent_state, expected_state=source, expected_state_version=int(current["state_version"]))

    def record_result(self, transaction_id: str, *, step: str, result_state: str, output_data: Mapping[str, Any] | None = None, updates: Mapping[str, Any] | None = None) -> dict[str, Any]:
        current = self.store.get_transaction(transaction_id)
        if current is None:
            raise CodeEvolutionTransactionError("transaction not found")
        source = str(current["current_state"])
        if source == result_state:
            return current
        attempt = self._next_attempt(transaction_id, step=step, phase="result")
        self.store.append_step_event(
            transaction_id,
            {
                "step": step,
                "phase": "result",
                "attempt": attempt,
                "from_state": source,
                "to_state": result_state,
                "output_digest": digest_json(dict(output_data or {})),
                "summary": f"result:{step}",
                "created_at": self._now(),
            },
        )
        return self.transition(transaction_id, result_state, updates=updates, expected_state=source, expected_state_version=int(current["state_version"]))

    def reconcile(self, transaction_id: str, *, step: str, decision: ReconciliationDecision, success_state: str | None = None) -> dict[str, Any]:
        current = self.store.get_transaction(transaction_id)
        if current is None:
            raise CodeEvolutionTransactionError("transaction not found")
        source = str(current["current_state"])
        target = "RECOVERY_QUARANTINED" if decision.quarantine_required else (success_state or source)
        attempt = self._next_attempt(transaction_id, step=step, phase="reconcile")
        self.store.append_step_event(
            transaction_id,
            {
                "step": step,
                "phase": "reconcile",
                "attempt": attempt,
                "from_state": source,
                "to_state": target,
                "evidence_digest": decision.evidence_digest,
                "summary": f"reconcile:{step}:{decision.status}",
                "created_at": self._now(),
            },
        )
        if target == source:
            return self.store.get_transaction(transaction_id) or {}
        if target in TERMINAL_STATES:
            outcome = {
                "SUCCEEDED_SEDIMENTED": "succeeded_sedimented",
                "ROLLED_BACK_HEALTHY": "rolled_back_healthy",
                "RECOVERY_QUARANTINED": "recovery_quarantined",
            }[target]
            payload_value = current.get("payload")
            payload: dict[str, Any] = dict(payload_value) if isinstance(payload_value, Mapping) else {}
            self.terminalize(
                transaction_id,
                {
                    "outcome": outcome,
                    "incident_digest": str(current.get("incident_digest") or ""),
                    "provider_digest": str(current.get("implementation_digest") or ""),
                    "policy_digest": str(current.get("policy_digest") or ""),
                    "authorization_digest": str(current.get("authorization_digest") or ""),
                    "base_commit": str(current.get("base_commit") or ""),
                    "candidate_commit": str(current.get("candidate_commit") or ""),
                    "deployed_commit": str(current.get("deployed_commit") or ""),
                    "deployment_receipt_digest": str(payload.get("deployment_receipt_digest") or ""),
                    "observation_digest": str(payload.get("observation_digest") or ""),
                    "sedimentation_record_id": str(payload.get("sedimentation_record_id") or ""),
                    "sedimentation_digest": str(payload.get("sedimentation_digest") or ""),
                    "rollback_digest": decision.evidence_digest if target == "ROLLED_BACK_HEALTHY" else "",
                    "evidence_digest": decision.evidence_digest,
                    "observation_valid": payload.get("observation_valid") is True,
                    "candidate_pushed_and_deployed": payload.get("candidate_pushed_and_deployed") is True,
                    "rollback_executed": target == "ROLLED_BACK_HEALTHY",
                    "created_at": self._now(),
                },
                terminal_state=target,
            )
            return self.store.get_transaction(transaction_id) or {}
        return self.transition(transaction_id, target, expected_state=source, expected_state_version=int(current["state_version"]))

    def terminalize(self, transaction_id: str, receipt: Mapping[str, Any], *, terminal_state: str) -> dict[str, Any]:
        if terminal_state not in TERMINAL_STATES:
            raise CodeEvolutionTransactionError("terminal state is not allowed")
        outcome = str(receipt.get("outcome") or "")
        if outcome not in TERMINAL_OUTCOMES:
            raise CodeEvolutionTransactionError("terminal outcome is not allowed")
        current = self.store.get_transaction(transaction_id)
        if current is None:
            raise CodeEvolutionTransactionError("transaction not found")
        if terminal_state not in TRANSITIONS.get(str(current["current_state"]), frozenset()):
            raise InvalidCodeEvolutionTransition(
                f"{current['current_state']}->{terminal_state} is not allowed"
            )
        expected_outcome = {
            "SUCCEEDED_SEDIMENTED": "succeeded_sedimented",
            "ROLLED_BACK_HEALTHY": "rolled_back_healthy",
            "ABORTED_NO_EXTERNAL_EFFECT": "aborted_no_external_effect",
            "ABORTED_CANDIDATE_RESTORED": "aborted_candidate_restored",
            "RECOVERY_QUARANTINED": "recovery_quarantined",
        }[terminal_state]
        if outcome != expected_outcome:
            raise CodeEvolutionTransactionError("terminal state and outcome do not match")
        return self.store.add_terminal_receipt(
            transaction_id,
            receipt,
            terminal_state=terminal_state,
            expected_state=str(current["current_state"]),
            expected_state_version=int(current["state_version"]),
        )

    def effect_disabled(self, transaction_id: str, *, step: str) -> dict[str, Any]:
        """Record a fail-closed diagnostic; no effect is attempted."""

        current = self.store.get_transaction(transaction_id)
        if current is None:
            raise CodeEvolutionTransactionError("transaction not found")
        source = str(current["current_state"])
        if source == "ABORTED_NO_EXTERNAL_EFFECT":
            return current
        if "ABORTED_NO_EXTERNAL_EFFECT" not in TRANSITIONS.get(source, frozenset()):
            raise InvalidCodeEvolutionTransition(f"{source}->ABORTED_NO_EXTERNAL_EFFECT is not allowed")
        attempt = self._next_attempt(transaction_id, step=step, phase="result")
        self.store.append_step_event(
            transaction_id,
            {
                "step": step,
                "phase": "result",
                "attempt": attempt,
                "from_state": source,
                "to_state": "ABORTED_NO_EXTERNAL_EFFECT",
                "summary": f"effect_disabled:{step}",
                "created_at": self._now(),
            },
        )
        self.store.add_terminal_receipt(
            transaction_id,
            {
                "outcome": "aborted_no_external_effect",
                "incident_digest": str(current.get("incident_digest") or ""),
                "provider_digest": str(current.get("implementation_digest") or ""),
                "policy_digest": str(current.get("policy_digest") or ""),
                "authorization_digest": str(current.get("authorization_digest") or ""),
                "base_commit": str(current.get("base_commit") or ""),
                "candidate_commit": str(current.get("candidate_commit") or ""),
                "deployed_commit": str(current.get("deployed_commit") or ""),
                "evidence_digest": digest_json(
                    {"transaction_id": transaction_id, "step": step, "effect_attempted": False}
                ),
                "effect_attempted": False,
                "created_at": self._now(),
            },
            terminal_state="ABORTED_NO_EXTERNAL_EFFECT",
            expected_state=source,
            expected_state_version=int(current["state_version"]),
        )
        return self.store.get_transaction(transaction_id) or {}

    def _next_attempt(self, transaction_id: str, *, step: str, phase: str) -> int:
        events = self.store.list_step_events(transaction_id, limit=2_000)
        return 1 + max(
            (
                int(event.get("attempt") or 0)
                for event in events
                if str(event.get("step") or "") == step
                and str(event.get("phase") or "") == phase
            ),
            default=0,
        )


def recover_transaction(manager: CodeEvolutionTransactionManager, transaction_id: str, *, external_state: Mapping[str, Any]) -> dict[str, Any]:
    """Apply only the typed reconciliation rule for the pending intent."""

    manager.acquire_lease(transaction_id)
    try:
        return _recover_transaction_with_lease(
            manager,
            transaction_id,
            external_state=external_state,
        )
    finally:
        try:
            manager.release_lease(transaction_id)
        except CodeEvolutionConflict:
            # Terminalization makes the transaction immutable and therefore
            # also makes an explicit lease-clear update unnecessary.
            pass


def _recover_transaction_with_lease(
    manager: CodeEvolutionTransactionManager,
    transaction_id: str,
    *,
    external_state: Mapping[str, Any],
) -> dict[str, Any]:
    """Reconcile after the caller has won the five-minute CAS lease."""

    transaction = manager.store.get_transaction(transaction_id)
    if transaction is None:
        raise CodeEvolutionTransactionError("transaction not found")
    state = str(transaction["current_state"])
    if state == "COMMIT_INTENT":
        decision = reconcile_commit(external_state)
        return manager.reconcile(transaction_id, step="commit", decision=decision, success_state="COMMITTED" if decision.status == "committed" else None)
    if state == "PUSH_INTENT":
        decision = reconcile_push(external_state)
        return manager.reconcile(transaction_id, step="push", decision=decision, success_state="PUSHED" if decision.status == "pushed" else None)
    if state == "DEPLOY_INTENT":
        decision = reconcile_deployment(external_state)
        return manager.reconcile(transaction_id, step="deploy", decision=decision, success_state="DEPLOYED_VERIFIED" if decision.status == "deployed_verified" else None)
    if state == "ROLLBACK_INTENT":
        decision = reconcile_rollback(external_state)
        return manager.reconcile(transaction_id, step="rollback", decision=decision, success_state="ROLLED_BACK_HEALTHY" if decision.status == "rolled_back_healthy" else None)
    if state == "OBSERVING":
        decision = reconcile_sedimentation(external_state)
        return manager.reconcile(transaction_id, step="sedimentation", decision=decision, success_state="SUCCEEDED_SEDIMENTED" if decision.status == "succeeded" else None)
    raise CodeEvolutionTransactionError(f"no external intent requires reconciliation from {state}")


__all__ = [
    "CODE_EVOLUTION_TRANSACTION_SCHEMA",
    "CodeEvolutionRecoveryRequired",
    "CodeEvolutionTransactionError",
    "CodeEvolutionTransactionManager",
    "InvalidCodeEvolutionTransition",
    "ReconciliationDecision",
    "STATES",
    "TERMINAL_OUTCOMES",
    "TERMINAL_STATES",
    "qualification_report",
    "qualifies_for_product_completion",
    "reconcile_commit",
    "reconcile_deployment",
    "reconcile_push",
    "reconcile_rollback",
    "reconcile_sedimentation",
    "recover_transaction",
]
