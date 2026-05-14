import json
import urllib.request
from pathlib import Path

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.eibrain.rpc_server import EIBrainRPCServer
from eimemory.api.runtime import Runtime
from eimemory.ei_bridge.protocol import EIMEMORY_RPC_CONTRACT_VERSION


def test_eibrain_rpc_root_payload_includes_contract_version(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    server = EIBrainRPCServer(runtime, host="127.0.0.1", port=0)
    server.start()
    try:
        with urllib.request.urlopen(f"http://{server.address[0]}:{server.address[1]}/", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.stop()

    assert payload["ok"] is True
    assert payload["service"] == "eimemory-rpc"
    assert payload["contract_version"] == EIMEMORY_RPC_CONTRACT_VERSION


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
    assert "news_digest" not in payload


def test_eibrain_rpc_bridge_errors_always_return_contract_version(tmp_path: Path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)

    payload = bridge.handle({"method": "nope", "params": {}})

    assert payload["ok"] is False
    assert payload["contract_version"] == EIMEMORY_RPC_CONTRACT_VERSION
