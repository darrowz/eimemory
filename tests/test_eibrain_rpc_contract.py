import json
import urllib.request
from pathlib import Path

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.api.runtime import Runtime
from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION


TEST_RPC_AUTH_TOKEN = "Abcdefghijklmnopqrstuvwxyz012345_-"


def _authorized_get(server: EIBrainRPCServer, path: str):
    request = urllib.request.Request(
        f"http://{server.address[0]}:{server.address[1]}{path}",
        headers={"Authorization": f"Bearer {TEST_RPC_AUTH_TOKEN}"},
    )
    return urllib.request.urlopen(request, timeout=5)


def test_eibrain_rpc_root_payload_includes_contract_version(tmp_path: Path, monkeypatch) -> None:
    runtime = Runtime.create(root=tmp_path)
    monkeypatch.setattr(
        runtime,
        "build_daily_brief",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("the compact RPC root must not build a daily brief")
        ),
    )
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0, auth_token=TEST_RPC_AUTH_TOKEN)
    server.start()
    try:
        with _authorized_get(server, "/") as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["service"] == "eimemory-rpc"
    assert payload["contract_version"] == EIMEMORY_RPC_CONTRACT_VERSION
    assert "news_digest" not in payload
    assert "research_digest" not in payload


def test_eibrain_rpc_healthz_returns_compact_contract_payload(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0)
    server.start()
    try:
        with urllib.request.urlopen(f"http://{server.address[0]}:{server.address[1]}/healthz", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["service"] == "eimemory-rpc"
    assert payload["contract_version"] == EIMEMORY_RPC_CONTRACT_VERSION
    assert payload["checks"]["ready"] is True
    assert "news_digest" not in payload


def test_eibrain_rpc_health_returns_compact_payload(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0)
    server.start()
    try:
        with urllib.request.urlopen(f"http://{server.address[0]}:{server.address[1]}/health", timeout=5) as response:
            body = response.read()
            payload = json.loads(body.decode("utf-8"))
    finally:
        server.stop()

    # Runtime paths are intentionally included as deployment identity evidence;
    # their host-dependent length must not make the compact contract flaky.
    assert len(body) < 1024
    assert payload["ok"] is True
    assert payload["checks"]["process"] is True
    assert "research_digest" not in payload


def test_eibrain_rpc_daily_brief_keeps_diagnostic_payload(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0, auth_token=TEST_RPC_AUTH_TOKEN)
    server.start()
    try:
        with _authorized_get(server, "/daily-brief") as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert "research_digest" in payload
    assert "source_health" in payload


def test_eibrain_rpc_bridge_errors_always_return_contract_version(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)

    payload = bridge.handle({"method": "nope", "params": {}})

    assert payload["ok"] is False
    assert payload["contract_version"] == EIMEMORY_RPC_CONTRACT_VERSION
