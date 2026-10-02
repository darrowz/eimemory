"""Collected capabilities are observed, not closed by a fixed sample quota."""
from __future__ import annotations

from eimemory.governance.l5.closure_rehearsal import (
    bootstrap_pending_non_recall_deficits,
    _compatible_live_task_accumulation,
)
from eimemory.governance.l5.l5_readiness import _real_business_gate
from eimemory.governance.l5.real_task_coverage import (
    IMPROVING,
    INSUFFICIENT_HISTORY,
    NOT_REUSED,
    REUSED,
    assess_capability_lifecycle,
)
from eimemory.governance.release.evidence_contract import ReleaseIdentity


def _release() -> ReleaseIdentity:
    return ReleaseIdentity("a" * 40, "1.14.34", "rec_verified", "sess_verified")


def _counts(types: list[str], samples: int = 1) -> dict[str, int]:
    return {task_type: samples for task_type in types}


def _channel(release: ReleaseIdentity, *, mode: str = "current", changed: bool = True, receipt: str | None = None) -> dict:
    return {
        "mode": mode,
        "changed": changed,
        "gate_errors": {},
        "evidence_release": {
            "commit": release.commit,
            "version": release.version,
            "receipt_id": receipt if receipt is not None else release.receipt_id,
            "session_id": release.session_id,
        },
    }


def _readiness(
    release: ReleaseIdentity,
    *,
    channel: dict,
    enabled: list[str] | None,
    counts: dict[str, int] | None,
    samples: int = 10,
) -> dict:
    return {
        "live_task_gate": {
            "ok": False,
            "current_deployment_verified_real_tasks": samples,
            "distinct_task_types": 0 if enabled is None else len(enabled),
            "enabled_task_types": enabled,
            "per_type_sample_counts": counts,
            "success_rate": 1.0,
            "evidence_mode": "current_release",
            "evidence_release_commit": release.commit,
            "current_release_commit": release.commit,
            "current_deployment_operational_probes": 10,
        },
        "verified_real_replay": {"ok": False},
        "hard_metric_samples": {
            "verified_real_tasks": samples,
            "verified_real_task_types": 0 if enabled is None else len(enabled),
            "enabled_task_types": enabled,
            "verified_real_task_type_counts": counts,
            "current_deployment_operational_probes": 10,
            "current_deployment_live_task_types": 5,
        },
        "hard_metrics": {
            "verified_real_task_success_rate": 1.0,
            "current_deployment_live_task_success_rate": 1.0,
        },
        "hard_metric_quality": {
            "verified_real_task_success_rate": {"sufficient": True},
            "current_deployment_live_task_success_rate": {"sufficient": True},
        },
        "release_lineage": {
            "ok": True,
            "validated": True,
            "compatible": True,
            "current_release": {
                "commit": release.commit,
                "version": release.version,
                "receipt_id": release.receipt_id,
                "session_id": release.session_id,
            },
            "domains": {"channel.delivery": channel},
        },
    }


def test_verified_channel_change_is_not_a_lineage_deficit() -> None:
    release = _release()
    enabled = ["memory.recall", "research.test"]
    deficits = bootstrap_pending_non_recall_deficits(
        _readiness(
            release,
            channel=_channel(release, changed=True),
            enabled=enabled,
            counts=_counts(enabled),
        ),
        release=release,
    )
    assert "channel_delivery_lineage_not_verified" not in deficits


def test_unverified_or_mismatched_channel_change_still_blocks() -> None:
    release = _release()
    enabled = ["memory.recall"]
    unverified = bootstrap_pending_non_recall_deficits(
        _readiness(
            release,
            channel={"mode": "changed_unverified", "changed": True, "gate_errors": {}, "evidence_release": {}},
            enabled=enabled,
            counts=_counts(enabled),
        ),
        release=release,
    )
    mismatched = bootstrap_pending_non_recall_deficits(
        _readiness(
            release,
            channel=_channel(release, receipt="other-receipt"),
            enabled=enabled,
            counts=_counts(enabled),
        ),
        release=release,
    )
    assert "channel_delivery_lineage_not_verified" in unverified
    assert "channel_delivery_lineage_not_verified" in mismatched


def test_collected_type_count_is_not_capped_and_short_samples_do_not_fail() -> None:
    release = _release()
    many = [f"capability.{index}" for index in range(8)]
    two = ["memory.recall", "research.test"]
    covered_many = bootstrap_pending_non_recall_deficits(
        _readiness(release, channel=_channel(release), enabled=many, counts=_counts(many)),
        release=release,
    )
    short = bootstrap_pending_non_recall_deficits(
        _readiness(release, channel=_channel(release), enabled=two, counts=_counts(two, samples=1)),
        release=release,
    )
    missing = bootstrap_pending_non_recall_deficits(
        _readiness(release, channel=_channel(release), enabled=None, counts=None),
        release=release,
    )
    empty = bootstrap_pending_non_recall_deficits(
        _readiness(release, channel=_channel(release), enabled=[], counts={}),
        release=release,
    )
    assert "enabled_real_task_type_samples_below_minimum" not in covered_many
    assert "enabled_real_task_type_samples_below_minimum" not in short
    assert "capability_collection_unreported" in missing
    assert "capability_collection_unreported" not in empty
    assert "enabled_real_task_types_missing" not in empty


def test_short_reuse_and_low_pass_rate_do_not_block_closure() -> None:
    release = _release()
    discovered = ["memory.recall", "research.test"]
    deficits = bootstrap_pending_non_recall_deficits(
        _readiness(
            release,
            channel=_channel(release),
            enabled=discovered,
            counts={"memory.recall": 1, "research.test": 0},
            samples=1,
        ),
        release=release,
    )
    low_rate = _readiness(
        release,
        channel=_channel(release),
        enabled=discovered,
        counts=_counts(discovered),
        samples=10,
    )
    low_rate["hard_metrics"]["verified_real_task_success_rate"] = 0.5
    low_rate["live_task_gate"]["success_rate"] = 0.5
    low_rate_deficits = bootstrap_pending_non_recall_deficits(low_rate, release=release)
    assert "enabled_real_task_type_samples_below_minimum" not in deficits
    assert "current_release_verified_real_tasks_below_minimum" not in deficits
    assert "historical_verified_real_tasks_below_minimum" not in deficits
    assert "historical_verified_real_task_success_below_minimum" not in low_rate_deficits
    assert "current_release_real_task_success_below_minimum" not in low_rate_deficits
    assert "channel_delivery_lineage_not_verified" not in deficits
    assert _compatible_live_task_accumulation(low_rate, release=release) is True


def test_business_gate_accepts_any_collected_type_count_with_five_samples_each() -> None:
    many = [f"capability.{index}" for index in range(8)]
    live = {
        "ok": True,
        "sample_count": 40,
        "current_deployment_verified_real_tasks": 40,
        "distinct_task_types": len(many),
        "enabled_task_types": many,
        "per_type_sample_counts": _counts(many),
        "success_rate": 0.8,
    }
    replay = {
        "ok": True,
        "sample_count": 40,
        "distinct_task_types": len(many),
        "enabled_task_types": many,
        "per_type_sample_counts": _counts(many),
        "pass_rate": 0.8,
        "provenance_contract": "verified_real_replay.v1",
    }
    accepted = _real_business_gate(live, replay)
    short = _real_business_gate(
        {**live, "per_type_sample_counts": _counts(many, samples=4)},
        {**replay, "ok": False},
    )
    assert accepted["ok"] is True
    assert short["ok"] is True


def test_lifecycle_records_reuse_and_adjacent_trend_without_closing_on_either() -> None:
    discovered = assess_capability_lifecycle(
        ["memory.recall"],
        {"memory.recall": 0},
    )
    reused = assess_capability_lifecycle(
        ["memory.recall"],
        per_type_outcomes={"memory.recall": [True]},
    )
    improved = assess_capability_lifecycle(
        ["memory.recall"],
        per_type_outcomes={"memory.recall": [False, True]},
    )
    recall = discovered["capabilities"]["memory.recall"]
    assert recall["reuse"] == NOT_REUSED
    assert recall["trend"] == INSUFFICIENT_HISTORY
    assert reused["capabilities"]["memory.recall"]["reuse"] == REUSED
    assert reused["capabilities"]["memory.recall"]["trend"] == INSUFFICIENT_HISTORY
    assert improved["capabilities"]["memory.recall"]["trend"] == IMPROVING
    assert discovered["inspection_alerts"] == []
