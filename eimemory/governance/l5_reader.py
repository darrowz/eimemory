"""Reversible reader selection for legacy L5 and dynamic L5 v3.

The selection is deliberately a deployment policy rather than a version,
machine, or health check.  ``legacy`` remains available only for the declared
rollback window, ``shadow`` preserves the legacy response while attaching a
comparison, and ``v3`` makes the profile-backed four-axis assessment the
primary L5 result.  No mode synthesizes a capability taxonomy.
"""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
import json
import os
from typing import Any

from eimemory.capabilities.profile_bootstrap import DEFAULT_L5_PROFILE_KEY
from eimemory.core.clock import now_iso
from eimemory.models.records import ScopeRef


L5_READER_SCHEMA = "l5.reader.v4"
L5_V3_READER_SCHEMA = "l5.reader.v3"
_READER_MODES = frozenset({"legacy", "shadow", "v3"})


class L5ReaderError(ValueError):
    """A requested L5 reader mode is malformed or lacks its profile."""


def resolve_l5_reader_mode(mode: str = "") -> str:
    """Resolve a bounded, explicit deployment reader mode.

    V3 is the production default.  ``legacy`` is an explicit rollback-window
    selection only; when the mandatory profile is not configured, v3 blocks
    visibly instead of silently reviving a compiled capability taxonomy.
    """

    candidate = str(mode or os.environ.get("EIMEMORY_L5_READER_MODE") or "v3").strip().lower()
    if candidate not in _READER_MODES:
        raise L5ReaderError("l5 reader mode must be legacy, shadow, or v3")
    return candidate


def build_l5_effective_report(
    runtime: Any,
    *,
    scope: Mapping[str, Any] | ScopeRef | None = None,
    persist: bool = False,
    limit: int = 500,
    loop_id: str = "l5_readiness",
    repo_root: str = "/dev-project/eimemory",
    reader_mode: str = "",
    profile_key: str = "",
    capability_scope: str = "global",
    runtime_scope: Mapping[str, Any] | ScopeRef | None = None,
    at_time: str = "",
    catalog: Any | None = None,
) -> dict[str, Any]:
    """Read L5 through a policy-selected primary reader.

    This function is the sole runtime cutover seam.  It never writes a reader
    selection, never converts legacy scores into v3 observations, and never
    lets a missing profile silently fall back when v3 was selected.
    """

    mode = resolve_l5_reader_mode(reader_mode)
    profile = str(
        profile_key
        or os.environ.get("EIMEMORY_L5_V3_PROFILE")
        or DEFAULT_L5_PROFILE_KEY
    ).strip()
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    # ``scope`` remains the evidence/report scope.  Dynamic Profile resolution
    # can intentionally use a different exact runtime scope, but it must be
    # supplied by the caller rather than inferred from a machine or release.
    selector_scope = runtime_scope if runtime_scope is not None else scope_ref
    if mode == "legacy":
        return _legacy_report(
            runtime,
            scope=scope_ref,
            persist=persist,
            limit=limit,
            loop_id=loop_id,
            repo_root=repo_root,
            mode=mode,
            profile_key=profile,
            capability_scope=capability_scope,
            runtime_scope=runtime_scope,
            at_time=at_time,
            catalog=catalog,
        )
    if not profile:
        return {
            "schema": L5_READER_SCHEMA,
            "schema_version": "l5_readiness.v3",
            "report_type": "l5_readiness_report",
            "reader_mode": mode,
            "ok": False,
            "status": "blocked",
            "reason": "l5_v3_profile_not_configured",
            "capability_scope": capability_scope,
            "scope": {
                "tenant_id": scope_ref.tenant_id,
                "agent_id": scope_ref.agent_id,
                "workspace_id": scope_ref.workspace_id,
                "user_id": scope_ref.user_id,
            },
        }
    if mode == "v3":
        from eimemory.governance.l5_assessment_v3 import build_l5_assessment_v3

        assessment = build_l5_assessment_v3(
            runtime,
            profile_key=profile,
            scope=selector_scope,
            capability_scope=capability_scope,
            persist=persist,
            at_time=at_time,
            max_candidates=min(499, max(1, int(limit))),
            observation_limit=min(500, max(1, int(limit))),
        )
        return _v3_readiness_envelope(
            assessment,
            scope=scope_ref,
            profile_key=profile,
            capability_scope=capability_scope,
            runtime=runtime,
            runtime_scope=selector_scope,
            at_time=at_time,
            catalog=catalog,
            repo_root=repo_root,
        )
    from eimemory.governance.l5_shadow import build_l5_v3_shadow

    shadow = build_l5_v3_shadow(
        runtime,
        profile_key=profile,
        scope=scope_ref,
        runtime_scope=runtime_scope,
        capability_scope=capability_scope,
        persist=persist,
        at_time=at_time,
        max_candidates=min(499, max(1, int(limit))),
        observation_limit=min(500, max(1, int(limit))),
        repo_root=repo_root,
        catalog=catalog,
    )
    legacy = dict(shadow.get("v2") or {})
    legacy["reader_mode"] = "shadow"
    legacy["reader_schema"] = L5_READER_SCHEMA
    legacy["l5_v3_shadow"] = shadow
    legacy["l5_v3_profile_key"] = profile
    legacy["l5_v3_capability_scope"] = capability_scope
    return legacy


def _legacy_report(
    runtime: Any,
    *,
    scope: ScopeRef,
    persist: bool,
    limit: int,
    loop_id: str,
    repo_root: str,
    mode: str,
    profile_key: str,
    capability_scope: str,
    runtime_scope: Mapping[str, Any] | ScopeRef | None,
    at_time: str,
    catalog: Any | None,
) -> dict[str, Any]:
    from eimemory.governance.l5_readiness import build_l5_readiness_report

    report = build_l5_readiness_report(
        runtime,
        scope=scope,
        persist=persist,
        limit=limit,
        loop_id=loop_id,
        repo_root=repo_root,
        profile_key=profile_key,
        capability_scope=capability_scope,
        runtime_scope=runtime_scope,
        at_time=at_time,
        catalog=catalog,
        # The reader's rollback path is the one explicit consumer allowed to
        # reproduce the frozen v2 taxonomy.  The readiness implementation now
        # defaults to dynamic profile selection, so this intent must be
        # carried explicitly rather than inferred from a reader mode string.
        legacy_compatibility=True,
    )
    result = dict(report)
    result["reader_mode"] = mode
    result["reader_schema"] = L5_READER_SCHEMA
    return result


def _v3_readiness_envelope(
    assessment: Mapping[str, Any],
    *,
    scope: ScopeRef,
    profile_key: str,
    capability_scope: str,
    runtime: Any | None = None,
    runtime_scope: Mapping[str, Any] | ScopeRef | None = None,
    at_time: str = "",
    catalog: Any | None = None,
    repo_root: str = "/dev-project/eimemory",
) -> dict[str, Any]:
    """Expose a bounded familiar envelope without flattening v3 axes."""

    loop_maturity = str(assessment.get("loop_maturity") or "observing")
    capability_ready = bool(assessment.get("ok") is True)
    deployment = assessment.get("deployment_assurance") if isinstance(assessment.get("deployment_assurance"), Mapping) else {}
    adapters = assessment.get("adapter_readiness") if isinstance(assessment.get("adapter_readiness"), Mapping) else {}
    adapter_ready = bool(adapters) and all(str(value) == "ready" for value in adapters.values())
    # Deployment remains an independent assurance axis.  No explicit
    # deployment-dependent evidence is a neutral, visible state (``None``),
    # not a fabricated green deployment result and not a reason to reset the
    # profile-backed cognitive result.  A declared requirement must, however,
    # pass its immutable commit/receipt/session verification.
    deployment_present = bool(deployment)
    deployment_required = deployment.get("required") is True
    deployment_blocking = deployment.get("blocking") is True or not deployment_present
    deployment_ready = (
        deployment.get("ok") is True
        if deployment_required
        else (None if deployment_present else False)
    )
    deployment_gate_ok = bool(
        not deployment_blocking
        and (not deployment_required or deployment_ready is True)
    )
    # Do not retain an upstream ``ready`` label after this reader has applied
    # the independent adapter/deployment gates.  In particular, a declared
    # deployment requirement that fails must be visible as a blocked reader
    # result even though it deliberately does not rewrite the cognitive axis.
    upstream_status = str(assessment.get("status") or "")
    if upstream_status == "blocked":
        status = "blocked"
    elif capability_ready and adapter_ready and deployment_gate_ok:
        status = "ready"
    elif deployment_blocking:
        status = "blocked"
    elif not adapter_ready:
        status = "degraded"
    else:
        status = upstream_status if upstream_status and upstream_status != "ready" else "degraded"
    envelope = {
        "schema": L5_V3_READER_SCHEMA if runtime is None else L5_READER_SCHEMA,
        "schema_version": "l5_readiness.v3" if runtime is None else "l5_readiness.v4",
        "report_type": "l5_readiness_report",
        "reader_mode": "v3",
        "ok": capability_ready and adapter_ready and deployment_gate_ok,
        "status": status,
        "profile_key": profile_key,
        "capability_scope": capability_scope,
        "scope": {
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "workspace_id": scope.workspace_id,
            "user_id": scope.user_id,
        },
        "loop_maturity": loop_maturity,
        "capability_ready": capability_ready,
        "adapter_ready": adapter_ready,
        "deployment_ready": deployment_ready,
        "deployment_required": deployment_required,
        "deployment_blocking": deployment_blocking,
        "assessment": dict(assessment),
        "gaps": list(assessment.get("gaps") or ()),
    }
    if runtime is None:
        # This private compatibility seam is still used by v3 contract tests
        # and by callers that explicitly ask for the raw axis envelope.  The
        # runtime-facing reader below is the only path allowed to claim full
        # product completion.
        return envelope

    checked_at = str(at_time or now_iso())
    provider, transaction, lineage = _code_evolution_evidence(
        runtime,
        runtime_scope=runtime_scope or scope,
        capability_scope=capability_scope,
        checked_at=checked_at,
        repo_root=repo_root,
        catalog=catalog,
    )
    from eimemory.governance.l5_product_completion import build_product_completion

    control_assessment = dict(assessment)
    control_assessment["ok"] = envelope["ok"]
    control_assessment["status"] = envelope["status"]
    completion = build_product_completion(
        control_assessment,
        provider=provider,
        transaction=transaction,
        current_lineage=lineage,
    )
    envelope.update(completion)
    envelope["assessment"] = dict(assessment)
    envelope["provider_evidence"] = {
        key: value
        for key, value in provider.items()
        if key not in {"provider", "resolution"}
    }
    envelope["transaction_evidence"] = dict(transaction)
    envelope["current_lineage"] = dict(lineage)
    envelope["completion_evidence_refs"] = _completion_evidence_refs(
        provider=provider,
        transaction=transaction,
        lineage=lineage,
    )
    return envelope


def _completion_evidence_refs(
    *,
    provider: Mapping[str, Any],
    transaction: Mapping[str, Any],
    lineage: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Expose only references backed by the corresponding current evidence."""

    refs = {"provider": [], "catalog": [], "transaction": [], "lineage": []}
    if provider.get("provider_ready") is True:
        refs["provider"] = [
            str(value)
            for value in (
                provider.get("binding_id"),
                provider.get("advertisement", {}).get("entity_id")
                if isinstance(provider.get("advertisement"), Mapping)
                else "",
                provider.get("advertisement_digest"),
                provider.get("implementation_digest"),
            )
            if str(value or "")
        ]
    if provider.get("catalog_ready") is True and str(provider.get("catalog_snapshot_digest") or ""):
        refs["catalog"] = [str(provider["catalog_snapshot_digest"])]
    if transaction.get("transaction_id") and transaction.get("terminal_receipt_digest"):
        refs["transaction"] = [
            str(transaction["transaction_id"]),
            str(transaction["terminal_receipt_digest"]),
        ]
    if lineage.get("ok") is True and lineage.get("compatible") is True:
        refs["lineage"] = [
            str(value)
            for value in (lineage.get("record_id"), lineage.get("lineage_digest"))
            if str(value or "")
        ]
    return refs


def _code_evolution_evidence(
    runtime: Any,
    *,
    runtime_scope: Mapping[str, Any] | ScopeRef,
    capability_scope: str,
    checked_at: str,
    repo_root: str,
    catalog: Any | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read v2 provider, sealed catalog, ledger, and lineage evidence only."""

    from eimemory.adapters.hermes.code_implementation import (
        BINDING_ID,
        CAPABILITY_ID,
        OPERATION,
        PROVIDER_INSTANCE_ID,
        PROVIDER_KIND,
        REVISION_ID,
        resolve_code_implementation_provider,
    )

    provider = resolve_code_implementation_provider(
        runtime,
        runtime_scope=(
            runtime_scope
            if isinstance(runtime_scope, Mapping)
            else {
                "tenant_id": runtime_scope.tenant_id,
                "agent_id": runtime_scope.agent_id,
                "workspace_id": runtime_scope.workspace_id,
                "user_id": runtime_scope.user_id,
            }
        ),
        capability_scope=capability_scope,
        checked_at=checked_at,
        # Full-product readiness is a current operational claim, so registry
        # coordinates and a fresh advertisement are necessary but not
        # sufficient; the fixed socket must attest live at assessment time.
        probe=True,
    )
    provider = {
        **provider,
        "capability_id": CAPABILITY_ID,
        "revision_id": REVISION_ID,
        "binding_id": BINDING_ID,
        "provider_kind": PROVIDER_KIND,
        "provider_instance_id": PROVIDER_INSTANCE_ID,
        "operation": OPERATION,
    }
    active_catalog = catalog if catalog is not None else getattr(runtime, "capability_catalog", None)
    catalog_structural = False
    catalog_digest = ""
    catalog_incubation_digest = ""
    catalog_passes = 0
    if active_catalog is not None:
        try:
            from eimemory.evaluation.hongtu_code_implementation import (
                CATALOG_CASE_ID,
                CATALOG_EXECUTOR_ID,
                validate_code_implementation_catalog_receipt,
            )

            case = active_catalog.get_case(CATALOG_CASE_ID)
            executor = active_catalog.describe_executor(CATALOG_EXECUTOR_ID)
            catalog_structural = bool(active_catalog.sealed and case is not None and executor is not None)
            catalog_digest = sha256(
                json.dumps(
                    {
                        "case": case.to_artifact() if case is not None else {},
                        "executor": executor or {},
                        "sealed": bool(getattr(active_catalog, "sealed", False)),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            # The two-pass requirement is durable lifecycle provenance from
            # the existing capability authority.  A process-local attribute
            # can be stale, forged by a caller, or disappear on restart, so it
            # is never accepted as product evidence.
            capabilities = getattr(runtime, "capabilities", None)
            list_lifecycle_events = getattr(capabilities, "list_lifecycle_events", None)
            if catalog_structural and callable(list_lifecycle_events):
                if isinstance(runtime_scope, Mapping):
                    lifecycle_scope = dict(runtime_scope)
                else:
                    lifecycle_scope = {
                        "tenant_id": runtime_scope.tenant_id,
                        "agent_id": runtime_scope.agent_id,
                        "workspace_id": runtime_scope.workspace_id,
                        "user_id": runtime_scope.user_id,
                    }
                lifecycle_events = list_lifecycle_events(
                    entity_type="definition",
                    entity_id=CAPABILITY_ID,
                    runtime_scope=lifecycle_scope,
                    capability_scope=capability_scope,
                    limit=32,
                )
                eligible_events = []
                for event in lifecycle_events:
                    if not isinstance(event, Mapping) or str(event.get("status") or "") != "active":
                        continue
                    provenance = event.get("provenance") if isinstance(event.get("provenance"), Mapping) else {}
                    raw_passes = provenance.get("preflight_passes")
                    if isinstance(raw_passes, bool):
                        continue
                    try:
                        passes = int(raw_passes)
                    except (TypeError, ValueError):
                        continue
                    case_ids = {str(value) for value in provenance.get("case_ids") or ()}
                    binding_ids = {str(value) for value in provenance.get("binding_ids") or ()}
                    execution_digests = [
                        str(value or "")
                        for value in provenance.get("preflight_execution_digests") or ()
                    ]
                    receipt_digests = [
                        str(value or "")
                        for value in provenance.get("provider_evaluation_receipt_digests") or ()
                    ]
                    provider_receipts = provenance.get("provider_evaluation_receipts")
                    receipts_valid = bool(
                        isinstance(provider_receipts, list)
                        and len(provider_receipts) == passes
                        and len(receipt_digests) == passes
                        and len(execution_digests) == passes
                        and len(set(receipt_digests)) == passes
                        and len(set(execution_digests)) == passes
                    )
                    if receipts_valid:
                        try:
                            for receipt, receipt_digest in zip(
                                provider_receipts,
                                receipt_digests,
                                strict=True,
                            ):
                                validate_code_implementation_catalog_receipt(
                                    receipt,
                                    receipt_digest=receipt_digest,
                                )
                            if any(
                                len(value) != 64
                                or any(char not in "0123456789abcdef" for char in value)
                                for value in execution_digests
                            ):
                                receipts_valid = False
                        except (TypeError, ValueError):
                            receipts_valid = False
                    if (
                        provenance.get("source") == "eimemory.capability_incubation"
                        and provenance.get("schema") == "capability.incubation.v1"
                        and passes >= 2
                        and CATALOG_CASE_ID in case_ids
                        and BINDING_ID in binding_ids
                        and receipts_valid
                    ):
                        eligible_events.append((passes, int(event.get("state_version") or 0), event))
                if eligible_events:
                    passes, _state_version, event = max(eligible_events, key=lambda item: item[:2])
                    catalog_passes = passes
                    catalog_incubation_digest = str(event.get("state_digest") or "")
        except Exception:
            catalog_structural = False
    provider["catalog_ready"] = catalog_structural and catalog_passes >= 2
    provider["catalog_structural_digest"] = catalog_digest
    provider["catalog_snapshot_digest"] = (
        sha256(
            json.dumps(
                {
                    "catalog_structural_digest": catalog_digest,
                    "incubation_state_digest": catalog_incubation_digest,
                    "catalog_passes": catalog_passes,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if catalog_structural and catalog_incubation_digest
        else catalog_digest
    )
    provider["catalog_incubation_state_digest"] = catalog_incubation_digest
    provider["catalog_passes"] = catalog_passes

    transaction: dict[str, Any] = {
        "qualifying_terminal_outcome": None,
        "manual_bootstrap": False,
        "origin": "",
        "known_before_detection": False,
        "prior_user_reported": False,
        "observation_valid": False,
        "quarantined": False,
        "evidence_verified": False,
    }
    try:
        from eimemory.storage.code_evolution_store import CodeEvolutionStore

        ledger = CodeEvolutionStore(runtime.store)
        rows = ledger.list_transactions(limit=100)
        if isinstance(runtime_scope, Mapping):
            requested_scope = tuple(str(runtime_scope.get(field) or "") for field in ("tenant_id", "agent_id", "workspace_id", "user_id"))
        else:
            requested_scope = tuple(
                str(getattr(runtime_scope, field, "") or "")
                for field in ("tenant_id", "agent_id", "workspace_id", "user_id")
            )
        scoped = [
            row for row in rows
            if str(row.get("repository_root") or "") == str(repo_root)
            and str(row.get("repository_ref") or "") in {"master", "refs/heads/master"}
            and tuple(str(row.get(field) or "") for field in ("tenant_id", "agent_id", "workspace_id", "user_id")) == requested_scope
        ]
        active, latest = _select_code_evolution_rows(ledger, scoped)
        if active is not None:
            transaction.update({
                "transaction_id": str(active.get("transaction_id") or ""),
                "current_state": str(active.get("current_state") or ""),
                "origin": str(active.get("origin") or ""),
                "detector": str(active.get("detector") or ""),
                "profile_key": str(active.get("profile_key") or ""),
                # SQLite stores booleans as 0/1. Preserve the actual source
                # facts in the active projection; absent facts are unknown,
                # never evidence that the incident was previously unknown.
                **{
                    field: bool(active[field])
                    if type(active.get(field)) in {bool, int} and active[field] in (0, 1)
                    else None
                    for field in ("known_before_detection", "prior_user_reported", "manual_bootstrap")
                },
                "base_commit": str(active.get("base_commit") or ""),
                "candidate_commit": str(active.get("candidate_commit") or ""),
                "deployed_commit": str(active.get("deployed_commit") or ""),
                "quarantined": any(
                    str(row.get("current_state") or "") == "RECOVERY_QUARANTINED"
                    for row in scoped
                    if not bool(row.get("terminal"))
                ),
                "nonterminal": True,
            })
        if latest is not None:
            receipt = ledger.get_terminal_receipt(str(latest.get("transaction_id") or "")) or {}
            receipt_payload = receipt.get("payload") if isinstance(receipt.get("payload"), Mapping) else {}
            evidence_error = _qualifying_ledger_evidence_error(
                ledger,
                transaction_row=latest,
                terminal_receipt=receipt,
                current_provider=provider,
                capabilities=getattr(runtime, "capabilities", None),
                runtime_scope=runtime_scope,
                capability_scope=capability_scope,
            )
            transaction.update({
                "transaction_id": latest.get("transaction_id"),
                "origin": latest.get("origin"),
                "detector": latest.get("detector"),
                "known_before_detection": bool(latest.get("known_before_detection")),
                "prior_user_reported": bool(latest.get("prior_user_reported")),
                "manual_bootstrap": bool(latest.get("manual_bootstrap")),
                "qualifying_terminal_outcome": receipt.get("outcome") or latest.get("current_state", "").lower(),
                # Qualification facts come only from the append-only terminal
                # receipt, never from the formerly mutable transaction payload.
                "observation_valid": receipt_payload.get("observation_valid") is True,
                "candidate_pushed_and_deployed": receipt_payload.get("candidate_pushed_and_deployed") is True,
                "rollback_executed": receipt_payload.get("rollback_executed") is True,
                "terminal_receipt_digest": str(receipt.get("receipt_digest") or ""),
                "base_commit": str(latest.get("base_commit") or ""),
                "candidate_commit": str(latest.get("candidate_commit") or ""),
                "prior_commit": str(latest.get("prior_commit") or ""),
                "deployed_commit": str(latest.get("deployed_commit") or ""),
                "quarantined": receipt.get("outcome") == "recovery_quarantined",
                "evidence_verified": not evidence_error,
                "evidence_error": evidence_error,
            })
    except Exception as exc:
        transaction["ledger_error"] = type(exc).__name__

    lineage = getattr(runtime, "code_evolution_current_lineage", None)
    if not isinstance(lineage, Mapping):
        # The release-lineage ledger is the existing authority for current
        # release identity.  Do not let a missing convenience attribute turn
        # into a green completion claim, but do resolve a validated lineage
        # record when the runtime has the ordinary receipt and catalog
        # authorities available.
        try:
            from eimemory.governance.evidence_contract import current_release_identity
            from eimemory.governance.release_lineage import current_release_lineage

            scope_ref = runtime_scope if isinstance(runtime_scope, ScopeRef) else ScopeRef.from_dict(dict(runtime_scope))
            current_release = current_release_identity(runtime, scope_ref)
            if current_release is None:
                lineage = {
                    "ok": False,
                    "compatible": False,
                    "reason": "current_release_identity_unavailable",
                }
            else:
                lineage = current_release_lineage(
                    runtime,
                    scope=scope_ref,
                    current_release=current_release,
                    repo_root=repo_root,
                    catalog=catalog,
                    legacy_compatibility=False,
                )
        except Exception as exc:
            lineage = {
                "ok": False,
                "compatible": False,
                "reason": f"current_code_evolution_lineage_unavailable:{type(exc).__name__}",
            }
    lineage_result = dict(lineage)
    if transaction.get("evidence_verified") is True:
        current_release = lineage_result.get("current_release") if isinstance(lineage_result.get("current_release"), Mapping) else {}
        lineage_commit = str(current_release.get("commit") or "")
        outcome = str(transaction.get("qualifying_terminal_outcome") or "")
        expected_commit = str(
            transaction.get("prior_commit")
            if outcome == "rolled_back_healthy"
            else transaction.get("deployed_commit")
            or transaction.get("candidate_commit")
            or ""
        )
        if (
            lineage_result.get("ok") is not True
            or lineage_result.get("compatible") is not True
            or not lineage_commit
            or lineage_commit != expected_commit
        ):
            transaction["evidence_verified"] = False
            transaction["evidence_error"] = "terminal_transaction_lineage_mismatch"
    return provider, transaction, lineage_result


def _select_code_evolution_rows(
    ledger: Any,
    scoped_rows: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Select the current transaction or the latest unresolved terminal."""

    nonterminal = [row for row in scoped_rows if not bool(row.get("terminal"))]
    if nonterminal:
        # ``list_transactions`` is ordered by updated_at descending.
        return nonterminal[0], None
    for row in scoped_rows:
        if not bool(row.get("terminal")):
            continue
        if str(row.get("current_state") or "") != "RECOVERY_QUARANTINED":
            return None, row
        if ledger.get_quarantine_resolution(str(row.get("transaction_id") or "")) is None:
            return None, row
    return None, None


def _qualifying_ledger_evidence_error(
    ledger: Any,
    *,
    transaction_row: Mapping[str, Any],
    terminal_receipt: Mapping[str, Any],
    current_provider: Mapping[str, Any],
    capabilities: Any,
    runtime_scope: Mapping[str, Any] | ScopeRef,
    capability_scope: str,
) -> str:
    """Revalidate one terminal candidate from append-only authorities only."""

    transaction_id = str(transaction_row.get("transaction_id") or "")
    outcome = str(terminal_receipt.get("outcome") or "")
    expected_state = {
        "succeeded_sedimented": "SUCCEEDED_SEDIMENTED",
        "rolled_back_healthy": "ROLLED_BACK_HEALTHY",
    }.get(outcome)
    if expected_state is None or transaction_row.get("current_state") != expected_state:
        return "terminal_state_outcome_mismatch"
    if (
        terminal_receipt.get("transaction_id") != transaction_id
        or transaction_row.get("terminal_receipt_digest") != terminal_receipt.get("receipt_digest")
    ):
        return "terminal_receipt_identity_mismatch"
    exact_provider = {
        "capability_id": "code.implementation",
        "revision_id": "code.implementation:v9",
        "binding_id": "binding.hermes.code-implementation:v9",
        "provider_kind": "hermes",
        "provider_instance_id": "hermes.eimemory.code-implementation.production",
    }
    for field, expected in exact_provider.items():
        if str(transaction_row.get(field) or "") != expected:
            return f"terminal_{field}_mismatch"
    if str(transaction_row.get("implementation_digest") or "") != str(current_provider.get("implementation_digest") or ""):
        return "terminal_provider_digest_mismatch"
    advertisement_error = _historical_advertisement_evidence_error(
        capabilities,
        transaction_row=transaction_row,
        runtime_scope=runtime_scope,
        capability_scope=capability_scope,
    )
    if advertisement_error:
        return advertisement_error
    if str(transaction_row.get("catalog_case_id") or "") != "hongtu_code_implementation_v2":
        return "terminal_catalog_case_mismatch"
    for field in (
        "incident_digest",
        "implementation_digest",
        "advertisement_digest",
        "catalog_snapshot_digest",
        "proposal_digest",
        "patch_digest",
        "candidate_tree_digest",
        "policy_digest",
        "authorization_digest",
    ):
        value = str(transaction_row.get(field) or "").strip().lower()
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            return f"terminal_{field}_invalid"
    for field in ("base_commit", "candidate_commit"):
        value = str(transaction_row.get(field) or "").strip().lower()
        if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
            return f"terminal_{field}_invalid"
    receipt_payload = terminal_receipt.get("payload") if isinstance(terminal_receipt.get("payload"), Mapping) else {}
    if (
        terminal_receipt.get("incident_digest") != transaction_row.get("incident_digest")
        or terminal_receipt.get("provider_digest") != transaction_row.get("implementation_digest")
        or terminal_receipt.get("policy_digest") != transaction_row.get("policy_digest")
        or terminal_receipt.get("authorization_digest") != transaction_row.get("authorization_digest")
        or terminal_receipt.get("base_commit") != transaction_row.get("base_commit")
        or terminal_receipt.get("candidate_commit") != transaction_row.get("candidate_commit")
    ):
        return "terminal_receipt_coordinates_mismatch"
    deployment_digest = str(receipt_payload.get("deployment_receipt_digest") or "").strip().lower()
    observation_digest = str(terminal_receipt.get("observation_digest") or "").strip().lower()
    if any(
        len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
        for value in (deployment_digest, observation_digest)
    ):
        return "terminal_deployment_or_observation_digest_invalid"
    consumption = ledger.get_policy_consumption(str(transaction_row.get("policy_digest") or ""))
    if (
        not isinstance(consumption, Mapping)
        or consumption.get("transaction_id") != transaction_id
        or consumption.get("authorization_receipt_digest") != transaction_row.get("authorization_digest")
    ):
        return "terminal_policy_consumption_invalid"
    receipts = ledger.list_verification_receipts(transaction_id)
    if {str(row.get("verification_kind") or "") for row in receipts} != {"focused", "regression", "full_suite"}:
        return "terminal_verification_receipts_incomplete"
    for receipt in receipts:
        if (
            receipt.get("result") != "pass"
            or int(receipt.get("exit_status", 1)) != 0
            or receipt.get("base_commit") != transaction_row.get("base_commit")
            or receipt.get("patch_digest") != transaction_row.get("patch_digest")
            or receipt.get("candidate_tree_digest") != transaction_row.get("candidate_tree_digest")
        ):
            return "terminal_verification_receipt_mismatch"
    events = ledger.list_step_events(transaction_id, limit=2_000)
    reached = {str(event.get("to_state") or "") for event in events}
    required_states = {
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
    }
    if not required_states.issubset(reached):
        return "terminal_state_event_chain_incomplete"
    if outcome == "rolled_back_healthy" and not {"ROLLBACK_INTENT", "ROLLED_BACK_HEALTHY"}.issubset(reached):
        return "terminal_rollback_event_chain_incomplete"
    if receipt_payload.get("candidate_pushed_and_deployed") is not True:
        return "terminal_candidate_deployment_unproven"
    if receipt_payload.get("observation_valid") is not True:
        return "terminal_observation_unproven"
    if outcome == "rolled_back_healthy" and receipt_payload.get("rollback_executed") is not True:
        return "terminal_rollback_execution_unproven"
    return ""


def _historical_advertisement_evidence_error(
    capabilities: Any,
    *,
    transaction_row: Mapping[str, Any],
    runtime_scope: Mapping[str, Any] | ScopeRef,
    capability_scope: str,
) -> str:
    """Revalidate the transaction-time ad while current liveness refreshes.

    Refresh advertisements are immutable and overlap by design.  Requiring a
    terminal transaction's original digest to equal the latest live digest
    would make a valid 48-hour observation impossible after the first timer
    tick.  The transaction keeps its exact historical coordinate; this helper
    resolves that coordinate from the durable capability authority and checks
    every provider/binding/implementation field.  The provider resolver still
    independently requires a current fresh advertisement and live health.
    """

    advertisement_id = str(transaction_row.get("advertisement_id") or "")
    advertisement_digest = str(transaction_row.get("advertisement_digest") or "").strip().lower()
    if not advertisement_id:
        return "terminal_advertisement_id_missing"
    context = getattr(capabilities, "advertisement_context", None)
    if not callable(context):
        return "terminal_advertisement_authority_unavailable"
    if isinstance(runtime_scope, Mapping):
        scope = dict(runtime_scope)
    else:
        scope = {
            "tenant_id": runtime_scope.tenant_id,
            "agent_id": runtime_scope.agent_id,
            "workspace_id": runtime_scope.workspace_id,
            "user_id": runtime_scope.user_id,
        }
    try:
        advertisement = context(
            advertisement_id,
            runtime_scope=scope,
            capability_scope=capability_scope,
        )
    except (RuntimeError, TypeError, ValueError):
        return "terminal_advertisement_authority_unavailable"
    if not isinstance(advertisement, Mapping):
        return "terminal_advertisement_unavailable"
    if (
        advertisement.get("entity_id") != advertisement_id
        or str(advertisement.get("entity_digest") or "").strip().lower()
        != advertisement_digest
        or advertisement.get("status") != "active"
    ):
        return "terminal_advertisement_identity_mismatch"
    descriptor = advertisement.get("descriptor")
    if not isinstance(descriptor, Mapping):
        return "terminal_advertisement_descriptor_invalid"
    environment = descriptor.get("environment_fingerprint")
    if not isinstance(environment, Mapping):
        return "terminal_advertisement_descriptor_invalid"
    exact = {
        "binding_id": "binding.hermes.code-implementation:v9",
        "capability_revision_id": "code.implementation:v9",
        "provider_kind": "hermes",
        "provider_instance_id": "hermes.eimemory.code-implementation.production",
        "side_effect_class": "network",
    }
    if any(str(descriptor.get(field) or "") != expected for field, expected in exact.items()):
        return "terminal_advertisement_provider_mismatch"
    implementation_digest = str(transaction_row.get("implementation_digest") or "")
    if (
        str(environment.get("implementation_digest") or "") != implementation_digest
        or "propose_patch_v2" not in tuple(descriptor.get("operations") or ())
    ):
        return "terminal_advertisement_provider_mismatch"
    return ""


__all__ = [
    "L5_READER_SCHEMA",
    "L5_V3_READER_SCHEMA",
    "L5ReaderError",
    "build_l5_effective_report",
    "resolve_l5_reader_mode",
]
