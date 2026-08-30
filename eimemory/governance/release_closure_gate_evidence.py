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
    """Return a complete evidence payload for release-lineage gate checks."""
    evidence = {
        "memory.recall": list(recall_references),
        "memory.governance": list(governance_references),
        "channel.delivery": [channel_acceptance_record_id],
        "storage.integrity": list(live_record_ids),
        "code.evolution": [receipt_record_id],
        "deployment.runtime": [receipt_record_id],
    }

    return evidence


__all__ = ["build_release_lineage_gate_evidence"]
