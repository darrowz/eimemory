from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from collections.abc import Callable
from typing import Any

from eimemory.governance.capability.capability_acceptance import (
    LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS,
    LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS,
)
from eimemory.governance.capability.capability_replay_packs import LEGACY_CORE_REPLAY_CAPABILITIES
from eimemory.governance.capability.change_policy import decide_change_policy
from eimemory.governance.release.evidence_contract import (
    ReleaseIdentity,
    release_identity_payload,
    same_release_authority,
)
from eimemory.governance.learning.learning_state import append_learning_record_once, stable_semantic_key
from eimemory.governance.l5.l5_readiness import _real_business_gate, readiness_gate_status
from eimemory.governance.l5.real_task_coverage import real_task_type_coverage_deficits
from eimemory.models.records import ScopeRef


NON_RECALL_EVIDENCE_INCOMPLETE = "bootstrap_pending_non_recall_l5_evidence_incomplete"


CORRECTION_TEXT = "\u4e0d\u8981\u8bf4\u505a\u4e0d\u5230\uff0c\u8981\u8865\u80fd\u529b\u89e3\u51b3"
CORRECTION_QUERY = "\u9047\u5230\u505a\u4e0d\u5230\u7684\u80fd\u529b\u600e\u4e48\u529e\uff1f\u4e0d\u8981\u8bf4\u505a\u4e0d\u5230\uff0c\u8981\u8865\u80fd\u529b\u89e3\u51b3"
LOOP_ID = "l5_closure_rehearsal"
# Historical fixed cohorts are compatibility-only.  Default rehearsal gets
# its cohort from the active Registry/Profile and never invents a weak/core
# partition from capability names.
LEGACY_WEAK_REPLAY_CAPABILITIES = [
    "search.discovery",
    "research.synthesis",
    "operations.uumit",
    "device.control",
]


def _resolve_repo_root(repo_root: str | Path | None) -> str:
    if repo_root is not None and str(repo_root).strip():
        return str(repo_root)
    from eimemory.config.trusted import trusted_repository_root
    return str(trusted_repository_root())


def run_l5_closure_rehearsal(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
    replay_bootstrap: dict[str, Any] | None = None,
    bootstrap_pending: dict[str, Any] | None = None,
    release_identity: ReleaseIdentity | None = None,
    release_lineage_finalizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    repo_root: str | None = None,
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: ScopeRef | dict[str, Any] | None = None,
    at_time: str = "",
    legacy_compatibility: bool = False,
    correction_capability_id: str = "",
) -> dict[str, Any]:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    exact_scope = runtime_scope if runtime_scope is not None else scope_ref
    report = _initial_closure_report(
        scope_ref,
        legacy_compatibility=legacy_compatibility,
        profile_key=profile_key,
        capability_scope=capability_scope,
    )

    bootstrap = (
        dict(replay_bootstrap)
        if isinstance(replay_bootstrap, dict)
        else run_capability_replay_gate(
            runtime,
            scope=scope_ref,
            persist=persist,
            loop_id=LOOP_ID,
            profile_key=profile_key,
            capability_scope=capability_scope,
            runtime_scope=exact_scope,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
    )
    acceptance = bootstrap.get("capability_acceptance") if isinstance(bootstrap.get("capability_acceptance"), dict) else {}
    report["capability_acceptance"] = acceptance
    report["sequence"].append("acceptance")
    # A caller-supplied bootstrap is evidence for one mode only.  In
    # particular, do not let a persisted historic weak cohort silently enter
    # the default Registry/Profile closure path just because it was supplied
    # as a prebuilt report.
    if bootstrap.get("legacy_compatibility") is not bool(legacy_compatibility):
        return _blocked_closure(report, "replay_bootstrap_legacy_compatibility_mismatch")
    if not _acceptance_gate(
        acceptance,
        expected_count=(len(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS) if legacy_compatibility else None),
    ):
        return _blocked_closure(report, *(bootstrap.get("blocked_reasons") or ["capability_acceptance_failed"]))

    weak_capability_replay = (
        bootstrap.get("weak_capability_replay")
        if isinstance(bootstrap.get("weak_capability_replay"), dict)
        else {}
    )
    replay_gate = bootstrap.get("replay_gate") if isinstance(bootstrap.get("replay_gate"), dict) else {}
    report["weak_capability_replay"] = weak_capability_replay
    report["replay_gate"] = replay_gate
    report["sequence"].append("replay")
    if bootstrap.get("ok") is not True or replay_gate.get("ok") is not True:
        return _blocked_closure(
            report,
            *(bootstrap.get("blocked_reasons") or replay_gate.get("blocked_reasons") or ["weak_capability_replay_invalid"]),
        )

    if legacy_compatibility:
        core_acceptance = _run_capability_acceptance(
            runtime,
            scope=scope_ref,
            persist=persist,
            case_ids=list(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS),
            profile_key="",
            capability_scope=capability_scope,
            runtime_scope=exact_scope,
            at_time=at_time,
            legacy_compatibility=True,
        )
    else:
        # A dynamic profile is one declared cohort.  Do not run a second,
        # hard-coded "core" cohort; retain these report fields only while
        # release consumers migrate to the generic names.
        core_acceptance = acceptance
    report["core_capability_acceptance"] = core_acceptance
    report["sequence"].append("core_acceptance")
    if not _acceptance_gate(
        core_acceptance,
        expected_count=(len(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS) if legacy_compatibility else None),
    ):
        return _blocked_closure(report, "core_capability_acceptance_failed")

    core_execution_id = str(core_acceptance.get("execution_id") or "").strip()
    core_probe_ids_by_case = {
        str(result.get("case_id") or "").strip(): str(result.get("probe_record_id") or "").strip()
        for result in core_acceptance.get("results") or []
        if isinstance(result, dict)
    }
    expected_core_case_ids = (
        set(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS)
        if legacy_compatibility
        else {
            str(result.get("case_id") or "").strip()
            for result in core_acceptance.get("results") or []
            if isinstance(result, dict) and str(result.get("case_id") or "").strip()
        }
    )
    if (
        not core_execution_id
        or not expected_core_case_ids
        or set(core_probe_ids_by_case) != expected_core_case_ids
        or any(not value for value in core_probe_ids_by_case.values())
    ):
        return _blocked_closure(report, "core_acceptance_anchor_missing")

    if legacy_compatibility:
        core_capability_replay = _build_capability_replay_packs(
            runtime,
            scope=scope_ref,
            capabilities=list(LEGACY_CORE_REPLAY_CAPABILITIES),
            persist=persist,
            loop_id=f"{LOOP_ID}_core",
            acceptance_execution_id=core_execution_id,
            acceptance_probe_ids_by_case=core_probe_ids_by_case,
            profile_key="",
            capability_scope=capability_scope,
            runtime_scope=exact_scope,
            at_time=at_time,
            legacy_compatibility=True,
        )
    else:
        core_capability_replay = weak_capability_replay
    core_replay_gate = _capability_replay_gate(
        core_capability_replay,
        expected_capabilities=(LEGACY_CORE_REPLAY_CAPABILITIES if legacy_compatibility else None),
        reason_prefix="core_capability_replay",
    )
    report["core_capability_replay"] = core_capability_replay
    report["core_replay_gate"] = core_replay_gate
    report["sequence"].append("core_replay")
    if core_replay_gate.get("ok") is not True:
        return _blocked_closure(
            report,
            *(core_replay_gate.get("blocked_reasons") or ["core_capability_replay_invalid"]),
        )
    if release_lineage_finalizer is not None:
        try:
            release_lineage = release_lineage_finalizer(core_capability_replay)
        except Exception as exc:
            release_lineage = {
                "ok": False,
                "validated": False,
                "compatible": False,
                "error": f"release_lineage_finalization_error:{type(exc).__name__}",
            }
        report["release_lineage"] = release_lineage
        report["sequence"].append("release_lineage")
        if not (
            isinstance(release_lineage, dict)
            and release_lineage.get("ok") is True
            and release_lineage.get("validated") is True
            and release_lineage.get("compatible") is True
        ):
            return _blocked_closure(
                report,
                str(
                    (release_lineage if isinstance(release_lineage, dict) else {}).get("error")
                    or "release_lineage_not_compatible"
                ),
            )

    target_capability = str(correction_capability_id or "").strip()
    if legacy_compatibility and not target_capability:
        target_capability = "proactive.judgment"
    if not target_capability:
        return _blocked_closure(report, "correction_capability_required")
    if not legacy_compatibility and target_capability not in _acceptance_capability_ids(acceptance):
        return _blocked_closure(report, "correction_capability_not_in_profile")
    correction_replay = runtime.record_user_correction_replay(
        {
            "text": CORRECTION_TEXT,
            "context": "assistant stopped at inability instead of creating the missing capability path",
            "target_capability": target_capability,
            "expected_behavior": "When a capability is missing, create a concrete plan, replay, gated implementation path, and rollback boundary.",
        },
        scope=asdict(scope_ref),
        persist=persist,
    )
    report["correction_replay"] = correction_replay
    report["sequence"].append("skill_rollback")
    if correction_replay.get("ok") is not True:
        return _blocked_closure(report, "correction_replay_failed")

    pre_answer_gate = runtime.build_ground_truth_pre_answer_gate(
        query=CORRECTION_QUERY,
        scope=asdict(scope_ref),
        persist=persist,
    )
    report["pre_answer_gate"] = pre_answer_gate
    if int(pre_answer_gate.get("matched_rule_count") or 0) < 1:
        return _blocked_closure(report, "ground_truth_rule_not_matched")

    playbook_ids = _seed_eiskill_playbooks(
        runtime,
        scope=scope_ref,
        persist=persist,
        target_capability=target_capability,
    )
    skill_promotion = runtime.promote_repeated_sops_to_skill_candidates(
        scope=asdict(scope_ref), min_repeats=3, persist=persist, limit=50
    )
    report["playbook_record_ids"] = playbook_ids
    report["skill_promotion"] = skill_promotion
    skill_id = _rehearsal_skill_id(skill_promotion, target_capability=target_capability, playbook_ids=playbook_ids)
    if not skill_id:
        return _blocked_closure(report, "skill_call_failed")
    skill_call = runtime.call_eiskill(
        skill_id=skill_id,
        scope=asdict(scope_ref),
        context={"query": CORRECTION_QUERY, "rehearsal": True},
        persist=persist,
    )
    report["skill_call"] = skill_call
    if skill_call.get("ok") is not True:
        return _blocked_closure(report, "skill_call_failed")

    rollback = _run_non_destructive_rollback(runtime, scope=scope_ref, persist=persist)
    report["rollback"] = rollback
    if rollback.get("ok") is not True:
        return _blocked_closure(report, "rollback_rehearsal_failed")

    observation_input = _observation_autonomous_report(
        replay_report=weak_capability_replay,
        skill_promotion=skill_promotion,
        rollback=rollback,
    )
    l5_observation = runtime.run_l5_cycle(
        scope=asdict(scope_ref),
        apply=False,
        force=False,
        max_goals=1,
        max_promotions=0,
        allow_network=False,
        loop_id=f"{LOOP_ID}_observation",
        persist=persist,
        autonomous_learning_report=observation_input,
        profile_key=profile_key,
        capability_scope=capability_scope,
        at_time=at_time,
        # A closure explicitly requested as legacy must keep that mode across
        # its observation and reader stages.  Do not infer this from a
        # release, machine, or empty dynamic selection.
        legacy_compatibility=legacy_compatibility,
    )
    report["l5_observation"] = l5_observation
    report["sequence"].append("l5_observation_assessment")
    assessment = l5_observation.get("assessment") if isinstance(l5_observation.get("assessment"), dict) else {}
    # Structural observation is non-authoritative and stays below L5. A
    # bootstrap-pending release must continue into the readiness contract
    # instead of treating that ceiling as a failed observation. Strict
    # closure still requires a complete L5 assessment.
    structural_ceiling = (
        legacy_compatibility
        and bootstrap_pending is not None
        and l5_observation.get("ok") is True
        and assessment.get("complete") is not True
        and assessment.get("level") != "L5"
    )
    if legacy_compatibility and not structural_ceiling:
        if l5_observation.get("ok") is not True:
            return _blocked_closure(report, "l5_observation_assessment_incomplete")
        # The observation ran, but legacy structural evidence is by design
        # non-authoritative: it can never self-certify "complete"/"L5"
        # (l5_loop caps the structural assessment below L5 on purpose).  A
        # bootstrap-pending release binding is the only sanctioned
        # continuation for a legacy closure, so surface that contract
        # explicitly instead of the previously misleading
        # "assessment_incomplete" - which read as a data problem while
        # being a structural impossibility without bootstrap_pending.
        return _blocked_closure(report, "legacy_closure_requires_bootstrap_pending")

    capability_dashboard = runtime.build_capability_dashboard_metrics(
        scope=asdict(scope_ref), persist=persist, loop_id=LOOP_ID
    )
    report["capability_dashboard"] = capability_dashboard
    report["sequence"].append("dashboard")
    if capability_dashboard.get("ok") is not True:
        return _blocked_closure(report, "capability_dashboard_failed")

    l5_readiness = runtime.build_l5_readiness_report(
        scope=asdict(scope_ref),
        persist=persist,
        loop_id=LOOP_ID,
        repo_root=repo_root,
        reader_mode="legacy" if legacy_compatibility else "v3",
        profile_key=profile_key,
        capability_scope=capability_scope,
    )
    report["l5_readiness"] = l5_readiness
    report["sequence"].append("readiness")
    readiness_status = (
        readiness_gate_status(
            l5_readiness,
            runtime=runtime,
            scope=scope_ref,
            repo_root=repo_root,
        )
        if legacy_compatibility
        else _dynamic_readiness_status(l5_readiness)
    )
    if bootstrap_pending is not None:
        if not legacy_compatibility:
            # The bootstrap-pending exception is a frozen v2 release contract.
            # It cannot be used to weaken a Profile-backed v3 closure gate.
            return _blocked_closure(report, "bootstrap_pending_requires_legacy_compatibility")
        if readiness_status:
            return _blocked_closure(report, "bootstrap_pending_readiness_must_remain_l45")
        pending_verification = verify_bootstrap_pending_readiness_contract(
            runtime,
            scope=scope_ref,
            bootstrap_pending=bootstrap_pending,
            release=release_identity,
            readiness=l5_readiness,
            repo_root=repo_root,
        )
        report["bootstrap_pending_verification"] = pending_verification
        if pending_verification.get("ok") is not True:
            return _blocked_closure(
                report,
                str(pending_verification.get("reason") or "bootstrap_pending_contract_invalid"),
            )
    elif not readiness_status:
        if release_identity is None:
            return _blocked_closure(report, "l5_readiness_not_l5")
        return _blocked_closure(report, "l5_readiness_not_l5")
    report["outcome_trace"] = _record_successful_task_outcome(runtime, scope=scope_ref, persist=persist)
    report["ok"] = True
    report["closure_complete"] = readiness_status == "L5"
    report["data_accumulating"] = not report["closure_complete"]
    report["change_policy"] = decide_change_policy(
        event="code_change",
        closure_complete=bool(report["closure_complete"]),
    )
    report["blocked_reasons"] = []
    return report


def verify_bootstrap_pending_readiness_contract(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None,
    bootstrap_pending: dict[str, Any] | None,
    release: ReleaseIdentity | None,
    readiness: dict[str, Any],
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Verify the one release-bound L4.5 state allowed inside release closure."""

    rejected = {
        "ok": False,
        "status": "blocked",
        "reason": "bootstrap_pending_contract_invalid",
        "record_id": "",
    }
    if not isinstance(release, ReleaseIdentity) or not release.complete:
        return {**rejected, "reason": "bootstrap_pending_release_identity_invalid"}
    if not isinstance(bootstrap_pending, dict):
        return rejected
    from eimemory.evaluation.real_query_gate import verify_current_bootstrap_data_pending

    reverified = verify_current_bootstrap_data_pending(
        runtime,
        scope=scope,
        release=release,
    )
    if reverified.get("ok") is not True:
        return {
            **rejected,
            "reason": str(reverified.get("reason") or "bootstrap_pending_reverification_failed"),
            "record_id": str(reverified.get("record_id") or ""),
            "reverified": reverified,
        }
    compared_fields = ("ok", "status", "reason", "record_id", "progress", "release_identity")
    if any(bootstrap_pending.get(field) != reverified.get(field) for field in compared_fields):
        return {
            **rejected,
            "reason": "bootstrap_pending_credential_mismatch",
            "record_id": str(reverified.get("record_id") or ""),
            "reverified": reverified,
        }
    evidence_diagnostics: dict[str, Any] = {}
    evidence_reason = _bootstrap_pending_readiness_evidence_reason(
        runtime,
        readiness,
        scope=scope,
        release=release,
        pending_record_id=str(reverified.get("record_id") or ""),
        repo_root=repo_root,
        diagnostics=evidence_diagnostics,
    )
    if evidence_reason:
        return {
            **rejected,
            "reason": evidence_reason,
            "record_id": str(reverified.get("record_id") or ""),
            "reverified": reverified,
            # Bounded, diagnostic-only codes naming which non-recall evidence
            # is missing. They never change the rejection above.
            **(
                {
                    "non_recall_evidence_deficits": list(
                        evidence_diagnostics.get("non_recall_evidence_deficits") or []
                    )
                }
                if evidence_reason == NON_RECALL_EVIDENCE_INCOMPLETE
                else {}
            ),
        }
    return {
        "ok": True,
        "status": "bootstrap_data_pending",
        "reason": str(reverified.get("reason") or ""),
        "record_id": str(reverified.get("record_id") or ""),
        "progress": dict(reverified.get("progress") or {}),
        "release_identity": release_identity_payload(release),
        "reverified": reverified,
    }


def _bootstrap_pending_readiness_evidence_reason(
    runtime: Any,
    readiness: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None,
    release: ReleaseIdentity,
    pending_record_id: str,
    repo_root: str,
    diagnostics: dict[str, Any] | None = None,
) -> str:
    if diagnostics is None:
        diagnostics = {}
    if not isinstance(readiness, dict) or readiness.get("schema_version") != "l5_readiness.v2":
        return "bootstrap_pending_readiness_schema_invalid"
    if readiness.get("ok") is not True:
        return "bootstrap_pending_readiness_not_ok"
    identity = readiness.get("release_identity") if isinstance(readiness.get("release_identity"), dict) else {}
    if not same_release_authority(
        ReleaseIdentity(
            commit=str(identity.get("release_commit") or ""),
            version=str(identity.get("release_version") or ""),
            receipt_id=str(identity.get("deployment_receipt_id") or ""),
            session_id=str(identity.get("release_session_id") or ""),
        ),
        release,
    ):
        return "bootstrap_pending_readiness_release_mismatch"
    score = readiness.get("observed_score", readiness.get("readiness_score"))
    if (
        readiness.get("observed_stage", readiness.get("current_stage")) != "L4.5"
        or not isinstance(score, (int, float))
        or isinstance(score, bool)
        or float(score) != 0.8
    ):
        return "bootstrap_pending_readiness_not_exact_l45"
    recall = readiness.get("production_recall_gate") if isinstance(readiness.get("production_recall_gate"), dict) else {}
    if not _bootstrap_pending_recall_gap_is_dataset_only(recall):
        return "bootstrap_pending_recall_gap_not_dataset_only"
    strict = (
        readiness.get("production_recall_strict_state")
        if isinstance(readiness.get("production_recall_strict_state"), dict)
        else {}
    )
    if not (
        strict.get("ok") is False
        and strict.get("status") == "not_run"
        and strict.get("reason") == "strict_state_missing"
        and str(strict.get("record_id") or "") == pending_record_id
    ):
        return "bootstrap_pending_strict_gap_invalid"
    weak = readiness.get("verified_replay") if isinstance(readiness.get("verified_replay"), dict) else {}
    core = (
        readiness.get("verified_core_replay")
        if isinstance(readiness.get("verified_core_replay"), dict)
        else {}
    )
    if not _replay_summary_consistent(weak) or not _replay_summary_consistent(core):
        return "bootstrap_pending_replay_evidence_inconsistent"
    live_gate = (
        readiness.get("live_task_gate")
        if isinstance(readiness.get("live_task_gate"), dict)
        else {}
    )
    replay_gate = (
        readiness.get("verified_real_replay")
        if isinstance(readiness.get("verified_real_replay"), dict)
        else {}
    )
    real_business_gate = _real_business_gate(live_gate, replay_gate)
    live_task_accumulating = real_business_gate.get("ok") is not True
    if live_task_accumulating and not _compatible_live_task_accumulation(
        readiness,
        release=release,
    ):
        diagnostics["non_recall_evidence_deficits"] = bootstrap_pending_non_recall_deficits(
            readiness,
            release=release,
        )
        return NON_RECALL_EVIDENCE_INCOMPLETE
    shadow_live_gate = (
        {
            **live_gate,
            "ok": True,
            # Forge the full live-path minimum standard, not just the sample
            # count: the tightened real-business gate also requires task-type
            # diversity, so a shadow without it cannot represent a completed
            # accumulation.
            "current_deployment_verified_real_tasks": 10,
            "distinct_task_types": 5,
        }
        if live_task_accumulating
        else live_gate
    )
    shadow = {
        **readiness,
        "current_stage": "L5",
        "observed_stage": "L5",
        "readiness_score": 1.0,
        "observed_score": 1.0,
        "live_task_gate": shadow_live_gate,
        "real_business_gate": _real_business_gate(shadow_live_gate, replay_gate),
        "production_recall_gate": {"ok": True, "status": "accepted"},
        "production_recall_strict_state": {
            "ok": True,
            "status": "strict_activated",
            "candidate_commit": release.commit,
        },
    }
    if readiness_gate_status(
        shadow,
        runtime=runtime,
        scope=scope,
        repo_root=repo_root,
    ) != "L5":
        diagnostics["non_recall_evidence_deficits"] = [
            "shadow_readiness_gate_not_l5",
        ]
        return NON_RECALL_EVIDENCE_INCOMPLETE
    return ""


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _present_and_malformed_count(value: Any) -> bool:
    return value is not None and _count(value) is None


def _optional_count_well_formed(value: Any) -> bool:
    return value is None or _count(value) is not None


def _optional_rate_well_formed(value: Any) -> bool:
    from math import isfinite

    if value is None:
        return True
    if type(value) not in (int, float) or isinstance(value, bool):
        return False
    try:
        return bool(isfinite(value) and 0.0 <= float(value) <= 1.0)
    except OverflowError:
        return False


_REPLAY_AUTHENTICITY_REASONS = frozenset({
    "terminal_evidence_invalid",
    "source_provenance_invalid",
    "source_evidence_digest_mismatch",
    "terminal_evidence_digest_mismatch",
    "source_task_type_mismatch",
    "malformed_sample",
    "malformed_verdict",
    "duplicate_terminal_evidence",
    "duplicate_source_record",
    "case_scope_mismatch",
    "source_record_id_missing",
})


def _reported_collection_blocks(enabled_types: object, counts: object) -> bool:
    """A missing collection claim is not a quota failure.

    Older readiness reports omit the field. A present but malformed claim
    still fails closed.
    """

    if enabled_types is None and counts is None:
        return False
    return bool(real_task_type_coverage_deficits(enabled_types, counts))


def _replay_authenticity_deficits(replay: dict[str, Any]) -> list[str]:
    reasons = replay.get("rejection_reasons")
    if isinstance(reasons, dict) and any(
        reasons.get(reason) for reason in _REPLAY_AUTHENTICITY_REASONS
    ):
        return ["verified_real_replay_authenticity_failed"]
    if str(replay.get("reason") or "") in _REPLAY_AUTHENTICITY_REASONS:
        return ["verified_real_replay_authenticity_failed"]
    return []


def _rate(value: Any) -> float | None:
    from math import isfinite

    if type(value) not in (int, float):
        return None
    try:
        return float(value) if isfinite(value) and 0.0 <= value <= 1.0 else None
    except OverflowError:
        return None


def _channel_delivery_lineage_verified(channel: dict[str, Any], release: ReleaseIdentity) -> bool:
    """Accept an unchanged inheritance or a receipt-matched current change.

    ``changed_unverified`` and a current mode whose evidence release does not
    match this release stay blocked. An inherited domain still requires
    ``changed`` to be false.
    """

    if channel.get("gate_errors") != {}:
        return False
    mode = channel.get("mode")
    if mode == "inherited":
        return channel.get("changed") is False
    if mode != "current":
        return False
    evidence = channel.get("evidence_release")
    if not isinstance(evidence, dict):
        return False
    return same_release_authority(
        ReleaseIdentity(
            commit=str(evidence.get("commit") or ""),
            version=str(evidence.get("version") or ""),
            receipt_id=str(evidence.get("receipt_id") or ""),
            session_id=str(evidence.get("session_id") or ""),
        ),
        release,
    )


def bootstrap_pending_non_recall_deficits(
    readiness: dict[str, Any],
    *,
    release: ReleaseIdentity,
) -> list[str]:
    """Name the non-recall L5 evidence that blocks bootstrap accumulation.

    Diagnostic only: this mirrors the thresholds of the real-business gate and
    of ``_compatible_live_task_accumulation`` so an operator can see *which*
    evidence is missing. It never admits a report; the verdict stays with the
    original gate. Output is a sorted list of fixed codes, never payload text.
    """

    def obj(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    readiness = obj(readiness)
    live = obj(readiness.get("live_task_gate"))
    replay = obj(readiness.get("verified_real_replay"))
    samples = obj(readiness.get("hard_metric_samples"))
    lineage = obj(readiness.get("release_lineage"))
    deficits: set[str] = set()

    # Quantity and pass rate are lifecycle observations. Only unreported or
    # malformed collection evidence, and a replay that failed authenticity,
    # belong in this list.
    deficits.update(real_task_type_coverage_deficits(
        live.get("enabled_task_types"),
        live.get("per_type_sample_counts"),
    ))
    deficits.update(real_task_type_coverage_deficits(
        samples.get("enabled_task_types"),
        samples.get("verified_real_task_type_counts"),
    ))
    deficits.update(_replay_authenticity_deficits(replay))
    probes = _count(samples.get("current_deployment_operational_probes"))
    live_probes = _count(live.get("current_deployment_operational_probes"))
    if (
        probes is not None
        and live_probes is not None
        and live.get("current_deployment_operational_probes") is not None
        and live_probes != probes
    ):
        deficits.add("current_release_operational_probe_count_inconsistent")
    if _present_and_malformed_count(live.get("current_deployment_verified_real_tasks")):
        deficits.add("capability_reuse_evidence_malformed")
    if _present_and_malformed_count(samples.get("verified_real_tasks")):
        deficits.add("capability_reuse_evidence_malformed")
    if not (
        live.get("evidence_mode") == "current_release"
        and str(live.get("evidence_release_commit") or "") == release.commit
        and str(live.get("current_release_commit") or "") == release.commit
    ):
        deficits.add("live_task_gate_release_binding_mismatch")
    if not (
        lineage.get("ok") is True
        and lineage.get("validated") is True
        and lineage.get("compatible") is True
    ):
        deficits.add("release_lineage_not_compatible")
    else:
        current = obj(lineage.get("current_release"))
        if not same_release_authority(
            ReleaseIdentity(
                commit=str(current.get("commit") or ""),
                version=str(current.get("version") or ""),
                receipt_id=str(current.get("receipt_id") or ""),
                session_id=str(current.get("session_id") or ""),
            ),
            release,
        ):
            deficits.add("release_lineage_release_mismatch")
        channel = obj(obj(lineage.get("domains")).get("channel.delivery"))
        if not _channel_delivery_lineage_verified(channel, release):
            deficits.add("channel_delivery_lineage_not_verified")
    return sorted(deficits)


def _bootstrap_pending_recall_gap_is_dataset_only(recall: dict[str, Any]) -> bool:
    if recall.get("ok") is not False:
        return False
    status = str(recall.get("status") or "")
    reason = str(recall.get("reason") or "")
    record_id = str(recall.get("record_id") or "")
    if status == "not_run" and reason == "current_release_production_recall_report_missing":
        return record_id == ""
    if status in {"not_run", "data_accumulating", "blocked"} and reason == "query_features_low_signal":
        return True
    return False


def _compatible_live_task_accumulation(
    readiness: dict[str, Any],
    *,
    release: ReleaseIdentity,
) -> bool:
    live = (
        readiness.get("live_task_gate")
        if isinstance(readiness.get("live_task_gate"), dict)
        else {}
    )
    metrics = (
        readiness.get("hard_metrics")
        if isinstance(readiness.get("hard_metrics"), dict)
        else {}
    )
    quality = (
        readiness.get("hard_metric_quality")
        if isinstance(readiness.get("hard_metric_quality"), dict)
        else {}
    )
    samples = (
        readiness.get("hard_metric_samples")
        if isinstance(readiness.get("hard_metric_samples"), dict)
        else {}
    )
    lineage = (
        readiness.get("release_lineage")
        if isinstance(readiness.get("release_lineage"), dict)
        else {}
    )
    lineage_current = (
        lineage.get("current_release")
        if isinstance(lineage.get("current_release"), dict)
        else {}
    )
    channel = (
        lineage.get("domains", {}).get("channel.delivery")
        if isinstance(lineage.get("domains"), dict)
        and isinstance(lineage.get("domains", {}).get("channel.delivery"), dict)
        else {}
    )
    verified_real_quality = (
        quality.get("verified_real_task_success_rate")
        if isinstance(quality.get("verified_real_task_success_rate"), dict)
        else {}
    )
    current_live_quality = (
        quality.get("current_deployment_live_task_success_rate")
        if isinstance(quality.get("current_deployment_live_task_success_rate"), dict)
        else {}
    )
    current_real_raw = (
        live.get("current_deployment_verified_real_tasks")
        if live.get("current_deployment_verified_real_tasks") is not None
        else live.get("sample_count")
    )
    count_values = (
        current_real_raw, live.get("sample_count"), live.get("distinct_task_types"),
        samples.get("verified_real_tasks"), samples.get("verified_real_task_types"),
        samples.get("current_deployment_operational_probes"),
        samples.get("current_deployment_live_task_types"),
        live.get("current_deployment_operational_probes"),
        verified_real_quality.get("sample_count"), current_live_quality.get("sample_count"),
    )
    rate_values = (
        live.get("success_rate"),
        metrics.get("verified_real_task_success_rate"),
        metrics.get("current_deployment_live_task_success_rate"),
    )
    if any(not _optional_count_well_formed(value) for value in count_values):
        return False
    if any(not _optional_rate_well_formed(value) for value in rate_values):
        return False
    if _reported_collection_blocks(
        samples.get("enabled_task_types"),
        samples.get("verified_real_task_type_counts"),
    ) or _reported_collection_blocks(
        live.get("enabled_task_types"),
        live.get("per_type_sample_counts"),
    ):
        return False
    live_probes = live.get("current_deployment_operational_probes")
    sample_probes = samples.get("current_deployment_operational_probes")
    if (
        live_probes is not None
        and sample_probes is not None
        and live_probes != sample_probes
    ):
        return False
    return bool(
        live.get("evidence_mode") == "current_release"
        and str(live.get("evidence_release_commit") or "") == release.commit
        and str(live.get("current_release_commit") or "") == release.commit
        and lineage.get("ok") is True
        and lineage.get("validated") is True
        and lineage.get("compatible") is True
        and same_release_authority(
            ReleaseIdentity(
                commit=str(lineage_current.get("commit") or ""),
                version=str(lineage_current.get("version") or ""),
                receipt_id=str(lineage_current.get("receipt_id") or ""),
                session_id=str(lineage_current.get("session_id") or ""),
            ),
            release,
        )
        and _channel_delivery_lineage_verified(channel, release)
    )


def _replay_summary_consistent(summary: dict[str, Any]) -> bool:
    from eimemory.governance.release.closure_contracts import replay_summary_ok

    return replay_summary_ok(summary)


def run_capability_replay_gate(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
    loop_id: str = LOOP_ID,
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: ScopeRef | dict[str, Any] | None = None,
    at_time: str = "",
    legacy_compatibility: bool = False,
) -> dict[str, Any]:
    """Run the one explicitly selected replay cohort.

    Normal operation resolves cases/capabilities through Registry/Profile.
    The old weak quartet is available only through the compatibility flag; it
    is not used as a fallback for an empty dynamic selection.
    """

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    exact_scope = runtime_scope if runtime_scope is not None else scope_ref
    acceptance = _run_capability_acceptance(
        runtime,
        scope=scope_ref,
        persist=persist,
        case_ids=list(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS) if legacy_compatibility else None,
        profile_key=profile_key,
        capability_scope=capability_scope,
        runtime_scope=exact_scope,
        at_time=at_time,
        legacy_compatibility=legacy_compatibility,
    )
    report = {
        "ok": False,
        "report_type": "capability_replay_gate",
        "scope": asdict(scope_ref),
        "legacy_compatibility": bool(legacy_compatibility),
        "profile_key": str(profile_key or "").strip(),
        "capability_scope": capability_scope,
        "capability_acceptance": acceptance,
        "capability_replay": {},
        "weak_capability_replay": {},
        "replay_gate": {"ok": False, "blocked_reasons": []},
        "blocked_reasons": [],
    }
    if not _acceptance_gate(
        acceptance,
        expected_count=(len(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS) if legacy_compatibility else None),
    ):
        from eimemory.governance.release.closure_contracts import acceptance_failure_details

        report["acceptance_failure"] = acceptance_failure_details(acceptance)
        report["blocked_reasons"] = ["capability_acceptance_failed"]
        return report

    expected_capabilities = (
        list(LEGACY_WEAK_REPLAY_CAPABILITIES)
        if legacy_compatibility
        else _acceptance_capability_ids(acceptance)
    )
    if not expected_capabilities:
        report["blocked_reasons"] = ["dynamic_acceptance_capability_selection_empty"]
        return report
    capability_replay = _build_capability_replay_packs(
        runtime,
        scope=scope_ref,
        capabilities=expected_capabilities if legacy_compatibility else None,
        persist=persist,
        loop_id=loop_id,
        acceptance_execution_id=str(acceptance.get("execution_id") or ""),
        acceptance_probe_ids_by_case={
            str(result.get("case_id") or ""): str(result.get("probe_record_id") or "")
            for result in acceptance.get("results") or []
            if isinstance(result, dict)
        },
        profile_key=profile_key,
        capability_scope=capability_scope,
        runtime_scope=exact_scope,
        at_time=at_time,
        legacy_compatibility=legacy_compatibility,
    )
    replay_gate = _capability_replay_gate(
        capability_replay,
        expected_capabilities=expected_capabilities,
        reason_prefix="legacy_weak_capability_replay" if legacy_compatibility else "capability_replay",
    )
    report["capability_replay"] = capability_replay
    report["weak_capability_replay"] = capability_replay
    report["replay_gate"] = replay_gate
    report["blocked_reasons"] = list(replay_gate.get("blocked_reasons") or [])
    report["ok"] = replay_gate.get("ok") is True
    return report


def run_weak_capability_replay_gate(
    runtime: Any,
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
    persist: bool = True,
    loop_id: str = LOOP_ID,
) -> dict[str, Any]:
    """Compatibility facade for historic release replay evidence only."""

    report = run_capability_replay_gate(
        runtime,
        scope=scope,
        persist=persist,
        loop_id=loop_id,
        legacy_compatibility=True,
    )
    report["report_type"] = "weak_capability_replay_gate"
    return report


def _run_capability_acceptance(
    runtime: Any,
    *,
    scope: ScopeRef,
    persist: bool,
    case_ids: list[str] | None,
    profile_key: str,
    capability_scope: str,
    runtime_scope: ScopeRef | dict[str, Any],
    at_time: str,
    legacy_compatibility: bool,
) -> dict[str, Any]:
    runner = getattr(runtime, "run_capability_acceptance", None)
    if callable(runner):
        return runner(
            scope=scope,
            persist=persist,
            case_ids=case_ids,
            profile_key=profile_key,
            capability_scope=capability_scope,
            runtime_scope=runtime_scope,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
    from eimemory.governance.capability.capability_acceptance import run_capability_acceptance

    return run_capability_acceptance(
        runtime,
        scope=scope,
        persist=persist,
        case_ids=case_ids,
        profile_key=profile_key,
        capability_scope=capability_scope,
        runtime_scope=runtime_scope,
        at_time=at_time,
        legacy_compatibility=legacy_compatibility,
    )


def _build_capability_replay_packs(
    runtime: Any,
    *,
    scope: ScopeRef,
    capabilities: list[str] | None,
    persist: bool,
    loop_id: str,
    acceptance_execution_id: str,
    acceptance_probe_ids_by_case: dict[str, str],
    profile_key: str,
    capability_scope: str,
    runtime_scope: ScopeRef | dict[str, Any],
    at_time: str,
    legacy_compatibility: bool,
) -> dict[str, Any]:
    builder = getattr(runtime, "build_capability_replay_packs", None)
    if callable(builder):
        return builder(
            scope=scope,
            capabilities=capabilities,
            persist=persist,
            loop_id=loop_id,
            acceptance_execution_id=acceptance_execution_id,
            acceptance_probe_ids_by_case=acceptance_probe_ids_by_case,
            profile_key=profile_key,
            capability_scope=capability_scope,
            runtime_scope=runtime_scope,
            at_time=at_time,
            legacy_compatibility=legacy_compatibility,
        )
    from eimemory.governance.capability.capability_replay_packs import build_capability_replay_packs

    return build_capability_replay_packs(
        runtime,
        scope=scope,
        capabilities=capabilities,
        persist=persist,
        loop_id=loop_id,
        acceptance_execution_id=acceptance_execution_id,
        acceptance_probe_ids_by_case=acceptance_probe_ids_by_case,
        profile_key=profile_key,
        capability_scope=capability_scope,
        runtime_scope=runtime_scope,
        at_time=at_time,
        legacy_compatibility=legacy_compatibility,
    )


def _acceptance_capability_ids(report: dict[str, Any]) -> list[str]:
    return sorted(
        {
            str(item.get("capability") or "").strip()
            for item in report.get("results") or []
            if isinstance(item, dict) and str(item.get("capability") or "").strip()
        }
    )


def _initial_closure_report(
    scope: ScopeRef,
    *,
    legacy_compatibility: bool = False,
    profile_key: str = "",
    capability_scope: str = "global",
) -> dict[str, Any]:
    not_run = {"ok": False, "status": "not_run", "reason": "upstream_gate_not_run"}
    return {
        "ok": False,
        "closure_complete": False,
        "data_accumulating": False,
        "blocked_reasons": [],
        "report_type": "l5_closure_rehearsal",
        "scope": asdict(scope),
        "legacy_compatibility": bool(legacy_compatibility),
        "profile_key": str(profile_key or "").strip(),
        "capability_scope": capability_scope,
        "sequence": [],
        "capability_acceptance": dict(not_run),
        "correction_replay": dict(not_run),
        "pre_answer_gate": dict(not_run),
        "outcome_trace": dict(not_run),
        "playbook_record_ids": [],
        "weak_capability_replay": dict(not_run),
        "replay_gate": {**not_run, "blocked_reasons": []},
        "core_capability_acceptance": dict(not_run),
        "core_capability_replay": dict(not_run),
        "core_replay_gate": {**not_run, "blocked_reasons": []},
        "release_lineage": dict(not_run),
        "change_policy": decide_change_policy(event="code_change", closure_complete=False),
        "skill_promotion": {**not_run, "skills": []},
        "skill_call": dict(not_run),
        "rollback": dict(not_run),
        "l5_observation": dict(not_run),
        "capability_dashboard": dict(not_run),
        "l5_readiness": dict(not_run),
        "bootstrap_pending_verification": dict(not_run),
    }


def _blocked_closure(report: dict[str, Any], *reasons: str) -> dict[str, Any]:
    report["ok"] = False
    report["closure_complete"] = False
    report["blocked_reasons"] = list(dict.fromkeys(str(reason) for reason in reasons if str(reason)))
    return report


def _acceptance_gate(report: dict[str, Any], *, expected_count: int | None) -> bool:
    from eimemory.governance.release.closure_contracts import acceptance_report_ok

    expected_ids = None
    if expected_count == len(LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS):
        expected_ids = LEGACY_WEAK_CAPABILITY_ACCEPTANCE_CASE_IDS
    elif expected_count == len(LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS):
        expected_ids = LEGACY_CORE_CAPABILITY_ACCEPTANCE_CASE_IDS
    return acceptance_report_ok(
        report, expected_count=expected_count, expected_case_ids=expected_ids,
    )


def _observation_autonomous_report(
    *,
    replay_report: dict[str, Any],
    skill_promotion: dict[str, Any],
    rollback: dict[str, Any],
) -> dict[str, Any]:
    results = [
        result
        for pack in replay_report.get("packs") or []
        if isinstance(pack, dict)
        for result in pack.get("case_results") or []
        if isinstance(result, dict)
    ]
    pass_count = sum(1 for result in results if str(result.get("verdict") or "").lower() == "pass")
    fail_count = sum(1 for result in results if str(result.get("verdict") or "").lower() == "fail")
    sample_count = pass_count + fail_count
    candidate_ids = [str(value) for value in skill_promotion.get("candidate_ids") or [] if str(value)]
    return {
        "ok": sample_count > 0 and pass_count == sample_count and bool(candidate_ids) and bool(rollback.get("ledger_id")),
        "loop_id": f"{LOOP_ID}_evidence",
        "candidate_id": candidate_ids[0] if candidate_ids else "",
        "candidate_ids": candidate_ids,
        "real_task_replay": {
            "ok": sample_count > 0 and pass_count == sample_count,
            "persisted_record_id": str(replay_report.get("manifest_record_id") or ""),
            "verdict": "pass" if sample_count > 0 and pass_count == sample_count else "fail",
            "sample_count": sample_count,
            "pass_count": pass_count,
            "fail_count": fail_count,
            "pass_rate": round(pass_count / sample_count, 3) if sample_count else 0.0,
        },
        "replay_gate_passed": sample_count > 0 and pass_count == sample_count,
        "blocked_reason": "observation_mode_no_apply",
        "promotion": {
            "ok": True,
            "applied": False,
            "blocked_reason": "observation_mode_no_apply",
        },
        "promotions": [],
    }


def _weak_replay_gate(report: dict[str, Any]) -> dict[str, Any]:
    return _capability_replay_gate(
        report,
        expected_capabilities=LEGACY_WEAK_REPLAY_CAPABILITIES,
        reason_prefix="weak_capability_replay",
    )


def _capability_replay_gate(
    report: dict[str, Any],
    *,
    expected_capabilities: list[str] | None,
    reason_prefix: str,
) -> dict[str, Any]:
    from eimemory.governance.release.closure_contracts import capability_replay_report_gate

    return capability_replay_report_gate(
        report, expected_capabilities=expected_capabilities, reason_prefix=reason_prefix,
    )


def _dynamic_readiness_status(readiness: dict[str, Any]) -> str:
    """Read supported dynamic envelopes without downgrading v4 completion."""

    if (
        not isinstance(readiness, dict)
        or readiness.get("schema_version") not in {"l5_readiness.v3", "l5_readiness.v4"}
        or readiness.get("reader_mode") != "v3"
        or readiness.get("ok") is not True
        or readiness.get("status") != "ready"
        or readiness.get("capability_ready") is not True
        or readiness.get("adapter_ready") is not True
    ):
        return ""
    if readiness.get("schema_version") == "l5_readiness.v4" and (
        readiness.get("product_l5_complete") is not True
        or readiness.get("completion_status") != "complete"
        or readiness.get("gaps")
    ):
        return ""
    if readiness.get("deployment_blocking") is True:
        return ""
    assessment = readiness.get("assessment") if isinstance(readiness.get("assessment"), dict) else {}
    if assessment.get("gaps"):
        return ""
    # L5 needs a demonstrated growth loop, but it does not require a fixed
    # number or name of capabilities.  "evolving" proves one evidenced loop;
    # "compounding" is stronger but not a prerequisite encoded as a cohort.
    if str(readiness.get("loop_maturity") or "") not in {"evolving", "compounding"}:
        return ""
    return "L5"


def _record_successful_task_outcome(runtime: Any, *, scope: ScopeRef, persist: bool) -> dict[str, Any]:
    if not persist:
        return {
            "event": {
                "event_type": "learning_rehearsal",
                "user_phrase": CORRECTION_QUERY,
                "result": "completed",
            },
            "outcome": {"outcome": "good", "status": "completed", "ok": True, "success": True, "verified": True},
            "dry_run": True,
        }
    event = runtime.record_event(
        {
            "source": "manual",
            "event_type": "learning_rehearsal",
            "user_phrase": CORRECTION_QUERY,
            "interpreted_intent": "verify missing-capability correction produces a concrete capability-building path",
            "goal": "open task_success_rate with a verified non-destructive rehearsal",
            "action_path": ["record correction", "check pre-answer gate", "call eiskill", "rollback rehearsal", "recompute dashboard"],
            "verification": "dashboard counts task success, skill reuse, and rollback evidence",
            "result": "completed",
            "confidence": 0.93,
            "rehearsal": True,
        },
        scope=asdict(scope),
    )
    outcome = runtime.record_outcome(
        event["id"],
        {
            "outcome": "good",
            "status": "completed",
            "ok": True,
            "success": True,
            "verified": True,
            "rehearsal": True,
            "reason": "L5 closure rehearsal completed without destructive actions.",
        },
        scope=asdict(scope),
    )
    return {"event": event, "outcome": outcome}


def _rehearsal_skill_id(report: dict[str, Any], *, target_capability: str, playbook_ids: list[str]) -> str:
    """Select only a skill derived from this rehearsal's exact target seeds."""
    expected = set(playbook_ids)
    if not expected or not target_capability:
        return ""
    for skill in report.get("skills") or []:
        if not isinstance(skill, dict):
            continue
        sources = skill.get("source_record_ids")
        if (skill.get("target_capability") == target_capability and isinstance(sources, list)
                and expected.issubset(set(sources))):
            return str(skill.get("skill_id") or "")
    return ""


def _seed_eiskill_playbooks(
    runtime: Any,
    *,
    scope: ScopeRef,
    persist: bool,
    target_capability: str = "",
) -> list[str]:
    if not persist:
        return []
    target_capability = str(target_capability or "").strip()
    if not target_capability:
        raise ValueError("correction_capability_required")
    sop_key = "missing-capability-closure-" + sha256(target_capability.encode("utf-8")).hexdigest()[:16]
    record_ids: list[str] = []
    for index in range(3):
        record = append_learning_record_once(
            runtime,
            kind="learning_playbook",
            title="Missing capability closure SOP",
            summary="When the agent lacks a capability, it must build or route a capability path instead of stopping at refusal.",
            scope=scope,
            loop_id=LOOP_ID,
            step_name=f"seed_eiskill_playbook_{index + 1}",
            semantic_key=stable_semantic_key("missing_capability_closure", target_capability, index),
            authority_tier="L0",
            status="active",
            content={
                "report_type": "sop_draft",
                "sop_key": sop_key,
                "target_capability": str(target_capability or ""),
                "steps": [
                    "state the missing capability precisely",
                    "create the smallest implementation or routing plan",
                    "attach replay or evaluation evidence",
                    "define rollback or quarantine boundary",
                    "report the verified next action",
                ],
                "trigger_conditions": ["user correction says do not stop at inability", "missing capability blocks task completion"],
                "action": "convert missing capability into a concrete implementation, replay, and gate plan",
                "verification": "pre-answer gate matches and dashboard records success evidence",
                "rollback": "disable this eiskill registry entry or quarantine the related intent pattern if replay fails",
                "replay_passed": True,
                "source_repeat": index + 1,
            },
            meta={
                "report_type": "sop_draft",
                "sop_key": sop_key,
                "target_capability": str(target_capability or ""),
                "replay_passed": True,
            },
            source="eimemory.closure_rehearsal",
        )
        record_ids.append(record.record_id)
    return record_ids


def _run_non_destructive_rollback(runtime: Any, *, scope: ScopeRef, persist: bool) -> dict[str, Any]:
    pattern_id = f"closure-rehearsal-rollback-{_scope_hash(scope)}"
    if persist:
        runtime.upsert_intent_pattern(
            {
                "id": pattern_id,
                "pattern": "closure rehearsal rollback sample",
                "default_event_type": "repair",
                "interpreted_intent": "non-destructive rollback rehearsal for L5 readiness",
                "confidence": 0.91,
                "status": "active",
            },
            scope=asdict(scope),
        )
        return runtime.rollback_intent_pattern(
            pattern_id,
            scope=asdict(scope),
            reason="non-destructive L5 rollback rehearsal",
            auto=False,
        )
    return {"ok": True, "status": "dry_run", "pattern_id": pattern_id}


def _scope_hash(scope: ScopeRef) -> str:
    payload = "|".join([scope.tenant_id, scope.agent_id, scope.workspace_id, scope.user_id])
    return sha256(payload.encode("utf-8")).hexdigest()[:12]
