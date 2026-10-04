"""Synthetic parsing and in-memory worker-pool state, never real completions."""
import json
from types import SimpleNamespace

import pytest

from eimemory.llm.command_client import _parse_completion_payload
from eimemory.llm.hermes_adapter import _extract_json_payload
from eimemory.llm.openclaw_adapter import _parse_response
from eimemory.llm import gateway_pool


@pytest.mark.parametrize("payload", [{"items": [1, 2], "count": 2}, {"message": "[brackets]"}, [{"items": [1]}]])
def test_hermes_preserves_outer_json_container(payload):
    encoded = json.dumps(payload)
    assert json.loads(_extract_json_payload("```json\n" + encoded + "\n```")) == payload


@pytest.mark.parametrize("key", ["text", "provider_id", "model_id"])
@pytest.mark.parametrize("value", [[], {}, 17, True, None, "   "])
def test_command_and_gateway_require_nonempty_string_fields(key, value):
    payload = {"text": "answer", "provider_id": "provider", "model_id": "model", key: value}
    with pytest.raises(ValueError):
        _parse_completion_payload(payload)
    with pytest.raises(ValueError):
        gateway_pool._parse_result(payload)


@pytest.mark.parametrize("field,value", [("text", []), ("provider", 17), ("model", {})])
def test_openclaw_does_not_stringify_invalid_response_fields(field, value):
    response = {"ok": True, "outputs": [{"text": "answer"}], "provider": "provider", "model": "model"}
    if field == "text":
        response["outputs"][0][field] = value
    else:
        response[field] = value
    with pytest.raises(ValueError):
        _parse_response(response)


@pytest.mark.parametrize("invalid", [[], {}, None, 17])
def test_gateway_error_fields_normalize_without_type_error(invalid):
    error = gateway_pool.GatewayCompletionError(invalid, {"gateway_stage": invalid})
    assert error.reason == "gateway_error"
    assert "gateway_stage" not in error.diagnostics


def test_valid_completion_fields_are_trimmed():
    result = gateway_pool._parse_result({"text": " answer ", "provider_id": " provider ", "model_id": " model "})
    assert (result.text, result.provider_id, result.model_id) == ("answer", "provider", "model")


class FakeWorker:
    def __init__(self, closed=False):
        self.closed = closed
        self.process = SimpleNamespace(poll=lambda: 1 if self.closed else None)
    def close(self):
        self.closed = True


def test_failed_replacement_restores_previously_dequeued_live_worker(monkeypatch):
    pool = gateway_pool._Pool(["unused"])
    live, dead = FakeWorker(), FakeWorker(closed=True)
    pool.workers[:] = [live, dead]
    pool.available.put_nowait(live)
    pool.available.put_nowait(dead)
    def fail(argv):
        raise OSError("synthetic creation failure")
    monkeypatch.setattr(gateway_pool, "_Worker", fail)
    with pytest.raises(OSError):
        pool.warm()
    assert pool.workers == [live]
    assert pool.available.get_nowait() is live


def test_all_dead_pool_can_warm_again_after_creation_failure(monkeypatch):
    pool = gateway_pool._Pool(["unused"])
    dead = FakeWorker(closed=True)
    pool.workers.append(dead)
    pool.available.put_nowait(dead)
    def fail(argv):
        raise OSError("synthetic creation failure")
    monkeypatch.setattr(gateway_pool, "_Worker", fail)
    with pytest.raises(OSError):
        pool.warm()
    assert pool.workers == []
    replacement = FakeWorker()
    monkeypatch.setattr(gateway_pool, "_Worker", lambda argv: replacement)
    pool.warm()
    assert pool.workers == [replacement]
    assert pool.available.get_nowait() is replacement


@pytest.mark.parametrize("text", ['[{"id":1},]', '{"items":[1],}'])
def test_hermes_does_not_salvage_inner_json_from_malformed_outer_container(text):
    result = _extract_json_payload(text)
    assert result == text
    with pytest.raises(json.JSONDecodeError):
        json.loads(result)


@pytest.mark.parametrize("timeout,expected", [(1.9, 1), (0, 1), (900, 600), (90, 90)])
def test_bridge_timeout_normalizes_like_command_client(timeout, expected):
    from eimemory.llm.bridge_pool import BridgePoolClient
    client = BridgePoolClient(["unused"], identity_key="fixture", timeout_seconds=timeout)
    assert client.timeout_seconds == expected
