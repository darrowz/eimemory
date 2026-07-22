from __future__ import annotations

from hashlib import sha256
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.cli.main import main as cli_main
from eimemory.evaluation.production_query_dataset import (
    accept_pending_production_query,
    build_production_query_dataset,
    collect_pending_production_queries,
    write_production_query_dataset,
)
from eimemory.evaluation.real_query_gate import freeze_production_recall_dataset
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import load_json_dataset_with_evidence


BASE_SCOPE = {"tenant_id": "default", "agent_id": "main", "workspace_id": "production", "user_id": "darrow"}
LABEL_PACKET_EVIDENCE = {
    "schema": "secure_dataset_fingerprint.v1",
    "digest": "d" * 64,
    "size": 512,
    "device": 1,
    "inode": 1,
}


def _seed_decision(runtime: Runtime, *, channel: str, index: int) -> RecordEnvelope:
    scope = resolve_channel_scope(channel, BASE_SCOPE)
    source_id = f"source-{channel}"
    record = RecordEnvelope.create(
        kind="memory",
        title=f"{channel} verified release memory {index}",
        summary="safe durable evidence",
        source=f"{channel}.memory",
        source_id=source_id,
        scope=ScopeRef.from_dict(scope),
        meta={"force_capture": True},
    )
    runtime.store.append(record)
    digest = sha256(f"raw secret query {channel} {index}".encode()).hexdigest()
    runtime.store.record_proactive_decision(
        {
            "decision_id": f"decision-{channel}-{index}",
            "channel": channel,
            "scope": scope,
            "source_key": sha256(source_id.encode()).hexdigest(),
            "source_ids": [source_id],
            "session_id": f"session-{channel}-{index}",
            "turn_id": f"turn-{index}",
            "query_id": f"query-{index}",
            "query_digest": digest,
            "effective_query_digest": digest,
            "task_type": "memory.recall",
            "policy_version": "proactive.test.v1",
            "release_identity": {
                "release_commit": "a" * 40,
                "release_version": "1.9.80",
                "deployment_receipt_id": "receipt",
                "release_session_id": "session",
            },
            "release_bound": True,
            "control_cohort": False,
            "pair_id": f"pair-{channel}-{index}",
        },
        [{"citation": "M1", "record_id": record.record_id, "source_id": source_id, "confidence": 0.9, "order": 0, "render_digest": "d" * 64}],
        [],
    )
    return record


def test_real_audit_collection_operator_acceptance_and_immutable_dataset_build(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path / "runtime")
    expected: dict[tuple[str, int], RecordEnvelope] = {}
    for channel in ("openclaw", "codex", "hermes"):
        for index in range(5):
            expected[(channel, index)] = _seed_decision(runtime, channel=channel, index=index)

    collected = collect_pending_production_queries(runtime, scope=BASE_SCOPE)
    assert collected["created"] == 15
    assert "raw secret query" not in json.dumps(collected)

    for pending_id in collected["pending_record_ids"]:
        pending = runtime.store.get_by_id(pending_id)
        assert pending is not None
        channel = str(pending.content["channel"])
        index = int(str(pending.content["capture_ref"]).rsplit("-", 1)[1])
        accepted = accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": [channel, "verified", "release", f"case-{index}"], "intent": "memory recall"},
            labels=[{"record_ref": expected[(channel, index)].record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
        assert accepted["ok"] is True

    dataset = build_production_query_dataset(runtime, scope=BASE_SCOPE)
    assert dataset["ready"] is True
    assert dataset["progress"]["per_channel_accepted"] == {"codex": 5, "hermes": 5, "openclaw": 5}
    output = tmp_path / "production-redacted.json"
    written = write_production_query_dataset(dataset["dataset"], output)
    loaded, evidence = load_json_dataset_with_evidence(str(output))
    frozen = freeze_production_recall_dataset({**loaded, "_secure_dataset_evidence": evidence})
    assert written["ok"] is True
    assert frozen["eligibility"]["ok"] is True
    assert "raw secret query" not in output.read_text(encoding="utf-8")
    runtime.close()


def test_operator_cannot_label_across_channel_or_source_boundary(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    correct = _seed_decision(runtime, channel="codex", index=0)
    wrong = _seed_decision(runtime, channel="hermes", index=0)
    pending_ids = collect_pending_production_queries(runtime, scope=BASE_SCOPE)["pending_record_ids"]
    pending_id = next(
        record_id
        for record_id in pending_ids
        if runtime.store.get_by_id(record_id).content["channel"] == "codex"
    )

    with pytest.raises(ValueError, match="boundary"):
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": ["codex", "memory"]},
            labels=[{"record_ref": wrong.record_id, "grade": 3}],
            labeler="operator",
            operator_scope=BASE_SCOPE,
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
    with pytest.raises(ValueError, match="boundary"):
        accept_pending_production_query(
            runtime,
            pending_record_id=pending_id,
            query_features={"terms": ["codex", "memory"]},
            labels=[{"record_ref": correct.record_id, "grade": 3}],
            labeler="operator",
            operator_scope={**BASE_SCOPE, "tenant_id": "other-tenant"},
            label_packet_evidence=LABEL_PACKET_EVIDENCE,
        )
    runtime.close()


def test_production_query_cli_collect_accept_and_status_without_raw_echo(tmp_path, monkeypatch, capsys) -> None:
    root = tmp_path / "runtime"
    runtime = Runtime.create(root=root)
    expected = _seed_decision(runtime, channel="codex", index=7)
    runtime.close()
    monkeypatch.setenv("EIMEMORY_ROOT", str(root))
    scope_args = [
        "--scope-agent", BASE_SCOPE["agent_id"],
        "--scope-workspace", BASE_SCOPE["workspace_id"],
        "--scope-user", BASE_SCOPE["user_id"],
    ]

    assert cli_main(["eval", "production-query", "collect", *scope_args]) == 0
    collected_text = capsys.readouterr().out
    collected = json.loads(collected_text)
    assert "raw secret query" not in collected_text
    pending_id = collected["pending_record_ids"][0]

    packet = tmp_path / "operator-label.json"
    packet.write_text(
        json.dumps(
            {
                "query_features": {"terms": ["codex", "verified", "release"], "intent": "memory recall"},
                "labels": [{"record_ref": expected.record_id, "grade": 3}],
                "labeler": "operator",
            }
        ),
        encoding="utf-8",
    )
    assert cli_main(
        ["eval", "production-query", "accept", pending_id, "--label-json", str(packet), *scope_args]
    ) == 0
    accepted_text = capsys.readouterr().out
    assert "query_features" not in accepted_text
    assert "raw secret query" not in accepted_text

    assert cli_main(["eval", "production-query", "status", *scope_args]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["ready"] is False
    assert status["progress"]["per_channel_accepted"]["codex"] == 1
