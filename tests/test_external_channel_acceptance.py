from __future__ import annotations

from dataclasses import asdict
import json
import time

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance import evidence_contract
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.models.records import RecordEnvelope, ScopeRef


def _release(runtime: Runtime, scope: ScopeRef, monkeypatch) -> ReleaseIdentity:
    receipt = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Deployment receipt",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
        )
    )
    release = ReleaseIdentity(
        commit="a" * 40,
        version="1.11.36",
        receipt_id=receipt.record_id,
        session_id="release-session-current",
    )
    monkeypatch.setattr(
        evidence_contract,
        "verified_deployment_receipt_identity",
        lambda _receipt: release,
    )
    return release


def _write_external(path, release: ReleaseIdentity, **overrides) -> None:
    received_at_ms = int(time.time() * 1000) + 1_000
    entry = {
        "status": "platform_accepted",
        "transport_owner": "hermes",
        "platform": "telegram",
        "conversation_kind": "direct",
        "inbound_message_id": "telegram-inbound-raw",
        "delivery_receipt_id": "hermes-obligation-raw",
        "runtime_commit": release.commit,
        "received_at_ms": received_at_ms,
        "platform_accepted_at_ms": received_at_ms + 1_000,
        **overrides,
    }
    path.write_text(
        json.dumps(
            {
                "schema_version": "external_channel_delivery.v1",
                "entries": {"receipt-1": entry},
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("platform", ["telegram", "weixin"])
def test_external_acceptance_accepts_hermes_non_feishu_delivery_and_redacts_ids(
    tmp_path, monkeypatch, platform
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    state_path = tmp_path / "external-delivery.json"
    _write_external(state_path, release, platform=platform)

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=state_path,
        )
        record = runtime.store.get_by_id(report["record_id"], scope=scope)
        from eimemory.governance.external_channel_acceptance import (
            validate_external_channel_acceptance,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["channel_binding"] == "current_release_delivery"
    assert report["source_runtime_commit"] == release.commit
    assert report["deployment_commit"] == release.commit
    assert report["transport_owner"] == "hermes"
    assert report["platform"] == platform
    assert record is not None
    assert record.source == "eimemory.external_channel.acceptance"
    assert validate_external_channel_acceptance(record, current_release=release)
    serialized = json.dumps(record.to_dict(), ensure_ascii=False)
    assert "telegram-inbound-raw" not in serialized
    assert "hermes-obligation-raw" not in serialized
    record.content["transport_owner"] = "manual"
    assert not validate_external_channel_acceptance(record, current_release=release)


@pytest.mark.parametrize(
    ("overrides", "expected_ok"),
    [
        ({"runtime_commit": "b" * 40, "status": "failed"}, False),
        ({"status": "failed"}, False),
        ({"inbound_message_id": ""}, False),
        ({"delivery_receipt_id": ""}, False),
        ({"platform": "local"}, False),
        ({"platform": "deployment-replay"}, False),
        ({"transport_owner": "manual"}, False),
        ({"conversation_kind": "synthetic"}, False),
        ({"platform_accepted_at_ms": 1}, False),
    ],
)
def test_external_acceptance_rejects_untrusted_or_stale_delivery_shapes(
    tmp_path, monkeypatch, overrides, expected_ok
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    state_path = tmp_path / "external-delivery.json"
    _write_external(state_path, release, **overrides)

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=state_path,
        )
    finally:
        runtime.close()

    assert report["ok"] is expected_ok
    assert report.get("error") == "current_release_channel_receipt_not_found"


def test_external_acceptance_uses_openclaw_feishu_compatibility_reader(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    received_at_ms = int(time.time() * 1000) + 1_000
    openclaw_path = tmp_path / "openclaw.json"
    openclaw_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v2",
                "entries": {
                    "om-inbound": {
                        "status": "platform_accepted",
                        "delivery_message_id": "om-outbound",
                        "runtime_commit": release.commit,
                        "session_key": "agent:main:feishu:direct:ou-user",
                        "received_at_ms": received_at_ms,
                        "platform_accepted_at_ms": received_at_ms + 1_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=openclaw_path,
            external_state_path=tmp_path / "missing-external.json",
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["transport_owner"] == "openclaw"
    assert report["platform"] == "feishu"
    assert report["conversation_kind"] == "direct"


def test_external_acceptance_reports_missing_and_malformed_ledgers(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")

    try:
        missing = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=tmp_path / "missing-external.json",
        )
        invalid = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=malformed,
        )
    finally:
        runtime.close()

    assert missing == {"ok": False, "error": "channel_delivery_state_missing"}
    assert invalid == {"ok": False, "error": "channel_delivery_state_invalid"}


def _prior_entry(
    *,
    commit: str,
    inbound_message_id: str,
    delivery_receipt_id: str,
    received_at_ms: int,
    platform_accepted_at_ms: int,
) -> dict:
    return {
        "status": "platform_accepted",
        "transport_owner": "hermes",
        "platform": "feishu",
        "conversation_kind": "direct",
        "inbound_message_id": inbound_message_id,
        "delivery_receipt_id": delivery_receipt_id,
        "runtime_commit": commit,
        "received_at_ms": received_at_ms,
        "platform_accepted_at_ms": platform_accepted_at_ms,
    }


def test_external_acceptance_inherits_latest_operator_channel(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    older_commit = "b" * 40
    newer_commit = "c" * 40
    state_path = tmp_path / "external-delivery.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "external_channel_delivery.v1",
                "entries": {
                    "older": _prior_entry(
                        commit=older_commit,
                        inbound_message_id="older-inbound-raw",
                        delivery_receipt_id="older-receipt-raw",
                        received_at_ms=1_700_000_000_000,
                        platform_accepted_at_ms=1_700_000_001_000,
                    ),
                    "newer": _prior_entry(
                        commit=newer_commit,
                        inbound_message_id="newer-inbound-raw",
                        delivery_receipt_id="newer-receipt-raw",
                        received_at_ms=1_700_000_002_000,
                        platform_accepted_at_ms=1_700_000_003_000,
                    ),
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=state_path,
        )
        record = runtime.store.get_by_id(report["record_id"], scope=scope)
        from eimemory.governance.external_channel_acceptance import (
            validate_external_channel_acceptance,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["channel_binding"] == "inherited_operator_channel"
    assert report["source_runtime_commit"] == newer_commit
    assert report["deployment_commit"] == release.commit
    assert record is not None
    assert record.content["channel_binding"] == "inherited_operator_channel"
    assert record.summary == "The established operator channel carries this release."
    assert validate_external_channel_acceptance(record, current_release=release)
    serialized = json.dumps(record.to_dict(), ensure_ascii=False)
    assert "newer-inbound-raw" not in serialized
    assert "newer-receipt-raw" not in serialized


def test_external_acceptance_prefers_post_deploy_current_commit(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    received_at_ms = int(time.time() * 1000) + 1_000
    state_path = tmp_path / "external-delivery.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "external_channel_delivery.v1",
                "entries": {
                    "prior": _prior_entry(
                        commit="b" * 40,
                        inbound_message_id="prior-inbound-raw",
                        delivery_receipt_id="prior-receipt-raw",
                        received_at_ms=1_700_000_000_000,
                        platform_accepted_at_ms=9_000_000_000_000,
                    ),
                    "current": _prior_entry(
                        commit=release.commit,
                        inbound_message_id="current-inbound-raw",
                        delivery_receipt_id="current-receipt-raw",
                        received_at_ms=received_at_ms,
                        platform_accepted_at_ms=received_at_ms + 1_000,
                    ),
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=state_path,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["channel_binding"] == "current_release_delivery"
    assert report["source_runtime_commit"] == release.commit


def test_external_acceptance_inherits_pre_deploy_same_commit_delivery(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    state_path = tmp_path / "external-delivery.json"
    _write_external(
        state_path,
        release,
        received_at_ms=1_700_000_000_000,
        platform_accepted_at_ms=1_700_000_001_000,
    )

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=state_path,
        )
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["channel_binding"] == "inherited_operator_channel"
    assert report["source_runtime_commit"] == release.commit
    assert report["deployment_commit"] == release.commit


def test_external_acceptance_empty_ledger_stays_unbound(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    state_path = tmp_path / "external-delivery.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "external_channel_delivery.v1",
                "entries": {},
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=tmp_path / "missing-openclaw.json",
            external_state_path=state_path,
        )
    finally:
        runtime.close()

    assert report == {
        "ok": False,
        "error": "current_release_channel_receipt_not_found",
    }


def test_external_acceptance_inherits_openclaw_operator_channel(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    release = _release(runtime, scope, monkeypatch)
    prior_commit = "d" * 40
    openclaw_path = tmp_path / "openclaw.json"
    openclaw_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v2",
                "entries": {
                    "om-inbound-raw": {
                        "status": "platform_accepted",
                        "delivery_message_id": "om-outbound-raw",
                        "runtime_commit": prior_commit,
                        "session_key": "agent:main:feishu:direct:ou-user",
                        "received_at_ms": 1_700_000_000_000,
                        "platform_accepted_at_ms": 1_700_000_001_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_external_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            openclaw_state_path=openclaw_path,
            external_state_path=tmp_path / "missing-external.json",
        )
        record = runtime.store.get_by_id(report["record_id"], scope=scope)
    finally:
        runtime.close()

    assert report["ok"] is True
    assert report["channel_binding"] == "inherited_operator_channel"
    assert report["source_runtime_commit"] == prior_commit
    assert report["transport_owner"] == "openclaw"
    assert report["platform"] == "feishu"
    serialized = json.dumps(record.to_dict(), ensure_ascii=False)
    assert "om-inbound-raw" not in serialized
    assert "om-outbound-raw" not in serialized
    assert "ou-user" not in serialized
