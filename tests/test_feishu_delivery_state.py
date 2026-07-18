from __future__ import annotations

import json
from pathlib import Path

import pytest

from eimemory.ops.feishu_delivery_state import (
    complete_delivery,
    escalate_delivery,
    prepare_delivery,
    reconcile_delivery,
)


def test_prepare_delivery_persists_sending_before_caller_can_send(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "attempts.json"

    decision = prepare_delivery(
        state_path,
        key="om_1",
        delivery_kind="final",
        idempotency_key="stable-key",
        payload_digest="digest",
        now_ms=1_000,
    )

    assert decision["send"] is True
    entry = json.loads(state_path.read_text(encoding="utf-8"))["entries"]["om_1"]
    assert entry["state"] == "sending"
    assert entry["attempt_count"] == 1


def test_prepare_delivery_never_reopens_ambiguous_intent(tmp_path: Path) -> None:
    state_path = tmp_path / "attempts.json"
    prepare_delivery(
        state_path,
        key="om_1",
        delivery_kind="final",
        idempotency_key="stable-key",
        payload_digest="digest",
        now_ms=1_000,
    )

    decision = prepare_delivery(
        state_path,
        key="om_1",
        delivery_kind="final",
        idempotency_key="stable-key",
        payload_digest="digest",
        now_ms=2_000,
    )

    assert decision["send"] is False
    assert decision["state"] == "sending"


def test_complete_delivery_requires_platform_receipt_for_success(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "attempts.json"
    prepare_delivery(
        state_path,
        key="om_1",
        delivery_kind="final",
        idempotency_key="stable-key",
        payload_digest="digest",
        now_ms=1_000,
    )

    with pytest.raises(ValueError, match="platform message receipt"):
        complete_delivery(
            state_path,
            key="om_1",
            ok=True,
            message_id="",
            error="",
            now_ms=2_000,
        )


def test_reconcile_sending_without_receipt_becomes_uncertain_not_retryable(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "attempts.json"
    prepare_delivery(
        state_path,
        key="om_1",
        delivery_kind="final",
        idempotency_key="stable-key",
        payload_digest="digest",
        now_ms=1_000,
    )

    entry = reconcile_delivery(
        state_path,
        key="om_1",
        found_message_id="",
        now_ms=7_000,
        uncertainty_after_ms=5_000,
        escalation_after_ms=10_000,
    )

    assert entry["state"] == "delivery_uncertain"
    assert entry["attempt_count"] == 1


def test_status_receipt_is_not_mislabeled_as_final_platform_acceptance(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "attempts.json"
    prepare_delivery(
        state_path,
        key="status:om_1",
        delivery_kind="status",
        idempotency_key="stable-key",
        payload_digest="digest",
        now_ms=1_000,
    )

    entry = complete_delivery(
        state_path,
        key="status:om_1",
        ok=True,
        message_id="om_notice",
        error="",
        now_ms=2_000,
    )

    assert entry["state"] == "status_notified"
    assert entry["notification_message_id"] == "om_notice"
    assert "platform_accepted_at_ms" not in entry


def test_platform_accepted_final_is_immutable_terminal(tmp_path: Path) -> None:
    state_path = tmp_path / "attempts.json"
    prepare_delivery(
        state_path,
        key="om_1",
        delivery_kind="final",
        idempotency_key="stable-key",
        payload_digest="digest",
        now_ms=1_000,
    )
    complete_delivery(
        state_path,
        key="om_1",
        ok=True,
        message_id="om_final",
        error="",
        now_ms=2_000,
    )

    with pytest.raises(ValueError, match="status_notified"):
        escalate_delivery(
            state_path,
            key="om_1",
            now_ms=3_000,
            reason="invalid",
        )
