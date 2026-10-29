from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import os
from typing import Any

from eimemory.governance.l5.l5_readiness import readiness_gate_status
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from eimemory.governance.l5.closure_rehearsal import (
    verify_bootstrap_pending_readiness_contract,
)
from eimemory.governance.l5.live_task_acceptance import LIVE_ACCEPTANCE_CASE_IDS
from eimemory.governance.release.release_closure_lineage import (
    finalize_release_lineage as _finalize_release_lineage,
)
from eimemory.models.records import ScopeRef


_BOOTSTRAP_PENDING_RECALL_REASONS = frozenset(
    {
        "eligible_dataset_missing",
        "production_dataset_not_ready",
        "production_recall_dataset_empty",
        "production_recall_dataset_unconfigured",
        "query_features_low_signal",
    }
)
_BOOTSTRAP_DIAGNOSTIC_LATENCY_MULTIPLIER = 1.20
_BOOTSTRAP_DIAGNOSTIC_MAX_LATENCY_SAMPLES = 10


def run_release_closure(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    repo_root: str,
    current_link: str,
    health_url: str,
    prior_commit: str,
    pending_path: str | Path | None = None,
    _checkpoint_release: ReleaseIdentity | None = None,
) -> dict[str, Any]:
    from eimemory.governance.safety.prompt_safety_executor import bind_prompt_safety_from_service

    bind_prompt_safety_from_service(runtime)
    from eimemory.evaluation.production_query_dataset import resolve_bound_logical_scope
    requested = asdict(scope) if isinstance(scope, ScopeRef) else dict(scope or {})
    scope_payload, _scope_resolution = resolve_bound_logical_scope(requested)
    not_run = {"ok": False, "status": "not_run", "reason": "upstream_gate_not_run"}
    report: dict[str, Any] = {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "report_type": "l5_release_closure",
        "legacy_compatibility": True,
        "scope": scope_payload,
        "scope_resolution": _scope_resolution,
        "blocked_stage": "",
        "blocked_reason": "",
        "deployment": {},
        "record_ids": {},
        "deployment_receipt": dict(not_run),
        "production_recall_gate": dict(not_run),
        "production_recall_strict_state": dict(not_run),
        "storage_migrations": dict(not_run),
        "replay_bootstrap": dict(not_run),
        "live_acceptance": dict(not_run),
        "channel_acceptance": dict(not_run),
        "release_lineage": dict(not_run),
        "closure_rehearsal": dict(not_run),
        "readiness": dict(not_run),
        "bootstrap_pending_verification": dict(not_run),
    }
    identity_kwargs = {
        "scope": scope_payload,
        "repo_root": str(repo_root),
        "current_link": str(current_link),
        "health_url": str(health_url),
        "prior_commit": str(prior_commit),
    }

    strict_transaction = os.environ.get("EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE") == "1"
    if strict_transaction:
        # A strict deployment intentionally leaves the control checkout at the
        # prior commit until the transaction owner has admitted the candidate.
        # Bind the live recheck to the installer's verified candidate identity
        # instead of implicitly treating the checkout HEAD as production.
        deployed_commit = str(os.environ.get("EIMEMORY_RUNTIME_COMMIT") or "").strip().lower()
        if not deployed_commit:
            return _blocked(report, "deployment_receipt", "strict_deployed_commit_required")
        identity_kwargs["deployed_commit"] = deployed_commit

    receipt = runtime.verify_and_record_deployment(**identity_kwargs)
    report["deployment_receipt"] = receipt
    if receipt.get("ok") is not True:
        return _blocked(report, "deployment_receipt", _failure_reason(receipt, "deployment_receipt_failed"))
    if _checkpoint_release is not None:
        from eimemory.governance.release.evidence_contract import same_release_authority

        observed = ReleaseIdentity(
            commit=str(receipt.get("commit") or ""),
            version=str(receipt.get("version") or ""),
            receipt_id=str(receipt.get("promotion_request_id") or ""),
            session_id=str(receipt.get("release_session_id") or receipt.get("promotion_request_id") or ""),
        )
        if not same_release_authority(observed, _checkpoint_release):
            return _blocked(report, "deployment_receipt", "pending_release_authority_mismatch")
    if strict_transaction:
        from eimemory.governance.release.release_pre_observation import run_pre_observation_closure

        return run_pre_observation_closure(
            runtime, receipt=receipt, identity_kwargs=identity_kwargs,
            transaction_id=os.environ.get("EIMEMORY_CODE_EVOLUTION_TRANSACTION_ID", ""),
        )
    report["deployment"] = _deployment_identity(receipt)
    report["record_ids"]["deployment_receipt"] = str(receipt.get("promotion_request_id") or "")
    from eimemory.governance.l5.l5_readiness import _storage_migration_status

    migration_status = _storage_migration_status(runtime)
    report["storage_migrations"] = migration_status
    if migration_status.get("ok") is not True:
        return _blocked(report, "storage_migrations", "storage_migrations_pending")
    receipt_identity = ReleaseIdentity(
        commit=str(receipt.get("commit") or ""),
        version=str(receipt.get("version") or ""),
        receipt_id=str(receipt.get("promotion_request_id") or ""),
        session_id=str(receipt.get("release_session_id") or receipt.get("promotion_request_id") or ""),
    )
    from eimemory.governance.release.release_closure_pending import (
        supersede_release_closure_pending,
    )

    # The reconciler already owns the checkpoint lock. Re-run every evidence
    # gate without recursively acquiring that lock or trusting cached reports.
    pending_checkpoint = (
        {"ok": True, "status": "revalidating"}
        if _checkpoint_release is not None
        else supersede_release_closure_pending(
            current_commit=receipt_identity.commit, pending_path=pending_path,
        )
    )
    report["pending_checkpoint"] = pending_checkpoint
    if pending_checkpoint.get("ok") is not True:
        return _blocked(
            report,
            "pending_checkpoint",
            str(
                pending_checkpoint.get("error")
                or "release_closure_pending_supersede_failed"
            ),
        )

    from eimemory.evaluation.controlled_production_acceptance import load_controlled_policy

    try:
        controlled_policy = load_controlled_policy(runtime, scope_payload)
    except (OSError, ValueError) as exc:
        return _blocked(report, "controlled_policy", "controlled_policy_invalid:" + type(exc).__name__)
    if controlled_policy is not None:
        return _run_controlled_operational_closure(
            runtime, report=report, scope=scope_payload, identity_kwargs=identity_kwargs,
            release=receipt_identity, policy=controlled_policy,
        )

    run_recall = getattr(runtime, "run_configured_production_recall_gate", None)
    if not callable(run_recall):
        return _blocked(report, "production_recall_gate", "production_recall_gate_runner_unavailable")
    executed_recall_gate = run_recall(scope=scope_payload)
    report["production_recall_gate"] = executed_recall_gate
    bootstrap_pending: dict[str, Any] | None = None
    if executed_recall_gate.get("accepted") is not True:
        if not _recall_result_allows_bootstrap_pending(executed_recall_gate):
            return _blocked(
                report,
                "production_recall_gate",
                _failure_reason(executed_recall_gate, "production_recall_gate_failed"),
            )
        from eimemory.evaluation.real_query_gate import verify_current_bootstrap_data_pending

        bootstrap_pending = verify_current_bootstrap_data_pending(
            runtime,
            scope=scope_payload,
            release=receipt_identity,
        )
        if bootstrap_pending.get("ok") is not True:
            return _blocked(
                report,
                "production_recall_gate",
                _failure_reason(executed_recall_gate, "production_recall_gate_failed"),
            )
        report["production_recall_gate"] = {
            **executed_recall_gate,
            "bootstrap": bootstrap_pending,
            "status": "data_accumulating",
        }
        report["record_ids"]["production_recall_bootstrap"] = str(bootstrap_pending.get("record_id") or "")

    # Every other agent with real traffic must pass the same gate separately
    # (accepted, or the same data-accumulating bootstrap allowance). No pooling.
    from eimemory.evaluation.recall_agent_scopes import recall_agent_scopes

    per_agent: dict[str, Any] = {}
    for agent_scope in recall_agent_scopes(runtime, scope_payload):
        exact = {key: agent_scope[key] for key in ("tenant_id", "agent_id", "workspace_id", "user_id")}
        if all(str(scope_payload.get(k) or "") == exact[k] for k in ("agent_id", "workspace_id", "user_id")):
            continue
        agent_gate = run_recall(scope=exact)
        allowed = agent_gate.get("accepted") is True or _recall_result_allows_bootstrap_pending(agent_gate)
        per_agent[exact["agent_id"]] = {
            "accepted": agent_gate.get("accepted") is True,
            "data_accumulating": agent_gate.get("accepted") is not True and allowed,
            "reason": "" if allowed else _failure_reason(agent_gate, "production_recall_gate_failed"),
        }
    report["production_recall_gate_per_agent"] = per_agent
    blocked_agents = sorted(agent for agent, item in per_agent.items() if item["reason"])
    if blocked_agents:
        agent = blocked_agents[0]
        return _blocked(report, "production_recall_gate", f"agent:{agent}:{per_agent[agent]['reason']}")
    accumulating = sorted(agent for agent, item in per_agent.items() if item["data_accumulating"])
    if bootstrap_pending is None and accumulating:
        # The primary scope is accepted, but an agent still lacks evidence:
        # the release cannot be certified on the strength of other agents.
        return _blocked(report, "production_recall_gate", f"agent:{accumulating[0]}:data_accumulating")

    if bootstrap_pending is None:
        verify_recall = getattr(runtime, "verify_production_recall_gate", None)
        if not callable(verify_recall):
            return _blocked(report, "production_recall_gate", "production_recall_gate_verifier_unavailable")
        recall_gate = verify_recall(
            scope=scope_payload,
            release_identity=receipt_identity,
            limit=500,
        )
        report["production_recall_gate"] = recall_gate
        report["record_ids"]["production_recall_gate"] = str(recall_gate.get("record_id") or "")
        if recall_gate.get("ok") is not True:
            return _blocked(
                report,
                "production_recall_gate",
                _failure_reason(recall_gate, "production_recall_gate_failed"),
            )
        activate_strict = getattr(runtime, "activate_production_recall_strict_state", None)
        if not callable(activate_strict):
            return _blocked(
                report,
                "production_recall_strict_state",
                "production_recall_strict_activator_unavailable",
            )
        strict_state = activate_strict(
            scope=scope_payload,
            release_identity=receipt_identity,
            gate_record_id=str(recall_gate.get("record_id") or ""),
        )
        report["production_recall_strict_state"] = strict_state
        report["record_ids"]["production_recall_strict_state"] = str(strict_state.get("record_id") or "")
        if strict_state.get("ok") is not True or strict_state.get("status") != "strict_activated":
            return _blocked(
                report,
                "production_recall_strict_state",
                _failure_reason(strict_state, "production_recall_strict_activation_failed"),
            )

    # Release closure is the one remaining historic cohort workflow.  Keep
    # that choice explicit by going through the named compatibility facade;
    # it must never accidentally select the default dynamic catalog.
    run_replay_bootstrap = getattr(runtime, "run_weak_capability_replay_gate", None)
    if not callable(run_replay_bootstrap):
        return _blocked(
            report,
            "replay_bootstrap",
            "legacy_capability_replay_runner_unavailable",
        )
    replay_bootstrap = run_replay_bootstrap(
        scope=scope_payload,
        persist=True,
        loop_id="release_closure_bootstrap",
    )
    report["replay_bootstrap"] = replay_bootstrap
    from eimemory.governance.release.closure_contracts import legacy_release_replay_ok

    if not legacy_release_replay_ok(replay_bootstrap):
        return _blocked(
            report,
            "replay_bootstrap",
            _failure_reason(replay_bootstrap, "legacy_capability_replay_failed"),
        )

    live_acceptance = runtime.run_live_task_acceptance(**identity_kwargs)
    report["live_acceptance"] = live_acceptance
    if not _live_acceptance_ok(live_acceptance, receipt=receipt):
        return _blocked(report, "live_acceptance", _failure_reason(live_acceptance, "live_acceptance_failed"))

    record_channel_acceptance = getattr(
        runtime, "record_external_channel_acceptance", None
    )
    if not callable(record_channel_acceptance):
        return _blocked(
            report,
            "channel_acceptance",
            "external_channel_acceptance_recorder_unavailable",
        )
    channel_acceptance = record_channel_acceptance(
        scope=scope_payload,
        current_release=receipt_identity,
    )
    report["channel_acceptance"] = channel_acceptance
    report["record_ids"]["channel_acceptance"] = str(
        channel_acceptance.get("record_id") or ""
    )
    if channel_acceptance.get("ok") is not True:
        blocked = _blocked(
            report,
            "channel_acceptance",
            _failure_reason(
                channel_acceptance,
                "current_release_channel_acceptance_missing",
            ),
        )
        if (
            blocked["blocked_reason"] == "current_release_channel_receipt_not_found"
            and _checkpoint_release is None
        ):
            from eimemory.governance.release.release_closure_pending import (
                build_release_closure_pending,
                write_release_closure_pending,
            )

            checkpoint = build_release_closure_pending(
                scope=scope_payload,
                repo_root=repo_root,
                current_link=current_link,
                health_url=health_url,
                prior_commit=prior_commit,
                current_release=receipt_identity,
                release_path=str(receipt.get("release_path") or ""),
                record_ids=report["record_ids"],
                replay_bootstrap=replay_bootstrap,
                live_acceptance=live_acceptance,
                bootstrap_pending=bootstrap_pending,
            )
            blocked["pending_checkpoint"] = write_release_closure_pending(
                checkpoint,
                path=pending_path,
            )
            if blocked["pending_checkpoint"].get("ok") is True:
                from eimemory.governance.release.release_closure_pending import (
                    reconcile_release_closure_pending,
                )

                reconciled = reconcile_release_closure_pending(
                    runtime,
                    pending_path=pending_path,
                )
                if reconciled.get("report_type") == "l5_release_closure":
                    return reconciled
                blocked["post_write_reconcile"] = reconciled
        return blocked

    return _continue_release_closure(
        runtime,
        report=report,
        scope_payload=scope_payload,
        repo_root=repo_root,
        current_release=receipt_identity,
        replay_bootstrap=replay_bootstrap,
        live_acceptance=live_acceptance,
        bootstrap_pending=bootstrap_pending,
    )


def read_strict_deployment_receipt(runtime: Any, *, scope, repo_root: str, current_link: str,
                                   health_url: str, transaction_id: str, deployed_commit: str) -> dict:
    """Re-read the original strict receipt and live target without writing one."""
    from eimemory.governance.release.deployment_receipt import (
        inspect_immutable_deployment, strict_code_evolution_receipt_error, _deployment_receipt_response,
    )
    from eimemory.governance.release.evidence_contract import same_scope
    from eimemory.storage.code_evolution_store import digest_json
    owner = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if not transaction_id or not deployed_commit:
        return {"ok": False, "error": "managed_strict_receipt_identity_required"}
    live = inspect_immutable_deployment(runtime, scope=owner, repo=repo_root,
        current_link=current_link, health_url=health_url, expected_commit=deployed_commit,
        transaction_id=transaction_id)
    if live.get("ok") is not True:
        return {"ok": False, "error": "managed_strict_deployment_not_current"}
    records = runtime.store.list_records_by_meta_value(kinds=["promotion_request"], scope=owner,
        meta_key="transaction_id", meta_value=transaction_id, status="deployed", limit=10) or []
    for record in records:
        if (not same_scope(getattr(record, "scope", None), owner)
            or strict_code_evolution_receipt_error(runtime, scope=owner, record=record, deployed_commit=deployed_commit)):
            continue
        receipt = _deployment_receipt_response(record)
        if digest_json(receipt) == live.get("deployment_receipt_digest"):
            return receipt
    return {"ok": False, "error": "managed_strict_receipt_unavailable"}


def run_managed_post_resume_closure(runtime: Any, *, job_key: str, validation_context: dict | None = None) -> dict:
    """Real business/stability admission while serving is ready and timers held.

    This entry is deliberately separate from strict pre-observation. It never
    re-records an ordinary deployment receipt and never starts the L5 clock.
    """
    from eimemory.contracts.code_deployment_job import validate_request, request_key
    from eimemory.core.runtime_manager import deployment_job_status, read_registry
    from eimemory.governance.evolution.code_deployment_job import JOBS_ROOT, _read, _bound_request
    from eimemory.governance.evolution.code_deployment_job import validation_context as native_validation_context
    from eimemory.governance.evolution.code_evolution_transaction import effect_execution_authorized
    from eimemory.storage.code_evolution_store import CodeEvolutionStore
    from eimemory.evaluation.controlled_production_acceptance import load_controlled_policy
    from eimemory.governance.release.closure_contracts import controlled_production_closure_report_ok
    from eimemory.governance.l5.l5_readiness import _storage_migration_status
    from eimemory.governance.safety.prompt_safety_executor import bind_prompt_safety_from_service
    from eimemory.contracts.user_control_plane import user_mode
    import re

    report = {"ok": False, "closure_complete": False, "data_accumulating": False,
        "report_type": "managed_post_resume_business", "blocked_stage": "", "blocked_reason": "",
        "business_qualified": False, "observation_started": False, "l5_certification": "not_certified",
        "managed_job_key": job_key, "record_ids": {}}
    if not isinstance(job_key, str) or re.fullmatch(r"[0-9a-f]{64}", job_key) is None:
        return _blocked(report, "managed_job", "managed_job_key_invalid")
    body = validate_request(_read(JOBS_ROOT / job_key / "request.json"))
    if request_key(body) != job_key or body["operation"] != "deploy":
        return _blocked(report, "managed_job", "managed_forward_request_required")
    transaction = CodeEvolutionStore(runtime.store).get_transaction(body["transaction_id"])
    if (not transaction or transaction.get("current_state") != "HEALTHY" or transaction.get("terminal")
        or not effect_execution_authorized(transaction) or _bound_request(transaction, "deploy") != (job_key, body)):
        return _blocked(report, "transaction", "managed_healthy_transaction_required")
    scope = dict(body["scope"])
    report.update(scope=scope, managed_transaction_id=body["transaction_id"])
    _, child = native_validation_context(job_key, "post_resume")
    identity = ("job_key", "operation", "unit", "original_invocation", "parent_original_invocation",
        "target_commit", "controller_commit", "phase")
    if not isinstance(validation_context, dict) or any(validation_context.get(k) != child.get(k) for k in identity):
        return _blocked(report, "managed_resume", "native_validation_context_required")
    native = deployment_job_status(job_key)
    state = native.get("state") if isinstance(native.get("state"), dict) else {}
    invocation = str(child.get("parent_original_invocation") or "")
    started = _read(JOBS_ROOT / job_key / "started.json")
    serving = _read(JOBS_ROOT / job_key / "serving-ready.json")
    prepared = serving.get('native') if isinstance(serving.get('native'),dict) else {}
    registry = read_registry()
    expected_manager='user' if user_mode() else 'system'
    if (native.get("manager") != expected_manager or native.get("unit") != f"eimemory-code-deployment-job@{job_key}.service"
        or native.get("phase") != "serving_awaiting_business" or native.get('core_serving_ready') is not True
        or native.get('background_held') is not True or native.get('resumed') is not False
        or state.get("ActiveState") not in {"active", "activating"}
        or not re.fullmatch(r"[0-9a-f]{32}", invocation) or state.get("InvocationID") != invocation
        or native.get("original_invocation") != invocation
        or started != {"request_key": job_key, "invocation": invocation}
        or serving.get("request_key") != job_key or serving.get("invocation") != invocation
        or prepared.get('phase')!='serving_awaiting_business' or prepared.get('core_serving_ready') is not True
        or prepared.get('background_held') is not True or prepared.get('resumed') is not False
        or registry.get("runtime_commit") != body["candidate_commit"]
        or registry.get("controller_commit") != body["controller_commit"]):
        return _blocked(report, "managed_resume", "native_restoration_not_verified")
    policy = transaction["payload"]["authorized_policy"]
    receipt = read_strict_deployment_receipt(runtime, scope=scope, repo_root=transaction["repository_root"],
        current_link=policy["deployment"]["current_link"], health_url=policy["deployment"]["health_url"],
        transaction_id=body["transaction_id"], deployed_commit=body["candidate_commit"])
    if receipt.get("ok") is not True:
        return _blocked(report, "deployment_receipt", str(receipt.get("error") or "managed_strict_receipt_invalid"))
    report.update(deployment_receipt=receipt, deployment=_deployment_identity(receipt))
    report["record_ids"]["deployment_receipt"] = receipt["promotion_request_id"]
    try:
        controlled_policy = load_controlled_policy(runtime, scope)
    except (OSError, ValueError) as exc:
        return _blocked(report, "controlled_policy", "controlled_policy_invalid:" + type(exc).__name__)
    if controlled_policy is None or controlled_policy.get("purpose") != "historical_business_qualification":
        return _blocked(report, "controlled_policy", "historical_business_policy_required")
    migration = _storage_migration_status(runtime)
    report["storage_migrations"] = migration
    if migration.get("ok") is not True:
        return _blocked(report, "storage_migrations", "storage_migrations_pending")
    bind_prompt_safety_from_service(runtime)
    release = ReleaseIdentity(commit=receipt["commit"], version=receipt["version"],
        receipt_id=receipt["promotion_request_id"], session_id=receipt["release_session_id"])
    identity_kwargs = dict(scope=scope, repo_root=transaction["repository_root"],
        current_link=policy["deployment"]["current_link"], health_url=policy["deployment"]["health_url"],
        prior_commit=body["base_commit"], deployed_commit=body["candidate_commit"], deployment_receipt=receipt)
    from eimemory.governance.evolution.code_evolution_provider_authority import transaction_provider_authority
    from eimemory.governance.capability.provider_selection import provider_selection
    authority = transaction_provider_authority(transaction)
    try:
        _, fields = provider_selection(runtime, owner_scope=scope,
            profile_key=str(transaction.get("profile_key") or ""), snapshot=authority)
    except ValueError as exc:
        return _blocked(report, "provider_authority", str(exc))
    report.update(fields)
    identity_kwargs["profile_key"] = str(transaction.get("profile_key") or "")
    if authority is not None:
        identity_kwargs["provider_authority_snapshot"] = authority
    report = _run_controlled_operational_closure(runtime, report=report, scope=scope,
        identity_kwargs=identity_kwargs, release=release, policy=controlled_policy,
        background_held_observation=True)
    report["business_qualified"] = controlled_production_closure_report_ok(report)
    report["observation_started"] = False
    return report


def run_manual_post_resume_closure(runtime: Any, *, job_key: str, validation_context: dict) -> dict:
    """Root-reviewed manual release admission; never an automatic L5 source.

    The permit's deployment owner is independent from the nightly/watch owner.
    No transaction, policy consumption, observation anchor or L5 fact is made.
    """
    from eimemory.core.runtime_manager import deployment_job_status, read_registry
    from eimemory.governance.evolution.code_deployment_job import (
        validation_context as actual_context, managed_validation_report,
    )
    from eimemory.governance.release.evidence_contract import current_release_identity
    from eimemory.governance.release.closure_contracts import controlled_production_closure_report_ok
    from eimemory.evaluation.controlled_production_acceptance import load_controlled_policy
    from eimemory.governance.l5.l5_readiness import _storage_migration_status
    from eimemory.governance.safety.prompt_safety_executor import bind_prompt_safety_from_service
    from eimemory.contracts.user_control_plane import user_mode

    report = dict(ok=False, closure_complete=False, data_accumulating=False,
        report_type="manual_post_resume_business", blocked_stage="", blocked_reason="",
        business_qualified=False, observation_started=False, l5_certification="not_certified",
        manual_release=True, managed_job_key=job_key, record_ids={})
    body, child = actual_context(job_key, "post_resume")
    identity = ("job_key", "operation", "unit", "original_invocation", "parent_original_invocation",
        "target_commit", "controller_commit", "launch_controller_commit", "manual_release", "phase")
    if (body.get("manual_release") is not True or body["target_commit"] != body["candidate_commit"]
        or not isinstance(validation_context, dict)
        or any(validation_context.get(name) != child.get(name) for name in identity)):
        return _blocked(report, "managed_job", "root_reviewed_manual_context_required")
    parent = deployment_job_status(job_key)
    registry = read_registry()
    expected_manager='user' if user_mode() else 'system'
    if (parent.get('manager')!=expected_manager or parent.get("original_invocation") != child["parent_original_invocation"]
        or parent.get("phase") != "serving_awaiting_business" or parent.get("resumed") is not False
        or parent.get('core_serving_ready') is not True or parent.get('background_held') is not True
        or parent.get("manual_release") is not True
        or registry["runtime_commit"] != body["target_commit"]):
        return _blocked(report, "managed_resume", "native_restoration_not_verified")
    scope = dict(body["scope"])
    report["scope"] = scope
    receipt, native = managed_validation_report(job_key, "receipt", heartbeat=lambda: None)
    actual = current_release_identity(runtime, ScopeRef.from_dict(scope))
    if (native["exit_status"] != 0 or receipt.get("ok") is not True or actual is None
        or actual.commit != body["target_commit"] or actual.receipt_id != receipt.get("promotion_request_id")
        or actual.version != receipt.get("version") or receipt.get("strict_transaction") is True):
        return _blocked(report, "deployment_receipt", "manual_physical_receipt_unverified")
    report.update(deployment_receipt=receipt, deployment=_deployment_identity(receipt))
    report["record_ids"]["deployment_receipt"] = actual.receipt_id
    policy = load_controlled_policy(runtime, scope)
    if policy is None or policy.get("purpose") != "historical_business_qualification":
        return _blocked(report, "controlled_policy", "historical_business_policy_required")
    migration = _storage_migration_status(runtime)
    report["storage_migrations"] = migration
    if migration.get("ok") is not True:
        return _blocked(report, "storage_migrations", "storage_migrations_pending")
    bind_prompt_safety_from_service(runtime)
    identity_kwargs = dict(scope=scope, repo_root=registry["repository_root"],
        current_link="/opt/eimemory/current", health_url="http://127.0.0.1:8091/health",
        prior_commit=body.get("rollback_commit", body["base_commit"]), deployed_commit=body["target_commit"], deployment_receipt=receipt)
    report = _run_controlled_operational_closure(runtime, report=report, scope=scope,
        identity_kwargs=identity_kwargs, release=actual, policy=policy,
        background_held_observation=True)
    report["business_qualified"] = controlled_production_closure_report_ok(report)
    report.update(manual_release=True, observation_started=False, l5_certification="not_certified")
    return report


def _run_controlled_operational_closure(
    runtime: Any, *, report: dict[str, Any], scope: dict[str, Any],
    identity_kwargs: dict[str, Any], release: ReleaseIdentity, policy: dict[str, Any],
    background_held_observation: bool = False,
) -> dict[str, Any]:
    """Keep the signed current attempt stable through final closure readback."""
    from eimemory.evaluation.controlled_acceptance_window import (
        controlled_acceptance_window, ControlledAcceptanceWindowError,
    )
    try:
        with controlled_acceptance_window(runtime):
            return _run_controlled_operational_closure_locked(runtime, report=report,
                scope=scope, identity_kwargs=identity_kwargs, release=release, policy=policy,
                background_held_observation=background_held_observation is True)
    except ControlledAcceptanceWindowError as exc:
        return _blocked(report, 'controlled_acceptance_window', str(exc))


def _run_controlled_operational_closure_locked(
    runtime: Any, *, report: dict[str, Any], scope: dict[str, Any],
    identity_kwargs: dict[str, Any], release: ReleaseIdentity, policy: dict[str, Any],
    background_held_observation: bool = False,
) -> dict[str, Any]:
    """Exercise the release under explicit operator authority, without L5 claims."""
    from eimemory.evaluation.controlled_production_acceptance import (
        run_controlled_production_acceptance, verify_controlled_production_acceptance,
    )
    from eimemory.governance.release.closure_contracts import (
        controlled_production_closure_report_ok, historical_business_qualification_ok,
        legacy_release_replay_ok,
    )
    from eimemory.ops.controlled_stability import collect_controlled_stability_sample
    from eimemory.ops.controlled_rpc_probe import run_controlled_rpc_probe
    import time

    # Managed entry points do not pass through run_release_closure's report
    # initializer. These gates are not executed by this controlled workflow.
    # Preserve any existing verdict so a prior failure cannot be overwritten.
    # Do not stamp production_recall* as upstream_gate_not_run: that reason
    # implies an L5 upstream miss, but this path intentionally skips them.
    for key in ('production_recall_gate', 'production_recall_strict_state'):
        report.setdefault(key, {
            'ok': False, 'status': 'not_run',
            'reason': 'controlled_path_skips_production_recall',
        })
    for key in ('closure_rehearsal', 'readiness'):
        report.setdefault(key, {
            'ok': False, 'status': 'not_run',
            'reason': 'controlled_path_skips_l5_gates',
        })
    report.update(
        report_type="controlled_production_release_closure", legacy_compatibility=False,
        acceptance_basis="controlled_existing_memory", evidence_class="operator_controlled_existing_memory",
        closure_scope="operational_only", certifies_natural_queries=False, certifies_no_answer=False,
        natural_certification="not_certified", l5_certification="not_certified",
        natural_samples_created=0,
        acceptance_purpose=str(policy.get("purpose") or "operational"),
    )
    database_path = runtime.store.sqlite.path
    executed = run_controlled_production_acceptance(
        runtime, scope=scope, persist=True, stable_before=str(policy.get("stable_before") or ""),
    )
    report["controlled_production_acceptance"] = executed
    reference = str(executed.get("record_id") or executed.get("persisted_record_id") or "")
    if executed.get("ok") is not True or executed.get("accepted") is not True or not reference:
        return _blocked(report, "controlled_production_acceptance",
                        _failure_reason(executed, "controlled_production_acceptance_failed"))
    verified = verify_controlled_production_acceptance(
        runtime, scope=scope, release=release, record_id=reference, limit=500,
    )
    report["controlled_production_acceptance"] = verified
    report["record_ids"]["controlled_production_acceptance"] = reference
    if verified.get("ok") is not True or verified.get("accepted") is not True:
        return _blocked(report, "controlled_production_acceptance",
                        _failure_reason(verified, "controlled_production_acceptance_verification_failed"))
    if policy.get("purpose") == "historical_business_qualification":
        if not historical_business_qualification_ok(verified):
            return _blocked(report, "controlled_production_acceptance", "historical_business_qualification_invalid")
        report.update(closure_scope="historical_business", business_origin="historical_business",
                      validation_method="controlled_recall",
                      business_qualification=verified["business_qualification"])
    transport = run_controlled_rpc_probe(runtime, scope=scope, expected_commit=release.commit)
    report["rpc_controlled_probe"] = transport
    if transport.get("ok") is not True:
        return _blocked(report, "rpc_controlled_probe", "controlled_rpc_probe_failed")
    # RPC identity checks can predate the successful retrieval and leave a
    # cached retrieval-health fragment. Observe only after its native TTL.
    _wait_for_controlled_health_refresh()
    # Stability must not race projection restore: finish caught_up/clean first.
    from eimemory.ops.projection_acceptance_restore import ensure_candidate_projection_restored
    projection_before_stability = ensure_candidate_projection_restored(
        runtime, timeout_seconds=120.0)
    report["projection_acceptance_restore_before_stability"] = projection_before_stability
    if (projection_before_stability.get("applicable") is True
            and projection_before_stability.get("ok") is not True):
        return _blocked(report, "projection_acceptance_restore",
                        _failure_reason(projection_before_stability,
                                        "projection_acceptance_restore_failed"))
    # Sticky oneshot Result=failed from a prior nightly/monitor exit must not
    # permanently block restored stability / business closure (see 5aa2c14).
    if background_held_observation is not True:
        from eimemory.ops.controlled_stability import reset_failed_allowlisted_job_units
        report["job_unit_reset_failed"] = reset_failed_allowlisted_job_units()
    first_sample = collect_controlled_stability_sample(
        expected_commit=release.commit, database_path=database_path,
        background_held=background_held_observation,
    )
    if first_sample.get("ok") is not True:
        report["stability"] = first_sample
        return _blocked(report, "stability", _failure_reason(first_sample, "controlled_stability_failed"))
    report["natural_evidence"] = _controlled_natural_progress(runtime, policy=policy)
    report["natural_online_evidence"] = {
        **report["natural_evidence"], "observation_only": True, "blocks_business_closure": False,
    }

    runner = getattr(runtime, "run_weak_capability_replay_gate", None)
    if not callable(runner):
        return _blocked(report, "replay_bootstrap", "legacy_capability_replay_runner_unavailable")
    replay = runner(scope=scope, persist=True, loop_id="controlled_production_release_closure")
    report["replay_bootstrap"] = replay
    if not legacy_release_replay_ok(replay):
        return _blocked(report, "replay_bootstrap", _failure_reason(replay, "legacy_capability_replay_failed"))
    pg_barrier_required = (verified.get("candidate_readiness_barrier", {}).get("applicable") is True)
    if pg_barrier_required:
        ready = _wait_for_controlled_projection(runtime, expected_commit=release.commit)
        report.setdefault("projection_readiness", {})["after_replay"] = ready
        if ready.get("ok") is not True:
            return _blocked(report, "projection_readiness", "post_replay_projection_not_ready")
    live = runtime.run_live_task_acceptance(**identity_kwargs)
    report["live_acceptance"] = live
    if not _live_acceptance_ok(live, receipt=report["deployment_receipt"]):
        return _blocked(report, "live_acceptance", _failure_reason(live, "live_acceptance_failed"))
    recorder = getattr(runtime, "record_external_channel_acceptance", None)
    if not callable(recorder):
        return _blocked(report, "channel_acceptance", "external_channel_acceptance_recorder_unavailable")
    channel = recorder(scope=scope, current_release=release)
    report["channel_acceptance"] = channel
    report["record_ids"]["channel_acceptance"] = str(channel.get("record_id") or "")
    if channel.get("ok") is not True:
        return _blocked(report, "channel_acceptance",
                        _failure_reason(channel, "current_release_channel_acceptance_missing"))

    from eimemory.governance.release.controlled_release_lineage import finalize_controlled_release_lineage

    report['release_lineage_required'] = True
    lineage = finalize_controlled_release_lineage(
        runtime, scope=scope, repo_root=identity_kwargs['repo_root'], release=release,
        controlled_reference=reference, replay=replay, live=live,
        channel_reference=report['record_ids']['channel_acceptance'],
    )
    report['release_lineage'] = lineage
    report['record_ids']['release_lineage'] = str(lineage.get('record_id') or '')
    if any(lineage.get(key) is not True for key in ('ok', 'validated', 'compatible')):
        return _blocked(report, 'release_lineage',
                        _failure_reason(lineage, 'controlled_release_lineage_incompatible'))

    from eimemory.ops.controlled_stability import assess_controlled_stability

    # Replay, live and channel gates can write memory and advance index authority.
    # Allow a full observation window after they finish, even when they took
    # longer than the window themselves. Keep each sleep short and interruptible.
    started = time.monotonic()
    _wait_for_controlled_window(started)
    if pg_barrier_required:
        ready = _wait_for_controlled_projection(runtime, expected_commit=release.commit)
        report.setdefault("projection_readiness", {})["before_final_sample"] = ready
        if ready.get("ok") is not True:
            return _blocked(report, "projection_readiness", "post_acceptance_projection_not_ready")
    last_sample = collect_controlled_stability_sample(
        expected_commit=release.commit, database_path=database_path,
        background_held=background_held_observation,
    )
    stability = assess_controlled_stability(
        [first_sample, last_sample], expected_commit=release.commit, expected_version=release.version,
        minimum_interval_seconds=60,
    )
    report["stability"] = stability
    if stability.get("ok") is not True:
        return _blocked(report, "stability", _failure_reason(stability, "controlled_stability_failed"))
    # Live probes and the observation window are not authority caches. Recheck
    # the exact signed record, policy, high-water and physical sources last.
    verified = verify_controlled_production_acceptance(
        runtime, scope=scope, release=release, record_id=reference, limit=500,
    )
    report["controlled_production_acceptance"] = verified
    if verified.get("ok") is not True or verified.get("accepted") is not True:
        return _blocked(report, "controlled_production_acceptance",
                        _failure_reason(verified, "controlled_production_acceptance_verification_failed"))
    if policy.get("purpose") == "historical_business_qualification":
        if not historical_business_qualification_ok(verified):
            return _blocked(report, "controlled_production_acceptance", "historical_business_qualification_invalid")
        report["business_qualification"] = verified["business_qualification"]
    report.update(ok=True, closure_complete=True, data_accumulating=False)
    if not controlled_production_closure_report_ok(report):
        return _blocked(report, "controlled_report_contract", "controlled_production_closure_contract_invalid")
    return report


def _wait_for_controlled_projection(runtime: Any, *, expected_commit: str,
                                    timeout_seconds: float = 300.0) -> dict[str, Any]:
    """Wait for actual projection and listener proof after memory-writing gates.

    This is a separate maintenance phase. It never extends a recall deadline,
    writes the index, embeds, or treats a cached pre-write health proof as ready.
    Budget aligns with candidate readiness / max_index_lag (300s).
    """
    from datetime import datetime, timezone
    import math
    import time
    from eimemory.ops.candidate_readiness_barrier import wait_for_candidate_readiness
    from eimemory.ops.controlled_stability import _health
    from eimemory.ops.projection_acceptance_restore import ensure_candidate_projection_restored
    from eimemory.retrieval.readiness_contract import readiness_ok

    if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 300):
        raise ValueError("invalid_projection_wait_budget")
    started = time.monotonic(); deadline = started + timeout_seconds
    source = getattr(getattr(getattr(runtime, "memory", None), "recall_engine", None), "candidate_source", None)
    result: dict[str, Any] = {"schema": "controlled_projection_readiness_wait.v1", "ok": False,
        "started_at": datetime.now(timezone.utc).isoformat(), "timeout_seconds": timeout_seconds,
        "attempt_count": 0, "blocked_reason": "projection_wait_exhausted", "error_code_counts": {},
        "read_only": True, "recall_budget_changed": False}

    def finish(ok: bool, reason: str) -> dict[str, Any]:
        result.update(ok=ok, blocked_reason=reason, elapsed_seconds=round(time.monotonic()-started, 6),
                      completed_at=datetime.now(timezone.utc).isoformat())
        return result

    # CPA / memory-writing gates may bump authority while vector-sync is still
    # held; restore before the read-only accept budget starts.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return finish(False, "projection_wait_exhausted")
    projection_restore = ensure_candidate_projection_restored(
        runtime, timeout_seconds=min(120.0, remaining))
    result["projection_acceptance_restore"] = projection_restore
    if (projection_restore.get("applicable") is True
            and projection_restore.get("ok") is not True):
        return finish(False, "projection_acceptance_restore_failed")

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result["attempt_count"] += 1
        barrier = wait_for_candidate_readiness(runtime, timeout_seconds=min(300.0, remaining))
        result["candidate_readiness_barrier"] = barrier
        if (source is not getattr(getattr(getattr(runtime, "memory", None), "recall_engine", None), "candidate_source", None)):
            return finish(False, "candidate_source_changed")
        if time.monotonic() >= deadline:
            return finish(False, "projection_wait_exhausted")
        if barrier.get("ok") is not True:
            return finish(False, "candidate_projection_not_ready")
        measurement = barrier.get("final_measurement")
        if source is None or barrier.get("applicable") is not True or not readiness_ok(measurement):
            return finish(False, "projection_barrier_contract_invalid")
        try:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                break
            health = _health(expected_commit, timeout_seconds=min(8.0, remaining))
        except Exception:
            health = {"ok": False, "probe_error": "endpoint_health_unavailable"}
        result["endpoint_health"] = health
        if source is not getattr(getattr(getattr(runtime, "memory", None), "recall_engine", None), "candidate_source", None):
            return finish(False, "candidate_source_changed")
        if time.monotonic() >= deadline:
            return finish(False, "projection_wait_exhausted")
        endpoint = health.get("postgres_readiness")
        if (health.get("ok") is True and readiness_ok(endpoint)
                and all(endpoint.get(key) == measurement.get(key) for key in ("watermark", "authority_revision"))):
            return finish(True, "")
        code = "endpoint_projection_not_current"
        result["error_code_counts"][code] = result["error_code_counts"].get(code, 0) + 1
        remaining = deadline-time.monotonic()
        if remaining > 0:
            time.sleep(min(.25, remaining))
    return finish(False, "projection_wait_exhausted")


def _wait_for_controlled_health_refresh() -> None:
    from eimemory.adapters.eibrain.health_probe import health_fragment_cache_ttl_seconds
    import time

    deadline = time.monotonic() + health_fragment_cache_ttl_seconds() + 0.05
    while (remaining := deadline - time.monotonic()) > 0:
        time.sleep(min(remaining, 1.0))


def _wait_for_controlled_window(started: float) -> None:
    import time

    while (remaining := 60.0 - (time.monotonic() - started)) > 0:
        time.sleep(min(remaining, 1.0))


def _controlled_natural_progress(runtime: Any, *, policy: dict[str, Any]) -> dict[str, Any]:
    """Read natural-case progress without relabeling it or awarding certification."""
    from eimemory.evaluation.production_query_dataset import build_production_query_dataset

    agents = []
    for configured in policy.get("agents") or []:
        exact = dict(configured["scope"])
        built = build_production_query_dataset(runtime, scope=exact)
        progress = built.get("progress") if isinstance(built.get("progress"), dict) else {}
        agents.append({
            "agent_id": str(exact.get("agent_id") or ""),
            "accepted_case_count": progress.get("accepted_case_count"),
            "required_case_count": progress.get("required_case_count"),
            "natural_certification": "not_certified_by_controlled_acceptance",
        })
    return {"certifies_natural_queries": False, "natural_samples_created": 0, "agents": agents}


def _continue_release_closure(
    runtime: Any,
    *,
    report: dict[str, Any],
    scope_payload: dict[str, Any],
    repo_root: str,
    current_release: ReleaseIdentity,
    replay_bootstrap: dict[str, Any],
    live_acceptance: dict[str, Any],
    bootstrap_pending: dict[str, Any] | None,
) -> dict[str, Any]:
    rehearsal_kwargs: dict[str, Any] = {
        "scope": scope_payload,
        "persist": True,
        "replay_bootstrap": replay_bootstrap,
        "repo_root": repo_root,
        "legacy_compatibility": True,
    }
    lineage_finalizer_enabled = bool(
        callable(getattr(runtime, "record_release_lineage", None))
        and callable(getattr(runtime, "current_release_lineage", None))
    )
    if lineage_finalizer_enabled:
        rehearsal_kwargs["release_lineage_finalizer"] = (
            lambda capability_replay: _finalize_release_lineage(
                runtime,
                scope=scope_payload,
                repo_root=repo_root,
                current_release=current_release,
                receipt_record_id=current_release.receipt_id,
                recall_gate_record_id=str(report["record_ids"].get("production_recall_gate") or ""),
                strict_state_record_id=str(
                    report["record_ids"].get("production_recall_strict_state") or ""
                ),
                bootstrap_pending_record_id=str(
                    report["record_ids"].get("production_recall_bootstrap") or ""
                ),
                channel_acceptance_record_id=str(
                    report["record_ids"].get("channel_acceptance") or ""
                ),
                replay_bootstrap=replay_bootstrap,
                capability_replay=capability_replay,
                live_acceptance=live_acceptance,
            )
        )
    if bootstrap_pending is not None:
        rehearsal_kwargs.update(
            {
                "bootstrap_pending": bootstrap_pending,
                "release_identity": current_release,
            }
        )
    rehearsal = runtime.run_l5_closure_rehearsal(**rehearsal_kwargs)
    report["closure_rehearsal"] = rehearsal
    rehearsal_lineage = (
        rehearsal.get("release_lineage")
        if isinstance(rehearsal.get("release_lineage"), dict)
        else {}
    )
    if rehearsal_lineage:
        report["release_lineage"] = rehearsal_lineage
        report["record_ids"]["release_lineage"] = str(
            rehearsal_lineage.get("record_id") or ""
        )
    if not _rehearsal_gate_ok(rehearsal):
        blocked = _blocked(
            report,
            "closure_rehearsal",
            _failure_reason(rehearsal, "closure_rehearsal_failed"),
        )
        _record_self_repair_incident(runtime, scope=scope_payload, report=blocked)
        return blocked
    if bootstrap_pending is not None and not (
        rehearsal.get("ok") is True
        and rehearsal.get("closure_complete") is False
        and rehearsal.get("data_accumulating") is True
    ):
        return _blocked(report, "closure_rehearsal", "bootstrap_pending_rehearsal_state_invalid")

    readiness = (
        rehearsal.get("l5_readiness")
        if isinstance(rehearsal.get("l5_readiness"), dict)
        and rehearsal["l5_readiness"].get("schema_version") == "l5_readiness.v2"
        else runtime.build_l5_readiness_report(
            scope=scope_payload,
            persist=True,
            limit=1000,
            loop_id="release_closure",
            reader_mode="legacy",
            **({"repo_root": repo_root} if lineage_finalizer_enabled else {}),
        )
    )
    report["readiness"] = readiness
    report["record_ids"]["readiness"] = str(readiness.get("persisted_record_id") or "")
    readiness_status = readiness_gate_status(
        readiness,
        runtime=runtime,
        scope=scope_payload,
        repo_root=repo_root,
    )
    if bootstrap_pending is not None:
        pending_verification = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=scope_payload,
            bootstrap_pending=bootstrap_pending,
            release=current_release,
            readiness=readiness,
            repo_root=repo_root,
        )
        report["bootstrap_pending_verification"] = pending_verification
        if pending_verification.get("ok") is not True:
            return _blocked(
                report,
                "readiness",
                str(pending_verification.get("reason") or "bootstrap_data_pending_readiness_invalid"),
            )
        report["ok"] = True
        report["closure_complete"] = False
        report["data_accumulating"] = True
        return report
    if readiness_status != "L5":
        return _blocked(report, "readiness", "readiness_not_l5")

    report["ok"] = True
    report["closure_complete"] = True
    return report


def resume_release_closure(
    runtime: Any,
    *,
    checkpoint: dict[str, Any],
    current_release: ReleaseIdentity,
    channel_acceptance: dict[str, Any],
) -> dict[str, Any]:
    """A checkpoint is a retry cursor, not proof that old gates still pass."""
    from eimemory.governance.release.evidence_contract import same_release_authority
    from eimemory.governance.release.release_closure_pending import _validate_checkpoint

    checkpoint = _validate_checkpoint(dict(checkpoint))
    expected = ReleaseIdentity(
        commit=checkpoint["current_commit"], version=current_release.version,
        receipt_id=checkpoint["deployment_receipt_id"],
        session_id=checkpoint["release_session_id"],
    )
    if not same_release_authority(current_release, expected):
        return {"ok": False, "closure_complete": False, "data_accumulating": False,
                "report_type": "l5_release_closure", "blocked_stage": "deployment_receipt",
                "blocked_reason": "pending_release_authority_mismatch"}
    if os.environ.get("EIMEMORY_CODE_EVOLUTION_TRANSACTION_MODE") == "1":
        return {"ok": False, "closure_complete": False, "data_accumulating": False,
                "report_type": "l5_release_closure", "blocked_stage": "deployment_receipt",
                "blocked_reason": "checkpoint_resume_in_strict_transaction"}
    inputs = checkpoint["inputs"]
    # Deliberately do not consume cached passed_gate_reports or the supplied
    # channel summary. The normal pipeline rechecks both against the runtime.
    return run_release_closure(
        runtime, scope=checkpoint["scope"], repo_root=str(inputs.get("repo_root") or ""),
        current_link=str(inputs.get("current_link") or ""),
        health_url=str(inputs.get("health_url") or ""),
        prior_commit=checkpoint["prior_commit"], _checkpoint_release=expected,
    )


def _blocked(report: dict[str, Any], stage: str, reason: str) -> dict[str, Any]:
    report["ok"] = False
    report["closure_complete"] = False
    report["blocked_stage"] = str(stage)
    report["blocked_reason"] = str(reason)
    return report


def _record_self_repair_incident(
    runtime: Any,
    *,
    scope: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """Expose recording status without changing the original gate decision."""
    recording = {"recording_ok": False, "status": "recorder_unavailable",
                 "incident_record_id": "", "repair_complete": False}
    if getattr(runtime, "store", None) is None:
        report["failure_recording"] = recording
        return
    try:
        from eimemory.core.clock import now_iso
        from eimemory.ops.release_closure_failure import record_release_closure_failure

        result = record_release_closure_failure(
            runtime, scope=scope, closure_report=report, detected_at=now_iso(),
        )
        if not isinstance(result, dict):
            raise ValueError("invalid_incident_recorder_result")
        incident_id = str(result.get("incident_record_id") or "")
        status = str(result.get("status") or "recorder_result_invalid")
        needs_incident = status in {"failure_detected", "diagnosis_required"}
        recorded = result.get("recording_ok") is not False and (
            bool(incident_id) if needs_incident else status in {"evidence_waiting", "non_actionable"}
        )
        recording.update(recording_ok=recorded, status=status,
                         incident_record_id=incident_id)
    except Exception as exc:  # Observer failure must not replace the gate result.
        recording.update(status="incident_recording_failed", exception_type=type(exc).__name__)
    report["failure_recording"] = recording


def _failure_reason(stage_report: dict[str, Any], fallback: str) -> str:
    error = str(
        stage_report.get("error")
        or stage_report.get("blocked_reason")
        or stage_report.get("reason")
        or ""
    ).strip()
    if error:
        return error
    blocked = [str(item).strip() for item in stage_report.get("blocked_reasons") or [] if str(item).strip()]
    return blocked[0] if blocked else fallback


def _deployment_identity(receipt: dict[str, Any]) -> dict[str, str]:
    return {
        "commit": str(receipt.get("commit") or ""),
        "version": str(receipt.get("version") or ""),
        "release_path": str(receipt.get("release_path") or ""),
        "promotion_request_id": str(receipt.get("promotion_request_id") or ""),
    }


def _live_acceptance_ok(report: dict[str, Any], *, receipt: dict[str, Any]) -> bool:
    from eimemory.governance.release.closure_contracts import live_acceptance_report_ok

    return live_acceptance_report_ok(report, receipt=receipt)


def _rehearsal_gate_ok(rehearsal: dict[str, Any]) -> bool:
    complete = rehearsal.get("closure_complete") is True
    accumulating = rehearsal.get("data_accumulating") is True
    return bool(rehearsal.get("ok") is True and complete != accumulating)


def _recall_result_allows_bootstrap_pending(report: dict[str, Any]) -> bool:
    return bool(
        _missing_dataset_recall_result(report)
        or _passing_diagnostic_recall_result(report)
        or _bounded_latency_only_diagnostic_recall_result(report)
        or _incomplete_known_item_smoke_result(report)
    )


def _incomplete_known_item_smoke_result(report: dict[str, Any]) -> bool:
    """Known-item smoke stays uncertified and may only enter data accumulation.

    A clean diagnostic lookup is not judged relevance. Leakage, seed errors,
    and real blocking metrics stay fail-closed.
    """

    quality = report.get("quality_gate") if isinstance(report.get("quality_gate"), dict) else {}
    blocking = quality.get("blocking_metrics")
    sample_count = report.get("sample_count")
    return bool(
        report.get("ok") is True
        and report.get("accepted") is False
        and report.get("gate_status") == "diagnostic"
        and report.get("dataset_kind") == "diagnostic"
        and report.get("evaluation_contract") == "known_item_smoke.v1"
        and report.get("blocked_reason") == "recall_quality_evidence_incomplete"
        and quality.get("ok") is False
        and quality.get("blocked_reason") == "recall_quality_evidence_incomplete"
        and quality.get("vacuous") is True
        and quality.get("evidence_status") == "insufficient"
        and blocking == {}
        and report.get("errors") == []
        and type(report.get("seed_error_count")) is int
        and report.get("seed_error_count") == 0
        and type(sample_count) is int
        and sample_count > 0
        and _exact_zero_number(report.get("false_recall_rate"))
        and _exact_zero_number(report.get("forbidden_hit_rate"))
        and _exact_zero_int(report.get("cross_channel_leakage_count"))
        and _exact_zero_int(report.get("source_filter_leakage_count"))
    )


def _missing_dataset_recall_result(report: dict[str, Any]) -> bool:
    threshold = report.get("threshold_gate") if isinstance(report.get("threshold_gate"), dict) else {}
    blocking_metrics = threshold.get("blocking_metrics")
    cross_channel_leakage = report.get("cross_channel_leakage_count")
    source_filter_leakage = report.get("source_filter_leakage_count")
    return bool(
        report.get("ok") is False
        and report.get("accepted") is False
        and report.get("gate_status") == "not_run"
        and str(report.get("blocked_reason") or "") in _BOOTSTRAP_PENDING_RECALL_REASONS
        and _zero_or_missing(cross_channel_leakage)
        and _zero_or_missing(source_filter_leakage)
        and (blocking_metrics is None or blocking_metrics == {})
    )


def _passing_diagnostic_recall_result(report: dict[str, Any]) -> bool:
    quality = report.get("quality_gate") if isinstance(report.get("quality_gate"), dict) else {}
    return bool(
        report.get("ok") is True
        and report.get("accepted") is False
        and report.get("gate_status") == "diagnostic"
        and report.get("dataset_kind") == "diagnostic"
        and report.get("gate_ok") is True
        and report.get("passed_threshold") is True
        and report.get("blocked_reason") == ""
        and quality.get("ok") is True
        and quality.get("blocked_reason") == ""
        and quality.get("blocking_metrics") == {}
        and report.get("errors") == []
        and type(report.get("seed_error_count")) is int
        and report.get("seed_error_count") == 0
        and type(report.get("sample_count")) is int
        and int(report.get("sample_count")) > 0
        and _exact_zero_number(report.get("false_recall_rate"))
        and _exact_zero_number(report.get("forbidden_hit_rate"))
        and _exact_zero_int(report.get("cross_channel_leakage_count"))
        and _exact_zero_int(report.get("source_filter_leakage_count"))
    )


def _bounded_latency_only_diagnostic_recall_result(report: dict[str, Any]) -> bool:
    quality = report.get("quality_gate") if isinstance(report.get("quality_gate"), dict) else {}
    blocking = (
        quality.get("blocking_metrics")
        if isinstance(quality.get("blocking_metrics"), dict)
        else {}
    )
    latency = blocking.get("latency_ms_p95") if len(blocking) == 1 else None
    latency = latency if isinstance(latency, dict) else {}
    thresholds = quality.get("thresholds") if isinstance(quality.get("thresholds"), dict) else {}
    actual = latency.get("actual")
    threshold = latency.get("threshold")
    sample_count = report.get("sample_count")
    numeric = (
        isinstance(actual, (int, float))
        and not isinstance(actual, bool)
        and isinstance(threshold, (int, float))
        and not isinstance(threshold, bool)
    )
    bootstrap_smoke_sample = (
        type(sample_count) is int
        and 0 < sample_count <= _BOOTSTRAP_DIAGNOSTIC_MAX_LATENCY_SAMPLES
    )
    bounded_latency = bool(
        numeric
        and bootstrap_smoke_sample
        and float(threshold) > 0.0
        and float(actual) > float(threshold)
        and float(actual)
        <= float(threshold) * _BOOTSTRAP_DIAGNOSTIC_LATENCY_MULTIPLIER
        and latency.get("operator") == "<="
        and report.get("latency_ms_p95") == actual
        and thresholds.get("latency_ms_p95") == threshold
    )
    return bool(
        report.get("ok") is False
        and report.get("accepted") is False
        and report.get("gate_status") == "diagnostic"
        and report.get("dataset_kind") == "diagnostic"
        and report.get("gate_ok") is False
        and report.get("passed_threshold") is False
        and report.get("blocked_reason") == "recall_quality_gate_failed"
        and quality.get("ok") is False
        and quality.get("blocked_reason") == "recall_quality_gate_failed"
        and bounded_latency
        and report.get("errors") == []
        and type(report.get("seed_error_count")) is int
        and report.get("seed_error_count") == 0
        and bootstrap_smoke_sample
        and _exact_zero_number(report.get("false_recall_rate"))
        and _exact_zero_number(report.get("forbidden_hit_rate"))
        and _exact_zero_int(report.get("cross_channel_leakage_count"))
        and _exact_zero_int(report.get("source_filter_leakage_count"))
    )


def _exact_zero_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == 0.0


def _exact_zero_int(value: Any) -> bool:
    return type(value) is int and value == 0


def _zero_or_missing(value: Any) -> bool:
    return value is None or type(value) is int and value == 0
