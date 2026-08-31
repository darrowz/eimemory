"""Strict deployment admission, deliberately distinct from terminal L5 closure.

No transaction state, observation timestamp or terminal evidence is written
here. The effect owner remains the only writer that can enter OBSERVING.
"""
from __future__ import annotations

from dataclasses import asdict
from math import ceil
import re
from typing import Any

from eimemory.governance.deployment_receipt import strict_code_evolution_receipt_error
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.models.records import ScopeRef
from eimemory.storage.code_evolution_store import CodeEvolutionStore


def run_pre_observation_closure(runtime: Any, *, receipt: dict, transaction_id: str,
                                identity_kwargs: dict) -> dict:
    from eimemory.governance.closure_rehearsal import run_capability_replay_gate, run_l5_closure_rehearsal
    from eimemory.governance.code_evolution_effects import _l5_observation_semantics, _observation_provenance
    from eimemory.governance.l5_reader import build_l5_effective_report
    from eimemory.governance.l5_readiness import _storage_migration_status
    from eimemory.governance.release_closure import _live_acceptance_ok

    report = dict(ok=False, report_type="code_evolution_pre_observation", schema_version="1",
                  status="blocked", closure_complete=False, data_accumulating=False,
                  observation_started=False, legacy_compatibility=False,
                  blocked_stage="", blocked_reason="", deployment_receipt=receipt,
                  deployment={key: receipt.get(key) for key in ("commit", "version", "release_path", "promotion_request_id")})

    def blocked(stage: str, reason: str) -> dict:
        report.update(ok=False, blocked_stage=stage, blocked_reason=reason)
        return report

    if (receipt.get("ok") is not True or receipt.get("strict_transaction") is not True
            or not transaction_id or receipt.get("transaction_id") != transaction_id):
        return blocked("deployment_receipt", "strict_deployment_receipt_required")
    scope = ScopeRef.from_dict(identity_kwargs["scope"])
    transaction = CodeEvolutionStore(runtime.store).get_transaction(transaction_id)
    if (not transaction or transaction.get("terminal")
            or transaction.get("current_state") not in {"DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"}
            or transaction.get("candidate_commit") != receipt.get("commit")
            or not str(transaction.get("profile_key") or "").strip()
            or any(transaction.get(key) != value for key, value in asdict(scope).items())):
        return blocked("transaction", "pre_observation_transaction_mismatch")
    record = runtime.store.get_by_id(str(receipt.get("promotion_request_id") or ""), scope=scope)
    error = strict_code_evolution_receipt_error(runtime, scope=scope, record=record,
                                               deployed_commit=receipt["commit"])
    if error:
        return blocked("deployment_receipt", error)
    provenance = _observation_provenance(transaction)
    if provenance is None:
        return blocked("transaction", "pre_observation_provenance_invalid")
    report["transaction"] = {
        **{key: transaction.get(key) for key in (
            "transaction_id", "profile_key", "current_state", "base_commit", "candidate_commit", "deployed_commit",
        )},
        **provenance,
    }
    migration = _storage_migration_status(runtime)
    report["storage_migrations"] = migration
    if migration.get("ok") is not True:
        return blocked("storage_migrations", "storage_migrations_pending")
    profile = transaction["profile_key"]
    selection = dict(scope=asdict(scope), runtime_scope=asdict(scope), profile_key=profile, persist=True)
    bootstrap = run_capability_replay_gate(runtime, **selection, loop_id=f"pre_observation:{transaction_id}:0")
    report["replay_bootstrap"] = bootstrap
    if bootstrap.get("ok") is not True:
        return blocked("replay", "profile_replay_failed")
    try:
        rounds = _replay_round_count(bootstrap.get("capability_replay") or {}, profile)
    except ValueError as exc:
        return blocked("replay", str(exc))
    for index in range(1, rounds):
        extra = run_capability_replay_gate(runtime, **selection, loop_id=f"pre_observation:{transaction_id}:{index}")
        if extra.get("ok") is not True:
            return blocked("replay", "profile_replay_failed")
    live = runtime.run_live_task_acceptance(**identity_kwargs, profile_key=profile)
    report["live_acceptance"] = live
    if not _live_acceptance_ok(live, receipt=receipt):
        return blocked("live_acceptance", "live_acceptance_failed")
    rehearsal = run_l5_closure_rehearsal(runtime, **selection, replay_bootstrap=bootstrap,
        repo_root=identity_kwargs["repo_root"], correction_capability_id=str(transaction.get("capability_id") or ""))
    report["closure_rehearsal"] = rehearsal
    # This stage can reach the final reader but cannot truthfully finish L5
    # before observation. Earlier failures are never treated as waiting.
    if (rehearsal.get("blocked_reasons") != ["l5_readiness_not_l5"]
            or any((rehearsal.get(key) or {}).get("ok") is not True
                   for key in ("skill_call", "rollback", "capability_dashboard"))):
        return blocked("closure_rehearsal", "pre_observation_rehearsal_failed")
    release = ReleaseIdentity(commit=receipt["commit"], version=receipt["version"],
                              receipt_id=receipt["promotion_request_id"], session_id=receipt["release_session_id"])
    cohort = _current_replay_cohort(runtime, scope, release, bootstrap["capability_replay"])
    report["replay_cohort"] = cohort
    if cohort.get("ok") is not True:
        return blocked("replay", "current_release_replay_manifests_incomplete")
    gates = {"memory.recall": [], "channel.delivery": [],
             "memory.governance": cohort["manifest_record_ids"],
             "storage.integrity": [case["record_id"] for case in live["cases"]],
             "deployment.runtime": [receipt["promotion_request_id"]],
             "code.evolution": [receipt["promotion_request_id"]]}
    # Unchanged recall/channel domains may inherit revalidated evidence.
    # Changed domains cannot: the lineage validator will reject them here.
    lineage = runtime.record_release_lineage(scope=asdict(scope), repo_root=identity_kwargs["repo_root"],
                                            current_release=release, gate_evidence=gates)
    report["release_lineage"] = lineage
    if any(lineage.get(key) is not True for key in ("ok", "validated", "compatible")):
        return blocked("release_lineage", "current_lineage_incompatible")
    readiness = build_l5_effective_report(runtime, scope=asdict(scope), runtime_scope=asdict(scope),
        profile_key=profile, reader_mode="v3", persist=False, repo_root=identity_kwargs["repo_root"])
    report["readiness"] = readiness
    ready, measure = _l5_observation_semantics(readiness, transaction)
    report["observation_admission"] = measure
    if not ready:
        return blocked("readiness", "pre_observation_readiness_invalid")
    report.update(ok=True, status="ready_for_observation")
    return report


def _replay_round_count(replay: dict, profile: str) -> int:
    contract = replay.get("selection_contract") or {}
    capabilities = contract.get("capabilities") or []
    minimums = contract.get("minimums_by_capability") or {}
    cases = contract.get("expected_case_ids") or {}
    if (contract.get("mode") != "dynamic_profile" or contract.get("profile_key") != profile
            or not capabilities or set(capabilities) != set(minimums) or set(capabilities) != set(cases)):
        raise ValueError("pre_observation_profile_contract_invalid")
    rounds = 1
    for capability in capabilities:
        values = [minimums[capability].get(key) for key in ("minimum_executed", "minimum_distinct_evidence")]
        if not cases[capability] or any(type(value) is not int or value < 1 for value in values):
            raise ValueError("pre_observation_profile_contract_invalid")
        rounds = max(rounds, ceil(max(values) / len(set(cases[capability]))))
    # Execution budget, not a maturity threshold. Larger requirements block;
    # they must never be silently clipped into passing evidence.
    if rounds > 10:
        raise ValueError("pre_observation_replay_budget_exceeded")
    return rounds


def _current_replay_cohort(runtime: Any, scope: ScopeRef, release: ReleaseIdentity, replay: dict) -> dict:
    from eimemory.evaluation.capability_catalog import resolve_application_capability_catalog
    from eimemory.governance.l5_readiness import _verified_replay_summary
    from eimemory.governance.release_lineage import _manifest_profile_replay_contract

    reference = str(replay.get("manifest_record_id") or "")
    capabilities, minimums, error = _manifest_profile_replay_contract(
        {reference: runtime.store.get_by_id(reference, scope=scope)}, [reference], scope=scope)
    if error:
        return {"ok": False, "reason": error}
    summary = _verified_replay_summary(runtime, scope=scope, release=release, limit=2000,
        capabilities=capabilities, missing_field="missing", minimums=minimums,
        catalog=resolve_application_capability_catalog())
    return {"ok": bool(not summary["missing"] and not summary["manifest_rejection_reasons"]
                        and not summary["rejection_reasons"] and summary["fail_count"] == 0
                        and summary["not_run_count"] == 0 and summary["executed_count"] > 0),
            "manifest_record_ids": summary["manifest_record_id_cohort"], "summary": summary}


def pre_observation_report_ok(report: dict) -> bool:
    """Structural output contract for the trusted installer's report reader."""
    from eimemory.governance.code_evolution_effects import _l5_observation_semantics
    from eimemory.governance.release_closure import _live_acceptance_ok

    receipt = report.get("deployment_receipt") or {}
    transaction = report.get("transaction") or {}
    lineage = report.get("release_lineage") or {}
    readiness = report.get("readiness") or {}
    rehearsal = report.get("closure_rehearsal") or {}
    identity = lineage.get("current_release") or {}
    deployment = report.get("deployment") or {}
    return bool(report.get("ok") is True and report.get("report_type") == "code_evolution_pre_observation"
        and report.get("schema_version") == "1" and report.get("status") == "ready_for_observation"
        and report.get("closure_complete") is False and report.get("data_accumulating") is False
        and report.get("observation_started") is False and report.get("legacy_compatibility") is False
        and not report.get("blocked_stage") and not report.get("blocked_reason")
        and receipt.get("ok") is True and receipt.get("strict_transaction") is True
        and re.fullmatch(r"[0-9a-f]{40}", str(receipt.get("commit") or ""))
        and bool(receipt.get("promotion_request_id")) and bool(receipt.get("release_session_id"))
        and all(deployment.get(key) == receipt.get(key) for key in ("commit", "version", "release_path", "promotion_request_id"))
        and identity.get("commit") == receipt.get("commit")
        and identity.get("receipt_id") == receipt.get("promotion_request_id")
        and identity.get("session_id") == receipt.get("release_session_id")
        and receipt.get("transaction_id") == transaction.get("transaction_id")
        and transaction.get("candidate_commit") == receipt.get("commit")
        and (report.get("storage_migrations") or {}).get("ok") is True
        and (report.get("replay_bootstrap") or {}).get("ok") is True
        and (report.get("replay_cohort") or {}).get("ok") is True
        and _live_acceptance_ok(report.get("live_acceptance") or {}, receipt=receipt)
        and rehearsal.get("blocked_reasons") == ["l5_readiness_not_l5"]
        and all((rehearsal.get(key) or {}).get("ok") is True
                for key in ("skill_call", "rollback", "capability_dashboard"))
        and transaction.get("current_state") in {"DEPLOY_INTENT", "DEPLOYED_VERIFIED", "HEALTHY", "OBSERVING"}
        and all(lineage.get(key) is True for key in ("ok", "validated", "compatible"))
        and _l5_observation_semantics(readiness, transaction)[0])
