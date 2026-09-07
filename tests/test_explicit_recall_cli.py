from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.cli.main import main
from eimemory.models.records import RecordEnvelope, ScopeRef


BASE = ScopeRef("default", "hongtu", "embodied", "cli-fixture-user")
SCOPE_ARGS = ["--scope-agent", BASE.agent_id, "--scope-workspace", BASE.workspace_id, "--scope-user", BASE.user_id]


@pytest.fixture
def explicit_cli(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz")
    runtime = Runtime.create(root=tmp_path / "runtime")
    gold = runtime.store.append(RecordEnvelope.create(
        kind="memory", title="Orchid archive destination", summary="Orchid stores durable archives in /srv/orchid/archive.",
        source="fixture.verified", source_id="shared", scope=BASE,
        meta={"memory_type": "durable_fact", "force_capture": True},
    ))
    capture = AgentRuntimeMemoryService(runtime).prefetch(
        channel="codex", scope=asdict(BASE), query="Orchid archive destination", limit=3,
        explicit_request={"session_id": "fixture-cli", "request_id": "request-1", "acceptance_generated": True},
    )["capture"]["record_id"]
    monkeypatch.setattr(Runtime, "create", classmethod(lambda cls, **kwargs: runtime))
    packet = tmp_path / "labels.json"
    packet.write_text(json.dumps({"labels": [{"record_ref": gold.record_id, "scope": asdict(BASE), "source_id": "shared", "grade": 3}], "labeler": "operator"}))
    packet.chmod(0o600)
    yield runtime, gold, capture, packet
    runtime.close()


def _invoke(capsys, operation, *args):
    code = main(["eval", "production-query", operation, *args, *SCOPE_ARGS])
    return code, json.loads(capsys.readouterr().out)


def test_explicit_cli_collect_secure_label_evaluate_and_readback(explicit_cli, tmp_path, capsys, trusted_dataset_path_ancestors):
    runtime, gold, capture, packet = explicit_cli
    code, collected = _invoke(capsys, "explicit-collect")
    assert code == 0 and collected["capture_record_ids"] == [capture]
    assert collected["natural_benchmark_eligible"] is False

    code, accepted = _invoke(capsys, "explicit-accept", capture, "--label-json", str(packet))
    assert code == 0
    stored = runtime.store.get_by_id(accepted["record_id"])
    evidence = stored.content["operator_packet_evidence"]
    assert evidence["digest"] == sha256(packet.read_bytes()).hexdigest()
    assert evidence["inode"] == packet.stat().st_ino
    assert stored.content["labels"][0]["scope"] == asdict(BASE)
    assert stored.content["labels"][0]["record_ref"] == gold.record_id

    manifest = tmp_path / "accepted-labels.json"
    manifest.write_text(json.dumps({"label_record_ids": [accepted["record_id"]]}))
    manifest.chmod(0o600)
    output = tmp_path / "acceptance-report.json"
    code, report = _invoke(capsys, "explicit-eval", "--labels-json", str(manifest), "--persist-report", "--output", str(output))
    assert code == 0 and report["sample_count"] == 1 and report["hit_rate"] == 1
    assert report["gate_status"] == "acceptance_only"
    assert report["natural_benchmark_eligible"] is False
    assert report["samples"][0]["acceptance_generated"] is True
    assert json.loads(output.read_text())["persisted_record_id"] == report["persisted_record_id"]
    saved = runtime.store.get_by_id(report["persisted_record_id"])
    assert saved.content["gate_status"] == "acceptance_only"
    assert runtime.store.sqlite.conn.execute("select count(*) from proactive_decisions").fetchone()[0] == 0
    code, natural = _invoke(capsys, "collect")
    assert code == 0 and natural["created"] == 0 and natural["pending_record_ids"] == []


@pytest.mark.parametrize("unsafe", ["symlink", "writable", "agent_labeler", "forged_result"])
def test_explicit_cli_rejects_unsafe_label_packet_before_writing(explicit_cli, tmp_path, capsys, unsafe, trusted_dataset_path_ancestors):
    runtime, _, capture, packet = explicit_cli
    if unsafe == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(packet)
        packet = link
    elif unsafe == "writable":
        packet.chmod(0o666)
    else:
        body = json.loads(packet.read_text())
        if unsafe == "agent_labeler":
            body["labeler"] = "agent"
        else:
            body["result"] = {"ok": True}
        packet.write_text(json.dumps(body))
    before = runtime.store.sqlite.conn.execute("select count(*) from records").fetchone()[0]
    code, report = _invoke(capsys, "explicit-accept", capture, "--label-json", str(packet))
    assert code == 2 and report["error"] == "production_query_operation_failed"
    assert runtime.store.sqlite.conn.execute("select count(*) from records").fetchone()[0] == before


@pytest.mark.parametrize("body", [{"label_record_ids": ["forged-label"], "result": {"ok": True}}, {"label_record_ids": "forged-label"}, {"label_record_ids": ["forged-label"]}])
def test_explicit_cli_eval_rejects_untrusted_manifest_or_label(explicit_cli, tmp_path, capsys, body, trusted_dataset_path_ancestors):
    runtime, _, _, _ = explicit_cli
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(body))
    manifest.chmod(0o600)
    before = runtime.store.sqlite.conn.execute("select count(*) from records").fetchone()[0]
    code, report = _invoke(capsys, "explicit-eval", "--labels-json", str(manifest), "--persist-report")
    assert code == 2 and report["error"] == "production_query_operation_failed"
    assert runtime.store.sqlite.conn.execute("select count(*) from records").fetchone()[0] == before
