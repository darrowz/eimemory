from copy import deepcopy
from dataclasses import asdict

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.models.records import RecordEnvelope, ScopeRef


BASE = dict(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="fixture-user")
KEY = "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", KEY)
    runtime = Runtime.create(root=tmp_path / "runtime")
    record = RecordEnvelope.create(
        kind="memory", title="Orchid service connection", summary="Orchid uses rpc.example:7443 and /srv/orchid/data.",
        scope=ScopeRef.from_dict(BASE), source="fixture.verified", source_id="shared",
        meta={"memory_type": "durable_fact", "force_capture": True},
    )
    runtime.store.append(record)
    yield runtime, AgentRuntimeMemoryService(runtime), record
    runtime.close()


def call(service, **overrides):
    args = dict(channel="codex", scope=BASE, query="Orchid service connection", limit=3,
                explicit_request={"session_id": "mcp-test-session", "request_id": "42", "acceptance_generated": True})
    args.update(overrides)
    return service.prefetch(**args)


def test_real_prefetch_captures_shared_result_and_retries_idempotently(setup):
    runtime, service, gold = setup
    result = call(service)
    from eimemory.evaluation.explicit_recall import load_explicit_capture, collect_explicit_queries
    capture = load_explicit_capture(runtime, result["capture"]["record_id"], scope=resolve_channel_scope("codex", BASE))
    assert capture.content["invocation_kind"] == "explicit"
    assert capture.content["acceptance_generated"] is True
    assert capture.content["request"]["query"] == "Orchid service connection"
    refs = capture.content["references"]
    assert any(ref["record_ref"] == gold.record_id and ref["scope"] == BASE and ref["source_id"] == "shared" for ref in refs)
    assert call(service) == result
    assert collect_explicit_queries(runtime, scope=BASE)["capture_record_ids"] == [capture.record_id]
    assert runtime.store.sqlite.conn.execute("select count(*) from proactive_decisions").fetchone()[0] == 0
    with pytest.raises(ValueError, match="identity conflict"):
        call(service, query="a different request")


@pytest.mark.parametrize("mutation", ["query", "result", "scope", "source", "acceptance"])
def test_capture_reader_rejects_tampering_even_after_normal_record_rewrite(setup, mutation):
    runtime, service, _ = setup
    result = call(service)
    from eimemory.evaluation.explicit_recall import load_explicit_capture
    sc = resolve_channel_scope("codex", BASE)
    captured = runtime.store.get_by_id(result["capture"]["record_id"], scope=sc)
    if mutation == "query": captured.content["request"]["query"] = "forged"
    elif mutation == "result": captured.content["result"]["bundle"]["items"] = []
    elif mutation == "scope": captured.content["references"][0]["scope"]["user_id"] = "another-user"
    elif mutation == "source": captured.content["references"][0]["source_id"] = "other-source"
    else: captured.content["acceptance_generated"] = False
    runtime.store.append(captured)
    with pytest.raises(ValueError, match="signature"):
        load_explicit_capture(runtime, captured.record_id, scope=sc)


def test_capture_cannot_be_read_or_labelled_by_another_user(setup):
    runtime, service, gold = setup
    result = call(service)
    from eimemory.evaluation.explicit_recall import load_explicit_capture, accept_explicit_query
    with pytest.raises(ValueError):
        load_explicit_capture(runtime, result["capture"]["record_id"], scope={**resolve_channel_scope("codex", BASE), "user_id": "other"})
    with pytest.raises(ValueError):
        accept_explicit_query(runtime, capture_record_id=result["capture"]["record_id"], operator_scope={**BASE, "user_id": "other"}, labels=[], packet_evidence={})


def test_empty_successful_recall_keeps_missed_durable_gold_as_a_failure(setup, monkeypatch):
    runtime, service, gold = setup
    from eimemory.models.records import RecallBundle
    from eimemory.evaluation.explicit_recall import accept_explicit_query, evaluate_explicit_queries
    monkeypatch.setattr(runtime.memory, "recall", lambda **kwargs: RecallBundle(items=[], rules=[], reflections=[], confidence=0, next_action_hint=""))
    result = call(service)
    assert result["ok"] is True
    accepted = accept_explicit_query(runtime, capture_record_id=result["capture"]["record_id"], operator_scope=BASE,
                                    labels=[dict(record_ref=gold.record_id, scope=BASE, source_id="shared", grade=3)],
                                    packet_evidence=dict(schema="secure_dataset_fingerprint.v1", digest="a" * 64, size=100, device=1, inode=1))
    report = evaluate_explicit_queries(runtime, scope=BASE, label_record_ids=[accepted["record_id"]])
    assert report["sample_count"] == 1 and report["ok"] is False
    assert report["hit_rate"] == report["mrr"] == 0
    assert report["samples"][0]["gold"][0]["record_ref"] == gold.record_id


def test_recall_failure_is_captured_truthfully(setup, monkeypatch):
    runtime, service, _ = setup
    def fail(**kwargs): raise RuntimeError("private failure text")
    monkeypatch.setattr(runtime.memory, "recall", fail)
    result = call(service)
    from eimemory.evaluation.explicit_recall import load_explicit_capture
    capture = load_explicit_capture(runtime, result["capture"]["record_id"], scope=resolve_channel_scope("codex", BASE))
    assert result["ok"] is False
    assert capture.content["result"]["error"] == "RuntimeError"
    assert capture.content["references"] == []
    assert "private failure text" not in str(capture.to_dict())


def test_shared_gold_acceptance_and_missing_hit_report_keep_physical_authority(setup):
    runtime, service, gold = setup
    result = call(service)
    from eimemory.evaluation.explicit_recall import accept_explicit_query, evaluate_explicit_queries
    label = {"record_ref": gold.record_id, "scope": BASE, "source_id": "shared", "grade": 3}
    packet = dict(schema="secure_dataset_fingerprint.v1", digest="a" * 64, size=100, device=1, inode=1)
    accepted = accept_explicit_query(runtime, capture_record_id=result["capture"]["record_id"], operator_scope=BASE, labels=[label], packet_evidence=packet)
    report = evaluate_explicit_queries(runtime, scope=BASE, label_record_ids=[accepted["record_id"]], persist=True)
    assert report["ok"] and report["hit_rate"] == 1
    assert report["gate_status"] == "acceptance_only"
    assert report["natural_benchmark_eligible"] is False
    assert report["samples"][0]["gold"][0]["scope"] == BASE
    assert runtime.store.get_by_id(report["persisted_record_id"]) is not None
    with pytest.raises(ValueError, match="source or scope"):
        accept_explicit_query(runtime, capture_record_id=result["capture"]["record_id"], operator_scope=BASE, labels=[{**label, "source_id": "wrong"}], packet_evidence=packet)
    with pytest.raises(ValueError, match="duplicate explicit event"):
        evaluate_explicit_queries(runtime, scope=BASE, label_record_ids=[accepted["record_id"], accepted["record_id"]])


@pytest.mark.parametrize("kind,memory_type,source", [("memory", "conversation", "fixture.raw"), ("memory", "durable_fact", "fixture.diagnostic"), ("evaluation_packet", "durable_fact", "fixture.label")])
def test_raw_and_diagnostic_records_never_become_explicit_gold(setup, kind, memory_type, source):
    runtime, service, _ = setup
    result = call(service)
    bad = RecordEnvelope.create(kind=kind, title="connection diagnostic smoke", summary="raw connection probe", scope=ScopeRef.from_dict(BASE), source=source,
                                meta={"memory_type": memory_type, "force_capture": True})
    runtime.store.append(bad)
    from eimemory.evaluation.explicit_recall import accept_explicit_query
    with pytest.raises(ValueError, match="durable recall gold"):
        accept_explicit_query(runtime, capture_record_id=result["capture"]["record_id"], operator_scope=BASE,
                              labels=[dict(record_ref=bad.record_id, scope=BASE, source_id="default", grade=3)],
                              packet_evidence=dict(schema="secure_dataset_fingerprint.v1", digest="a" * 64, size=100, device=1, inode=1))


@pytest.mark.parametrize("field", ["summary", "context"])
def test_modified_provider_result_cannot_be_attested_as_authoritative(setup, monkeypatch, field):
    runtime, service, _ = setup
    original = service._prefetch_result
    def tamper(**kwargs):
        result, bundle = original(**kwargs)
        if field == "summary":
            result["bundle"]["items"][0]["summary"] = "attacker substituted address"
        else:
            result["context"] = "attacker substituted context"
        return result, bundle
    monkeypatch.setattr(service, "_prefetch_result", tamper)
    result = call(service)
    assert result["ok"] is False
    from eimemory.evaluation.explicit_recall import load_explicit_capture
    capture = load_explicit_capture(runtime, result["capture"]["record_id"], scope=resolve_channel_scope("codex", BASE))
    assert capture.content["references"] == []
    assert capture.content["error"] == "ValueError"


def _verified_release_fixture(runtime, scope):
    from eimemory.governance.evidence_contract import verified_deployment_receipt_identity, release_identity_payload
    commit, prior = "a" * 40, "b" * 40
    release_path = f"/opt/eimemory/releases/{commit}"
    receipt = RecordEnvelope.create(
        kind="promotion_request", title="Anonymous fixture deployment", summary="Fixture verified receipt",
        scope=ScopeRef.from_dict(scope), source="eimemory.deployment_receipt", status="deployed",
        content={
            "report_type": "deployment_receipt", "promotion_target": "code_patch", "action": "code_patch",
            "gate": {"ok": True, "receipt_verified": True},
            "side_effect": {
                "ok": True, "production_applied": True, "deployment_executed": True,
                "verification": {"ok": True, "skipped": False, "prior_commit": prior},
                "deployment": {"ok": True, "skipped": False, "release_path": release_path},
                "post_deploy_health": {"ok": True, "skipped": False, "commit": commit, "release_path": release_path},
                "commit": {"commit_sha": commit}, "release": {"version": "0.0.1", "release_path": release_path},
                "rollback_evidence": {"prior_commit_sha": prior, "rollback_command": "fixture rollback"},
            },
        },
    )
    runtime.store.append(receipt)
    identity = verified_deployment_receipt_identity(receipt)
    assert identity is not None
    return receipt, release_identity_payload(identity)


@pytest.mark.parametrize("receipt_channel", ["base", "codex"])
def test_release_reference_independently_resolves_exact_or_existing_base_fallback(setup, monkeypatch, receipt_channel):
    runtime, service, _ = setup
    physical_scope = BASE if receipt_channel == "base" else resolve_channel_scope("codex", BASE)
    receipt, release = _verified_release_fixture(runtime, physical_scope)
    monkeypatch.setattr(service, "_proactive_release", lambda *args: release)
    result = call(service)
    from eimemory.evaluation.explicit_recall import load_explicit_capture
    capture = load_explicit_capture(runtime, result["capture"]["record_id"], scope=resolve_channel_scope("codex", BASE))
    assert capture.content["release_identity"] == release
    assert capture.content["release_reference"]["record_ref"] == receipt.record_id
    assert capture.content["release_reference"]["scope"] == physical_scope
    assert capture.content["release_reference"]["source"] == "eimemory.deployment_receipt"
    assert call(service) == result


@pytest.mark.parametrize("invalid", ["other_user", "other_channel", "missing_receipt", "wrong_source"])
def test_release_reference_rejects_unavailable_or_unauthorized_receipt(setup, monkeypatch, invalid):
    runtime, service, _ = setup
    receipt_scope = ({**BASE, "user_id": "another-user"} if invalid == "other_user" else
                     resolve_channel_scope("hermes", BASE) if invalid == "other_channel" else BASE)
    receipt, release = _verified_release_fixture(runtime, receipt_scope)
    if invalid == "missing_receipt":
        release = {**release, "deployment_receipt_id": "receipt-does-not-exist"}
    elif invalid == "wrong_source":
        receipt.source = "fixture.untrusted-receipt"
        runtime.store.append(receipt)
    monkeypatch.setattr(service, "_proactive_release", lambda *args: release)
    with pytest.raises(ValueError, match="release receipt invalid"):
        call(service)


@pytest.mark.parametrize("mutation", ["failed_health", "signed_digest_changed"])
def test_capture_reader_rejects_changed_receipt_despite_normal_record_rewrite(setup, monkeypatch, mutation):
    runtime, service, _ = setup
    receipt, release = _verified_release_fixture(runtime, BASE)
    monkeypatch.setattr(service, "_proactive_release", lambda *args: release)
    result = call(service)
    if mutation == "failed_health":
        receipt.content["side_effect"]["post_deploy_health"]["ok"] = False
    else:
        receipt.summary = "changed authoritative receipt contents"
    runtime.store.append(receipt)
    from eimemory.evaluation.explicit_recall import load_explicit_capture
    with pytest.raises(ValueError, match="release (receipt|provenance|reference)"):
        load_explicit_capture(runtime, result["capture"]["record_id"], scope=resolve_channel_scope("codex", BASE))


class _InProcessRPCTransport:
    """Only replace HTTP transport; dispatch, memory and persistence are real."""

    def __init__(self, runtime):
        from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
        self.bridge = EIBrainRPCBridge(runtime)

    def call_or_bypass(self, method, params):
        return {**self.bridge.handle({"method": method, "params": params}), "bypassed": False}


def _mcp_explicit_call(server):
    return server.handle_message({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "eimemory_recall", "arguments": {
            "query": "Orchid service connection", "limit": 3, "acceptance_generated": True,
        }},
    })


def test_mcp_rpc_dispatch_captures_real_query_and_shared_result(setup):
    runtime, _, gold = setup
    from eimemory.adapters.codex.mcp_server import CodexMCPServer
    from eimemory.evaluation.explicit_recall import load_explicit_capture
    server = CodexMCPServer(client=_InProcessRPCTransport(runtime), scope=BASE)
    response = _mcp_explicit_call(server)
    assert response["id"] == 7
    assert response["result"]["isError"] is False
    rpc_payload = response["result"]["structuredContent"]
    assert rpc_payload["ok"] is True and rpc_payload["bypassed"] is False
    result = rpc_payload["result"]
    capture = load_explicit_capture(runtime, result["capture"]["record_id"], scope=resolve_channel_scope("codex", BASE))
    assert capture.content["request"]["session_id"] == server.session_id
    assert capture.content["request"]["request_id"] == "7"
    assert capture.content["request"]["query"] == "Orchid service connection"
    assert capture.content["acceptance_generated"] is True
    assert any(ref["record_ref"] == gold.record_id and ref["scope"] == BASE for ref in capture.content["references"])
    assert _mcp_explicit_call(server) == response
    assert runtime.store.sqlite.conn.execute("select count(*) from proactive_decisions").fetchone()[0] == 0


def test_mcp_sessions_do_not_collide_when_jsonrpc_request_id_is_reused(setup):
    runtime, _, _ = setup
    from eimemory.adapters.codex.mcp_server import CodexMCPServer
    from eimemory.evaluation.explicit_recall import load_explicit_capture
    transport = _InProcessRPCTransport(runtime)
    servers = [CodexMCPServer(client=transport, scope=BASE) for _ in range(2)]
    captures = []
    for server in servers:
        response = _mcp_explicit_call(server)
        assert response["result"]["isError"] is False
        record_id = response["result"]["structuredContent"]["result"]["capture"]["record_id"]
        captures.append(load_explicit_capture(runtime, record_id, scope=resolve_channel_scope("codex", BASE)))
    assert servers[0].session_id != servers[1].session_id
    assert captures[0].record_id != captures[1].record_id
    assert {capture.content["request"]["request_id"] for capture in captures} == {"7"}
    assert {capture.content["request"]["session_id"] for capture in captures} == {server.session_id for server in servers}
