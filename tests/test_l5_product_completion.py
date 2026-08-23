from __future__ import annotations

from eimemory.governance.l5_product_completion import build_product_completion


def _assessment() -> dict:
    return {
        "ok": True,
        "status": "ready",
        "loop_maturity": "evolving",
        "adapter_readiness": {"hermes": "ready"},
        "deployment_assurance": {"ok": None, "required": False, "blocking": False},
    }


def test_product_completion_keeps_bootstrap_and_manual_incident_incomplete() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True},
        transaction={
            "qualifying_terminal_outcome": "succeeded_sedimented",
            "manual_bootstrap": True,
            "origin": "user_reported",
            "observation_valid": True,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["control_plane_ok"] is True
    assert report["product_l5_complete"] is False
    assert report["ok"] is False
    assert report["completion_status"] == "incomplete"
    assert "manual_bootstrap_nonqualifying" in report["gaps"]


def test_product_completion_requires_qualifying_terminal_and_current_lineage() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction={
            "transaction_id": "tx-system-implementation",
            "terminal_receipt_digest": "a" * 64,
            "qualifying_terminal_outcome": "succeeded_sedimented",
            "manual_bootstrap": False,
            "origin": "system_detector",
            "known_before_detection": False,
            "prior_user_reported": False,
            "observation_valid": True,
            "quarantined": False,
            "evidence_verified": True,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["product_l5_complete"] is True
    assert report["completion_status"] == "complete"
    assert report["ok"] is True


def test_product_completion_rejects_unverified_terminal_row_shape() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction={
            "transaction_id": "tx-shaped-only",
            "terminal_receipt_digest": "a" * 64,
            "qualifying_terminal_outcome": "succeeded_sedimented",
            "manual_bootstrap": False,
            "origin": "system_detector",
            "known_before_detection": False,
            "prior_user_reported": False,
            "observation_valid": True,
            "quarantined": False,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["product_l5_complete"] is False
    assert "transaction_evidence_unverified" in report["gaps"]


def test_product_completion_labels_healthy_rollback_without_calling_it_success() -> None:
    report = build_product_completion(
        _assessment(),
        provider={"ready": True, "catalog_ready": True, "advertisement_fresh": True},
        transaction={
            "transaction_id": "tx-system-rollback",
            "terminal_receipt_digest": "b" * 64,
            "qualifying_terminal_outcome": "rolled_back_healthy",
            "manual_bootstrap": False,
            "origin": "system_detector",
            "known_before_detection": False,
            "prior_user_reported": False,
            "candidate_pushed_and_deployed": True,
            "rollback_executed": True,
            "observation_valid": True,
            "quarantined": False,
            "evidence_verified": True,
        },
        current_lineage={"ok": True, "compatible": True},
    )

    assert report["product_l5_complete"] is True
    assert report["code_evolution"]["qualifying_terminal_outcome"] == "rolled_back_healthy"
    assert report["code_evolution"]["label"] == "rolled_back_healthy"
