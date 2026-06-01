from __future__ import annotations

import py_compile

import eimemory.experience
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.api.runtime import Runtime


def test_eibrain_rpc_records_outcome_trace(monkeypatch, tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)
    calls: list[tuple[object, dict, dict | None]] = []

    def _record_outcome_trace(runtime_arg, payload, *, scope=None):
        calls.append((runtime_arg, payload, scope))
        return {"ok": True, "record_id": "outcome-trace-1"}

    monkeypatch.setattr(eimemory.experience, "record_outcome_trace", _record_outcome_trace, raising=False)

    response = bridge.handle(
        {
            "method": "experience.record_outcome_trace",
            "params": {
                "scope": {"agent_id": "honxin", "workspace_id": "honjia", "user_id": "darrow"},
                "payload": {"trace_id": "trace-1", "outcome": "success"},
            },
        }
    )

    assert response["ok"] is True
    assert response["result"] == {"ok": True, "record_id": "outcome-trace-1"}
    assert calls == [
        (
            runtime,
            {"trace_id": "trace-1", "outcome": "success"},
            {"tenant_id": "default", "agent_id": "hongtu", "workspace_id": "embodied", "user_id": "darrow"},
        )
    ]


def test_eibrain_rpc_rejects_invalid_outcome_trace_payload(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    bridge = EIBrainRPCBridge(runtime)

    response = bridge.handle(
        {
            "method": "experience.record_outcome_trace",
            "params": {
                "scope": {"agent_id": "honxin", "workspace_id": "honjia"},
                "payload": "not-an-object",
            },
        }
    )

    assert response["ok"] is False
    assert response["error"] == "invalid_request"


def test_record_outcome_trace_script_compiles() -> None:
    py_compile.compile("scripts/record_outcome_trace.py", doraise=True)
