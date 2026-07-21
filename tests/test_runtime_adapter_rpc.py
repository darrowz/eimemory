from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
import urllib.request

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
    finally:
        server.stop()

    assert response["ok"] is True
    assert response["bypassed"] is False
    assert response["result"]["channel"] == "codex"
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
