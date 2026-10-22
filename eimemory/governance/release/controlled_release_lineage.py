"""Bind independently verified controlled business evidence to release lineage.

The signed acceptance remains a controlled recall result.  It grants neither
natural-query certification nor autonomous code-evolution credit.  Every other
domain keeps its existing native validator and exact physical references.
"""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from typing import Any

from eimemory.contracts.release_identity import release_identity_payload
from eimemory.governance.release.evidence_contract import ReleaseIdentity
from eimemory.models.records import ScopeRef


CONTROLLED_SOURCE = 'eimemory.evaluation.controlled_production_acceptance'


def controlled_recall_gate_error(runtime: Any, *, scope: ScopeRef,
                                 release: ReleaseIdentity, references: list[str]) -> str:
    """Re-run HMAC, latest-attempt, physical version and complete-search checks."""
    from eimemory.evaluation.controlled_production_acceptance import (
        verify_controlled_production_acceptance,
    )
    if len(references) != 1 or not isinstance(references[0], str) or not references[0]:
        return 'exact_controlled_recall_reference_required'
    checked = verify_controlled_production_acceptance(
        runtime, scope=asdict(scope), release=release, record_id=references[0], limit=500,
    )
    if not (
        checked.get('ok') is True and checked.get('accepted') is True
        and checked.get('persisted') is True and checked.get('status') == 'accepted'
        and checked.get('schema') in {'controlled_production_acceptance.v1', 'controlled_production_acceptance.v2'}
        and checked.get('mode') == 'controlled_existing_memory'
        and checked.get('evidence_class') == 'operator_controlled_existing_memory'
        and checked.get('record_id') == references[0]
        and checked.get('release_identity') == release_identity_payload(release)
        and checked.get('certifies_natural_queries') is False
        and checked.get('certifies_no_answer') is False
        and type(checked.get('natural_samples_created')) is int
        and checked['natural_samples_created'] == 0
        and not checked.get('blocked_reason')
    ):
        return 'current_controlled_recall_evidence_invalid'
    agents = checked.get('agents')
    if not isinstance(agents, list) or not agents:
        return 'controlled_recall_complete_scopes_required'
    for agent in agents:
        if not (
            isinstance(agent, dict) and agent.get('accepted') is True
            and agent.get('complete') is True and not agent.get('blocked_reason')
            and type(agent.get('sample_count')) is int and agent['sample_count'] >= 3
            and type(agent.get('pass_count')) is int
            and agent['pass_count'] == agent['sample_count']
            and type(agent.get('fail_count')) is int and agent['fail_count'] == 0
        ):
            return 'controlled_recall_complete_scopes_required'
    return ''


def finalize_controlled_release_lineage(runtime: Any, *, scope: dict[str, str],
                                        repo_root: str, release: ReleaseIdentity,
                                        controlled_reference: str, replay: dict,
                                        live: dict, channel_reference: str) -> dict:
    """Record and independently re-read all six current release domains."""
    from eimemory.governance.release.release_closure_lineage import (
        _canonical_live_case_record_ids,
    )
    from eimemory.governance.release.release_lineage import (
        current_release_lineage, record_release_lineage,
    )

    manifests = {
        str(value.get('manifest_record_id') or '').strip()
        for key in ('capability_replay', 'weak_capability_replay')
        if isinstance((value := replay.get(key)), dict)
        and str(value.get('manifest_record_id') or '').strip()
    }
    live_ids = _canonical_live_case_record_ids(live)
    if (len(manifests) != 1 or live_ids is None or not controlled_reference
            or not channel_reference or not release.complete):
        return dict(ok=False, validated=False, compatible=False,
                    error='controlled_release_lineage_references_incomplete')
    gates = {
        'memory.recall': [controlled_reference],
        'memory.governance': sorted(manifests),
        'channel.delivery': [channel_reference],
        'storage.integrity': live_ids,
        'code.evolution': [release.receipt_id],
        'deployment.runtime': [release.receipt_id],
    }
    # The controlled release workflow explicitly uses the retained replay
    # catalog.  This does not change dynamic Profile selection or thresholds.
    recorded = record_release_lineage(
        runtime, scope=scope, repo_root=repo_root, current_release=release,
        gate_evidence=gates, legacy_compatibility=True,
    )
    if not isinstance(recorded, dict) or any(recorded.get(key) is not True
                                            for key in ('ok', 'validated', 'compatible')):
        return recorded if isinstance(recorded, dict) else dict(
            ok=False, validated=False, compatible=False,
            error='controlled_release_lineage_record_failed')
    resolved = current_release_lineage(
        runtime, scope=scope, repo_root=repo_root, current_release=release,
        legacy_compatibility=True,
    )
    if (not isinstance(resolved, dict)
            or any(resolved.get(key) is not True for key in ('ok', 'validated', 'compatible'))
            or not recorded.get('record_id')
            or resolved.get('record_id') != recorded['record_id']
            or resolved.get('gate_evidence') != gates):
        return dict(ok=False, validated=False, compatible=False,
                    error='controlled_release_lineage_readback_failed')
    return {**resolved, 'recall_evidence_basis': 'controlled_existing_memory',
            'certifies_natural_queries': False, 'l5_certified': False}


def refresh_current_controlled_release_lineage(runtime: Any, *, scope: ScopeRef,
                                               release: ReleaseIdentity) -> dict:
    """Revalidate an established closure after a new native acceptance attempt.

    Nightly legitimately advances the signed acceptance high water. Reusing an
    older signature as if it were still current is forbidden; rebind the exact
    newest accepted record and independently validate every unchanged domain.
    This creates no business query, replay, live case or replacement failure.
    """
    from eimemory.evaluation.controlled_production_acceptance import (
        load_controlled_policy, verify_controlled_production_acceptance,
    )
    from eimemory.governance.release import release_lineage as native
    from eimemory.governance.release.evidence_contract import (
        deployment_receipt_for_scope, same_release_authority,
        verified_deployment_receipt_identity,
    )
    from eimemory.governance.l5.l5_readiness import _verified_replay_summary
    from eimemory.config.trusted import trusted_repository_root

    receipt = deployment_receipt_for_scope(runtime, release.receipt_id, scope)
    if not same_release_authority(verified_deployment_receipt_identity(receipt), release):
        return dict(ok=False, status='blocked', reason='controlled_lineage_release_unverified')
    owner = receipt.scope
    if load_controlled_policy(runtime, owner) is None:
        return dict(ok=True, status='not_applicable', reason='controlled_lineage_not_configured')
    checked = verify_controlled_production_acceptance(runtime, scope=asdict(owner), release=release, limit=500)
    if checked.get('accepted') is not True or not checked.get('record_id'):
        return dict(ok=False, status='blocked', reason='current_controlled_recall_evidence_invalid')
    selected = None
    for record in native._release_lineage_records(runtime, scope=owner, before_rowid=None):
        content = record.content if isinstance(record.content, dict) else {}
        body = content.get('lineage')
        declares_current = (record.meta.get('current_commit') == release.commit
                            or content.get('release_commit') == release.commit)
        # No fallback past a corrupt newest physical current-release record.
        if not isinstance(body, dict):
            if declares_current:
                raise ValueError('controlled_lineage_current_record_invalid')
            continue
        identity = native._identity_from_payload(body.get('current_release'))
        if declares_current and not same_release_authority(identity, release):
            raise ValueError('controlled_lineage_current_record_invalid')
        if same_release_authority(identity, release):
            selected = body
            break
    if selected is None:
        return dict(ok=True, status='awaiting_initial_closure', reason='controlled_lineage_not_established')
    gates = deepcopy(selected.get('gate_evidence'))
    if (selected.get('schema_version') != native.SCHEMA_VERSION
            or selected.get('scope') != asdict(owner)
            or not isinstance(gates, dict) or set(gates) != set(native.DOMAINS)
            or any(not isinstance(value, list) for value in gates.values())):
        raise ValueError('controlled_lineage_current_record_invalid')
    old_refs = gates['memory.recall']
    if not old_refs:
        return dict(ok=True, status='awaiting_initial_closure', reason='controlled_lineage_not_established')
    old_records = [runtime.store.get_by_id(ref, scope=owner) for ref in old_refs]
    if (len(old_records) != 1 or old_records[0] is None
            or old_records[0].source != CONTROLLED_SOURCE or old_records[0].scope != owner):
        return dict(ok=True, status='not_applicable', reason='lineage_uses_other_recall_contract')
    if any(not values for values in gates.values()):
        raise ValueError('controlled_lineage_current_record_incomplete')
    gates['memory.recall'] = [checked['record_id']]
    # Resolve the same original replay selection through the native complete
    # current-release cohort. A newer failed or incomplete replay still fails.
    records = {ref: runtime.store.get_by_id(ref, scope=owner) for ref in gates['memory.governance']}
    capabilities, minimums, error = native._manifest_profile_replay_contract(
        records, gates['memory.governance'], scope=owner,
    )
    if error:
        raise ValueError('controlled_lineage_replay_selection_invalid')
    catalog = native._resolve_lineage_catalog(None, legacy_compatibility=True)
    summary = _verified_replay_summary(runtime, scope=owner, limit=2000,
        capabilities=capabilities, missing_field='replay_capabilities_missing',
        release=release, minimums=minimums, catalog=catalog, legacy_compatibility=True)
    cohort = summary.get('manifest_record_id_cohort')
    if not isinstance(cohort, list) or not cohort:
        cohort = list(dict(summary.get('manifest_record_ids') or {}).values())
    if (summary.get('replay_capabilities_missing') or summary.get('manifest_rejection_reasons')
            or summary.get('rejection_reasons') or summary.get('fail_count') != 0
            or summary.get('not_run_count') != 0 or not cohort
            or any(not isinstance(ref, str) or not ref for ref in cohort)):
        return dict(ok=False, status='blocked', reason='current_release_replay_manifests_incomplete')
    gates['memory.governance'] = sorted(set(cohort))
    root = str(trusted_repository_root())
    recorded = native.record_release_lineage(runtime, scope=owner, repo_root=root,
        current_release=release, gate_evidence=gates, legacy_compatibility=True)
    if any(recorded.get(key) is not True for key in ('ok', 'validated', 'compatible')):
        return dict(ok=False, status='blocked', reason='controlled_lineage_revalidation_failed',
                    report=recorded)
    resolved = native.current_release_lineage(runtime, scope=owner, repo_root=root,
        current_release=release, legacy_compatibility=True)
    if (any(resolved.get(key) is not True for key in ('ok', 'validated', 'compatible'))
            or resolved.get('record_id') != recorded.get('record_id')
            or resolved.get('gate_evidence') != gates):
        raise ValueError('controlled_lineage_refresh_readback_invalid')
    return dict(ok=True, status='revalidated', record_id=resolved['record_id'],
        controlled_reference=checked['record_id'], scope=asdict(owner),
        recall_evidence_basis='controlled_existing_memory', certifies_natural_queries=False,
        l5_certified=False)
