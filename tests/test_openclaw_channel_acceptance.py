from __future__ import annotations

from dataclasses import asdict
import json
import time

from eimemory.api.runtime import Runtime
from eimemory.governance import openclaw_channel_acceptance as channel_acceptance
from eimemory.governance import evidence_contract
from eimemory.governance.evidence_contract import ReleaseIdentity
from eimemory.models.records import RecordEnvelope, ScopeRef


def test_channel_acceptance_requires_current_commit_platform_receipt_and_redacts_ids(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(
        agent_id="hongtu",
        workspace_id="embodied",
        user_id="darrow",
    )
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
        version="1.9.106",
        receipt_id=receipt.record_id,
        session_id="release-session-current",
    )
    monkeypatch.setattr(
        channel_acceptance,
        "verified_deployment_receipt_identity",
        lambda _receipt: release,
    )
    monkeypatch.setattr(
        evidence_contract,
        "current_release_identity",
        lambda _runtime, _scope: release,
    )
    state_path = tmp_path / "reply-state.json"
    received_at_ms = int(time.time() * 1000)
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v2",
                "entries": {
                    "om_raw_inbound": {
                        "status": "platform_accepted",
                        "delivery_message_id": "om_raw_platform_receipt",
                        "runtime_commit": release.commit,
                        "session_key": "agent:main:feishu:direct:ou_raw_user",
                        "received_at_ms": received_at_ms,
                        "platform_accepted_at_ms": received_at_ms + 1_000,
                        "final_text": "raw reply content must not enter lineage",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_openclaw_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            state_path=state_path,
        )
        record = runtime.store.get_by_id(report["record_id"], scope=scope)
    finally:
        runtime.close()

    assert report["ok"] is True
    assert record is not None
    assert channel_acceptance.validate_openclaw_channel_acceptance(
        record,
        current_release=release,
    )
    serialized = json.dumps(record.to_dict(), ensure_ascii=False)
    assert "om_raw_inbound" not in serialized
    assert "om_raw_platform_receipt" not in serialized
    assert "ou_raw_user" not in serialized
    assert "raw reply content" not in serialized


def test_channel_acceptance_rejects_receipt_from_another_runtime_commit(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    receipt = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Deployment receipt",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
        )
    )
    release = ReleaseIdentity("a" * 40, "1.9.106", receipt.record_id, "session")
    monkeypatch.setattr(
        channel_acceptance,
        "verified_deployment_receipt_identity",
        lambda _receipt: release,
    )
    state_path = tmp_path / "reply-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v2",
                "entries": {
                    "om_old": {
                        "status": "platform_accepted",
                        "delivery_message_id": "om_receipt",
                        "runtime_commit": "b" * 40,
                        "session_key": "agent:main:feishu:direct:ou_user",
                        "received_at_ms": 1_000,
                        "platform_accepted_at_ms": 2_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_openclaw_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            state_path=state_path,
        )
    finally:
        runtime.close()

    assert report == {
        "ok": False,
        "error": "current_release_channel_receipt_not_found",
    }


def test_channel_acceptance_rejects_predeployment_receipt_for_same_commit(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    receipt = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Deployment receipt",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
        )
    )
    release = ReleaseIdentity("a" * 40, "1.9.106", receipt.record_id, "session")
    monkeypatch.setattr(
        channel_acceptance,
        "verified_deployment_receipt_identity",
        lambda _receipt: release,
    )
    state_path = tmp_path / "reply-state.json"
    state_path.write_text(
        json.dumps(
            {
                "schema_version": "openclaw_reply_delivery.v2",
                "entries": {
                    "om_old": {
                        "status": "platform_accepted",
                        "delivery_message_id": "om_receipt",
                        "runtime_commit": release.commit,
                        "session_key": "agent:main:feishu:direct:ou_user",
                        "received_at_ms": 1_000,
                        "platform_accepted_at_ms": 2_000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    try:
        report = runtime.record_openclaw_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            state_path=state_path,
        )
    finally:
        runtime.close()

    assert report == {
        "ok": False,
        "error": "current_release_channel_receipt_not_found",
    }


def test_channel_acceptance_reports_missing_and_malformed_state(
    tmp_path, monkeypatch
) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")
    receipt = runtime.store.append(
        RecordEnvelope.create(
            kind="promotion_request",
            title="Deployment receipt",
            scope=scope,
            source="eimemory.deployment_receipt",
            status="deployed",
        )
    )
    release = ReleaseIdentity("a" * 40, "1.9.106", receipt.record_id, "session")
    monkeypatch.setattr(
        channel_acceptance,
        "verified_deployment_receipt_identity",
        lambda _receipt: release,
    )
    state_path = tmp_path / "reply-state.json"

    try:
        missing = runtime.record_openclaw_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            state_path=state_path,
        )
        state_path.write_text("{", encoding="utf-8")
        malformed = runtime.record_openclaw_channel_acceptance(
            scope=asdict(scope),
            current_release=release,
            state_path=state_path,
        )
    finally:
        runtime.close()

    assert missing == {"ok": False, "error": "channel_delivery_state_missing"}
    assert malformed == {"ok": False, "error": "channel_delivery_state_invalid"}
