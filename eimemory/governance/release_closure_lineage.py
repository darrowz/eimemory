"""Release-lineage finalization for the legacy L5 closure workflow."""

from __future__ import annotations

from typing import Any

from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.governance.live_task_acceptance import LIVE_ACCEPTANCE_CASE_IDS
from eimemory.governance.release_closure_gate_evidence import (
    build_release_lineage_gate_evidence,
)


def finalize_release_lineage(
    runtime: Any,
    *,
    scope: dict[str, Any],
    repo_root: str,
    current_release: ReleaseIdentity,
    receipt_record_id: str,
    recall_gate_record_id: str,
    strict_state_record_id: str,
    bootstrap_pending_record_id: str,
    channel_acceptance_record_id: str,
    replay_bootstrap: dict[str, Any],
    capability_replay: dict[str, Any],
    live_acceptance: dict[str, Any],
) -> dict[str, Any]:
    capability_replay_value = replay_bootstrap.get("capability_replay")
    weak_replay_value = replay_bootstrap.get("weak_capability_replay")
    bootstrap_replay = (
        capability_replay_value
        if isinstance(capability_replay_value, dict)
        else weak_replay_value
        if isinstance(weak_replay_value, dict)
        else {}
    )
    bootstrap_manifest = str(bootstrap_replay.get("manifest_record_id") or "").strip()
    capability_manifest = str(capability_replay.get("manifest_record_id") or "").strip()
    live_record_ids = _canonical_live_case_record_ids(live_acceptance)
    if (
        not receipt_record_id
        or receipt_record_id != current_release.receipt_id
        or not bootstrap_manifest
        or not capability_manifest
        or not channel_acceptance_record_id
        or live_record_ids is None
        or bool(recall_gate_record_id) != bool(strict_state_record_id)
        or bool(bootstrap_pending_record_id)
        == bool(recall_gate_record_id and strict_state_record_id)
    ):
        return {
            "ok": False,
            "validated": False,
            "compatible": False,
            "error": "release_lineage_gate_references_incomplete",
        }
    recall_references = (
        [bootstrap_pending_record_id, capability_manifest]
        if bootstrap_pending_record_id
        else [recall_gate_record_id, strict_state_record_id]
    )
    gate_evidence = build_release_lineage_gate_evidence(
        recall_references=recall_references,
        governance_references=sorted({bootstrap_manifest, capability_manifest}),
        channel_acceptance_record_id=channel_acceptance_record_id,
        live_record_ids=live_record_ids,
        receipt_record_id=receipt_record_id,
    )
    recorded = runtime.record_release_lineage(
        scope=scope,
        repo_root=repo_root,
        current_release=current_release,
        gate_evidence=gate_evidence,
        legacy_compatibility=True,
    )
    if not isinstance(recorded, dict) or recorded.get("ok") is not True:
        return (
            recorded
            if isinstance(recorded, dict)
            else {
                "ok": False,
                "validated": False,
                "compatible": False,
                "error": "release_lineage_record_failed",
            }
        )
    resolved = runtime.current_release_lineage(
        scope=scope,
        repo_root=repo_root,
        current_release=current_release,
        legacy_compatibility=True,
    )
    if (
        not isinstance(resolved, dict)
        or resolved.get("ok") is not True
        or resolved.get("validated") is not True
        or str(resolved.get("record_id") or "") != str(recorded.get("record_id") or "")
    ):
        return {
            "ok": False,
            "validated": False,
            "compatible": False,
            "error": "release_lineage_revalidation_failed",
        }
    return resolved


def _canonical_live_case_record_ids(
    live_acceptance: dict[str, Any],
) -> list[str] | None:
    cases = live_acceptance.get("cases")
    if not isinstance(cases, list):
        return None
    by_case: dict[str, str] = {}
    for item in cases:
        if not isinstance(item, dict):
            return None
        case_id = str(item.get("case_id") or "").strip()
        record_id = str(item.get("record_id") or "").strip()
        if not case_id or not record_id or case_id in by_case:
            return None
        by_case[case_id] = record_id
    if set(by_case) != set(LIVE_ACCEPTANCE_CASE_IDS):
        return None
    ordered = [by_case[case_id] for case_id in LIVE_ACCEPTANCE_CASE_IDS]
    return ordered if len(set(ordered)) == len(ordered) else None


__all__ = ["finalize_release_lineage"]
