from __future__ import annotations

import json
from pathlib import Path
import threading
from urllib.error import HTTPError
import urllib.request
from copy import deepcopy

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.adapters.runtime.channel import RUNTIME_ADAPTER_CONTRACT_VERSION
from eimemory.adapters.runtime.http_client import AgentRuntimeRPCClient
from eimemory.api.runtime import Runtime
from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION


AUTH_TOKEN = "AgentRuntimeAdapterToken_0123456789-Strong"
CODEX_ATTESTATION_TOKEN = "CodexAttestationToken_0123456789-Strong"
HERMES_ATTESTATION_TOKEN = "HermesAttestationToken_0123456789-Strong"
BASE_SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied",
    "user_id": "darrow",
}


def _hermes_mutation_params(**overrides: object) -> dict:
    params = {
        "channel": "hermes",
        "scope": BASE_SCOPE,
        "action": "add",
        "target": "memory",
        "source_id": "hermes-primary",
        "content": "Hermes durable research must cite primary sources.",
        "idempotency_key": "hermes-write-001",
        "provenance": {
            "write_origin": "hermes.memory_write",
            "session_id": "hermes-session",
            "platform": "hermes",
            "tool_call_id": "tool-001",
        },
    }
    params.update(overrides)
    return params


def test_runtime_adapter_rpc_mutates_hermes_memory_with_deterministic_retry_and_provenance(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)
    params = _hermes_mutation_params()

    added = bridge.handle({"method": "adapter.mutate_memory", "params": params})
    retried = bridge.handle({"method": "adapter.mutate_memory", "params": deepcopy(params)})

    assert added["ok"] is True
    assert added["result"]["action"] == "add"
    assert added["result"]["record"]["source_id"] == "hermes-primary"
    assert added["result"]["record"]["provenance"] == params["provenance"]
    assert added["result"]["record"]["meta"]["hermes_target"] == "memory"
    assert len(added["result"]["content_revision"]) == 64
    assert retried["ok"] is True
    assert retried["result"]["idempotent"] is True
    assert retried["result"]["record"]["record_id"] == added["result"]["record"]["record_id"]


def test_runtime_adapter_rpc_replaces_and_removes_only_the_exact_active_hermes_target(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)
    added = bridge.handle({"method": "adapter.mutate_memory", "params": _hermes_mutation_params()})
    old = added["result"]["record"]
    replacement_text = "Hermes durable research must cite primary sources and archive citations."
    replaced = bridge.handle(
        {
            "method": "adapter.mutate_memory",
            "params": _hermes_mutation_params(
                action="replace",
                content=replacement_text,
                old_text=old["content"]["text"],
                expected_revision=added["result"]["content_revision"],
                idempotency_key="hermes-write-002",
            ),
        }
    )

    assert replaced["ok"] is True
    successor = replaced["result"]["record"]
    assert successor["status"] == "active"
    assert {link["relation"] for link in successor["links"]} == {"supersedes"}
    old_record = runtime.store.get_by_id(old["record_id"], scope=old["scope"])
    assert old_record is not None and old_record.status == "superseded"
    assert old_record.links[-1].relation == "superseded_by"

    removed = bridge.handle(
        {
            "method": "adapter.mutate_memory",
            "params": _hermes_mutation_params(
                action="remove",
                content="",
                target_record_id=successor["record_id"],
                expected_revision=replaced["result"]["content_revision"],
                idempotency_key="hermes-write-003",
            ),
        }
    )

    assert removed["ok"] is True
    tombstone = removed["result"]["record"]
    assert tombstone["status"] == "removed"
    assert replacement_text not in json.dumps(tombstone, ensure_ascii=False)
    successor_record = runtime.store.get_by_id(successor["record_id"], scope=successor["scope"])
    assert successor_record is not None and successor_record.status == "removed"
    bundle = runtime.memory.recall(query="primary sources archive citations", scope=successor["scope"])
    assert all(item.record_id not in {old["record_id"], successor["record_id"]} for item in bundle.items)


def test_runtime_adapter_rpc_fails_closed_for_reused_key_stale_or_inactive_target(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)
    added = bridge.handle({"method": "adapter.mutate_memory", "params": _hermes_mutation_params()})
    record = added["result"]["record"]
    revision = added["result"]["content_revision"]

    conflict = bridge.handle(
        {"method": "adapter.mutate_memory", "params": _hermes_mutation_params(content="different content")}
    )
    stale = bridge.handle(
        {
            "method": "adapter.mutate_memory",
            "params": _hermes_mutation_params(
                action="replace", content="replacement", target_record_id=record["record_id"],
                expected_revision="0" * 64, idempotency_key="hermes-write-stale",
            ),
        }
    )
    removed = bridge.handle(
        {
            "method": "adapter.mutate_memory",
            "params": _hermes_mutation_params(
                action="remove", content="", target_record_id=record["record_id"],
                expected_revision=revision, idempotency_key="hermes-write-remove",
            ),
        }
    )
    inactive = bridge.handle(
        {
            "method": "adapter.mutate_memory",
            "params": _hermes_mutation_params(
                action="replace", content="replacement", target_record_id=record["record_id"],
                expected_revision=revision, idempotency_key="hermes-write-inactive",
            ),
        }
    )

    assert conflict["error"] == "mutation_idempotency_conflict"
    assert stale["error"] == "mutation_stale_revision"
    assert removed["ok"] is True
    assert inactive["error"] == "mutation_target_inactive"


def test_runtime_adapter_rpc_rejects_cross_source_and_unknown_provenance(tmp_path: Path) -> None:
    bridge = EIBrainRPCBridge(Runtime.create(root=tmp_path))
    added = bridge.handle({"method": "adapter.mutate_memory", "params": _hermes_mutation_params()})
    record = added["result"]["record"]
    revision = added["result"]["content_revision"]

    cross_source = bridge.handle(
        {
            "method": "adapter.mutate_memory",
            "params": _hermes_mutation_params(
                action="replace", content="replacement", source_id="other-source",
                target_record_id=record["record_id"], expected_revision=revision,
                idempotency_key="hermes-write-cross-source",
            ),
        }
    )
    bad_provenance = bridge.handle(
        {
            "method": "adapter.mutate_memory",
            "params": _hermes_mutation_params(
                idempotency_key="hermes-write-bad-provenance", provenance={"source_id": "not-allowed"},
            ),
        }
    )

    assert cross_source["error"] == "mutation_target_scope_mismatch"
    assert bad_provenance["error"] == "invalid_request"


def test_runtime_adapter_rpc_allows_only_one_concurrent_successor_and_rolls_back_faults(tmp_path: Path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)
    added = bridge.handle({"method": "adapter.mutate_memory", "params": _hermes_mutation_params()})
    record = added["result"]["record"]
    revision = added["result"]["content_revision"]
    responses: list[dict] = []

    def replace(key: str) -> None:
        responses.append(
            bridge.handle(
                {"method": "adapter.mutate_memory", "params": _hermes_mutation_params(
                    action="replace", content="concurrent replacement", target_record_id=record["record_id"],
                    expected_revision=revision, idempotency_key=key,
                )}
            )
        )

    first = threading.Thread(target=replace, args=("hermes-write-race-1",))
    second = threading.Thread(target=replace, args=("hermes-write-race-2",))
    first.start(); second.start(); first.join(); second.join()

    assert sum(response["ok"] is True for response in responses) == 1
    assert sum(response.get("error") == "mutation_target_inactive" for response in responses) == 1
    successor = next(response["result"]["record"] for response in responses if response["ok"] is True)
    original_upsert = runtime.store.sqlite.upsert
    calls = 0

    def fail_after_successor(record_to_write, *, commit=True):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected target write fault")
        return original_upsert(record_to_write, commit=commit)

    monkeypatch.setattr(runtime.store.sqlite, "upsert", fail_after_successor)
    with __import__("pytest").raises(OSError, match="injected target write fault"):
        bridge.handle(
            {"method": "adapter.mutate_memory", "params": _hermes_mutation_params(
                action="remove", content="", target_record_id=successor["record_id"],
                expected_revision=next(response["result"]["content_revision"] for response in responses if response["ok"] is True),
                idempotency_key="hermes-write-fault",
            )}
        )
    restored = runtime.store.get_by_id(successor["record_id"], scope=successor["scope"])
    assert restored is not None and restored.status == "active"
    assert runtime.store.count_records(kinds=["memory"], scope=successor["scope"], status="removed") == 0


def test_runtime_adapter_rpc_status_derives_channel_scope(tmp_path: Path) -> None:
    bridge = EIBrainRPCBridge(Runtime.create(root=tmp_path))

    response = bridge.handle(
        {"method": "adapter.status", "params": {"channel": "codex", "scope": BASE_SCOPE}}
    )

    assert response["ok"] is True
    assert response["result"]["adapter_contract_version"] == RUNTIME_ADAPTER_CONTRACT_VERSION
    assert response["result"]["scope"]["workspace_id"] == "embodied::channel::codex"
    assert response["result"]["authority_mode"] == "per_channel"


def test_runtime_adapter_rpc_remember_and_prefetch_stay_in_channel(tmp_path: Path) -> None:
    bridge = EIBrainRPCBridge(Runtime.create(root=tmp_path))
    remember = bridge.handle(
        {
            "method": "adapter.remember",
            "params": {
                "channel": "hermes",
                "scope": BASE_SCOPE,
                "event_id": "hermes-explicit-1",
                "text": "Always include primary evidence when Hermes writes a durable research conclusion.",
                "memory_type": "preference",
            },
        }
    )
    prefetch = bridge.handle(
        {
            "method": "adapter.prefetch",
            "params": {
                "channel": "hermes",
                "scope": BASE_SCOPE,
                "query": "Hermes primary evidence durable research conclusion",
                "task_type": "research.summary",
            },
        }
    )
    codex = bridge.handle(
        {
            "method": "adapter.prefetch",
            "params": {
                "channel": "codex",
                "scope": BASE_SCOPE,
                "query": "Hermes primary evidence durable research conclusion",
                "task_type": "code.review",
            },
        }
    )

    assert remember["ok"] is True
    assert prefetch["result"]["bundle"]["items"][0]["record_id"] == remember["result"]["record"]["record_id"]
    assert codex["result"]["bundle"]["items"] == []


def test_runtime_adapter_rpc_rejects_invalid_channel_and_terminal_types(tmp_path: Path) -> None:
    bridge = EIBrainRPCBridge(Runtime.create(root=tmp_path))

    invalid_channel = bridge.handle(
        {"method": "adapter.status", "params": {"channel": "other", "scope": BASE_SCOPE}}
    )
    invalid_terminal = bridge.handle(
        {
            "method": "adapter.record_terminal",
            "params": {
                "channel": "codex",
                "scope": BASE_SCOPE,
                "end_kind": "agent_end",
                "session_id": "s1",
                "event_id": "t1",
                "task_type": "code.fix",
                "success": True,
            },
        }
    )

    assert invalid_channel == {
        "contract_version": EIMEMORY_RPC_CONTRACT_VERSION,
        "ok": False,
        "error": "invalid_request",
    }
    assert invalid_terminal["ok"] is False
    assert invalid_terminal["error"] == "invalid_request"


def test_runtime_http_client_calls_authenticated_rpc(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path / "store")
    server = EIBrainRPCServer(
        runtime,
        host="127.0.0.1",
        port=0,
        auth_token=AUTH_TOKEN,
    )
    server.start()
    try:
        client = AgentRuntimeRPCClient(
            base_url=f"http://{server.address[0]}:{server.address[1]}/",
            auth_token=AUTH_TOKEN,
            timeout_seconds=1.0,
            failure_ledger_path=tmp_path / "failures.jsonl",
        )
        response = client.call_or_bypass(
            "adapter.status",
            {"channel": "codex", "scope": BASE_SCOPE},
        )
        mutation = client.call_or_bypass(
            "adapter.mutate_memory",
            _hermes_mutation_params(idempotency_key="hermes-http-auth-001"),
        )
    finally:
        server.stop()

    assert response["ok"] is True
    assert response["bypassed"] is False
    assert response["result"]["channel"] == "codex"
    assert mutation["ok"] is True
    assert mutation["result"]["action"] == "add"
    assert not (tmp_path / "failures.jsonl").exists()


def test_runtime_http_attestation_requires_separate_producer_credential_and_channel_match(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(
        "EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY",
        "RuntimeReceiptEvidenceKey_0123456789-Strong",
    )
    runtime = Runtime.create(root=tmp_path / "store")
    server = EIBrainRPCServer(
        runtime,
        host="127.0.0.1",
        port=0,
        auth_token=AUTH_TOKEN,
        attestation_tokens={
            CODEX_ATTESTATION_TOKEN: "codex",
            HERMES_ATTESTATION_TOKEN: "hermes",
        },
    )
    server.start()
    payload = {
        "method": "adapter.attest_tool_result",
        "params": {
            "channel": "codex", "scope": BASE_SCOPE, "session_id": "s1",
            "run_id": "r1", "tool_call_id": "c1", "tool_name": "pytest",
            "result": {"exit_code": 0, "summary": "2 passed"},
        },
    }

    def post(token: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://{server.address[0]}:{server.address[1]}/",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    try:
        normal_status, normal = post(AUTH_TOKEN, payload)
        mismatch_status, mismatch = post(
            CODEX_ATTESTATION_TOKEN,
            {**payload, "params": {**payload["params"], "channel": "hermes"}},
        )
        codex_status, codex = post(CODEX_ATTESTATION_TOKEN, payload)
    finally:
        server.stop()
        runtime.close()

    assert (normal_status, normal["error"]) == (401, "attestation_unauthorized")
    assert (mismatch_status, mismatch["error"]) == (400, "invalid_request")
    assert codex_status == 200
    assert codex["result"]["receipt"]["channel"] == "codex"


def test_runtime_http_client_bypasses_and_opens_bounded_circuit(tmp_path: Path) -> None:
    ledger = tmp_path / "failures.jsonl"
    client = AgentRuntimeRPCClient(
        base_url="http://127.0.0.1:1/",
        auth_token=AUTH_TOKEN,
        timeout_seconds=0.05,
        failure_ledger_path=ledger,
        circuit_failure_threshold=2,
        circuit_reset_seconds=60.0,
        max_failure_ledger_bytes=2_048,
    )

    first = client.call_or_bypass("adapter.status", {"channel": "codex", "scope": BASE_SCOPE})
    second = client.call_or_bypass("adapter.status", {"channel": "codex", "scope": BASE_SCOPE})
    third = client.call_or_bypass("adapter.status", {"channel": "codex", "scope": BASE_SCOPE})

    assert first == {
        "ok": False,
        "bypassed": True,
        "error": "adapter_unavailable",
        "result": None,
    }
    assert second["error"] == "adapter_unavailable"
    assert third["error"] == "circuit_open"
    assert ledger.stat().st_size <= 2_048
    entries = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 3
    assert all(AUTH_TOKEN not in json.dumps(entry) for entry in entries)
    assert entries[-1]["error"] == "circuit_open"


def test_runtime_http_client_ledger_failure_cannot_break_fail_open(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("file", encoding="utf-8")
    client = AgentRuntimeRPCClient(
        base_url="http://127.0.0.1:1/",
        auth_token=AUTH_TOKEN,
        timeout_seconds=0.05,
        failure_ledger_path=blocked_parent / "failures.jsonl",
    )

    result = client.call_or_bypass("adapter.status", {"channel": "codex", "scope": BASE_SCOPE})

    assert result == {
        "ok": False,
        "bypassed": True,
        "error": "adapter_unavailable",
        "result": None,
    }


def test_runtime_http_client_never_writes_an_oversized_failure_entry(tmp_path: Path) -> None:
    ledger = tmp_path / "failures.jsonl"
    client = AgentRuntimeRPCClient(
        base_url="http://127.0.0.1:1/",
        auth_token=AUTH_TOKEN,
        timeout_seconds=0.05,
        failure_ledger_path=ledger,
        max_failure_ledger_bytes=1_024,
    )

    result = client.call_or_bypass("adapter." + "x" * 5_000, {})

    assert result["bypassed"] is True
    assert not ledger.exists() or ledger.stat().st_size <= 1_024


def test_runtime_http_client_rejects_oversized_rpc_response(tmp_path: Path, monkeypatch) -> None:
    class OversizedResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, size: int = -1) -> bytes:
            return b"x" * (size if size >= 0 else 10_000)

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: OversizedResponse())
    client = AgentRuntimeRPCClient(
        base_url="http://127.0.0.1:8091/",
        auth_token=AUTH_TOKEN,
        failure_ledger_path=tmp_path / "failures.jsonl",
        max_response_bytes=1_024,
    )

    result = client.call_or_bypass("adapter.status", {"channel": "codex", "scope": BASE_SCOPE})

    assert result["bypassed"] is True
    assert result["error"] == "adapter_unavailable"
