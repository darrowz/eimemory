"""Exact release-lineage gate evidence assembly."""

from __future__ import annotations


def build_release_lineage_gate_evidence(
    *,
    recall_references: list[str],
    governance_references: list[str],
    channel_acceptance_record_id: str,
    live_record_ids: list[str],
    receipt_record_id: str,
) -> dict[str, list[str]]:
    return {
        "memory.recall": recall_references,
        "memory.governance": governance_references,
        "channel.delivery": [channel_acceptance_record_id],
        "storage.integrity": live_record_ids,
        "deployment.runtime": [receipt_record_id],
    }


__all__ = ["build_release_lineage_gate_evidence"]
