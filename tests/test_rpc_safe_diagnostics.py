"""Run the real HTTP parser/router with in-memory streams, without sockets."""
from email.message import Message
from io import BytesIO
import json

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.eibrain.rpc_server import _RPCHandler
from eimemory.api.runtime import Runtime

PUBLIC = 'SyntheticPublicCredential_0123456789'
HOST = 'SyntheticHostCredential_9876543210'
PATH = 'logs/rpc-errors.jsonl'


def request():
    return {'method': 'adapter.attest_tool_result', 'params': {
        'channel': 'hermes', 'scope': {'tenant_id': 'fixture', 'agent_id': 'fixture',
        'workspace_id': 'fixture', 'user_id': 'fixture-user'}, 'session_id': 'fixture-session',
        'run_id': 'fixture-turn', 'tool_call_id': 'fixture-call', 'tool_name': 'terminal',
        'tool_input': {'command': 'synthetic-private-input'},
        'result': {'exit_code': 0, 'output': 'synthetic-private-result'}, 'duration_ms': 1}}


def post(runtime, bridge, payload, token=HOST):
    handler = object.__new__(_RPCHandler)
    handler.bridge, handler.runtime = bridge, runtime
    handler.auth_token = PUBLIC
    handler.attestation_tokens = {HOST: 'hermes'}
    raw = json.dumps(payload).encode()
    handler.headers = Message()
    handler.headers['Authorization'] = 'Bearer ' + token
    handler.headers['Content-Length'] = str(len(raw))
    handler.rfile = BytesIO(raw)
    replies = []
    handler._send_json = lambda status, body: replies.append((status, body))
    handler.do_POST()
    return replies[0]


@pytest.mark.parametrize('error', [RuntimeError('synthetic-private-exception'),
                                 OSError('synthetic-private-exception')])
def test_attestation_500_has_bounded_safe_diagnostic(tmp_path, monkeypatch, error):
    with Runtime.create(root=tmp_path) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        def fail(**kwargs):
            raise error
        monkeypatch.setattr(bridge.runtime_adapter, 'attest_tool_result', fail)
        status, body = post(runtime, bridge, request())
        assert status == 500 and body == {'ok': False, 'error': 'internal_error'}
        path = tmp_path / PATH
        assert path.exists()
        diagnostic = json.loads(path.read_text().splitlines()[-1])
        assert diagnostic['method'] == 'adapter.attest_tool_result'
        assert diagnostic['error'] == 'internal_error'
        assert diagnostic['exception_class'] == type(error).__name__
        assert set(diagnostic) == {'ts', 'method', 'error', 'exception_class'}
        assert all(private not in path.read_text() for private in
                   (PUBLIC, HOST, 'synthetic-private', 'fixture-user', 'fixture-call'))


def test_real_attestation_and_rejection_contracts_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'synthetic-receipt-signing-key-0123456789-ABCDEFG')
    with Runtime.create(root=tmp_path) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        status, body = post(runtime, bridge, request())
        assert status == 200 and body['ok'] is True
        assert body['result']['receipt']['channel'] == 'hermes'
        assert post(runtime, bridge, request(), PUBLIC)[0] == 401
        wrong = request()
        wrong['params']['channel'] = 'codex'
        assert post(runtime, bridge, wrong)[0] == 400
        wrong = request()
        wrong['params']['duration_ms'] = True
        assert post(runtime, bridge, wrong)[0] == 400
        assert not (tmp_path / PATH).exists()


def test_diagnostic_write_failure_preserves_original_500(tmp_path, monkeypatch):
    with Runtime.create(root=tmp_path) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        def fail(**kwargs):
            raise RuntimeError('synthetic-private-exception')
        monkeypatch.setattr(bridge.runtime_adapter, 'attest_tool_result', fail)
        (tmp_path / 'logs').mkdir(exist_ok=True)
        (tmp_path / PATH).mkdir()
        assert post(runtime, bridge, request()) == (500, {'ok': False, 'error': 'internal_error'})


def test_diagnostics_are_bounded_and_do_not_record_custom_exception_names(tmp_path, monkeypatch):
    from eimemory.adapters.eibrain import rpc_server
    monkeypatch.setattr(rpc_server, 'MAX_RPC_ERROR_LOG_BYTES', 1024)
    private_type = type('synthetic_private_exception_name', (Exception,), {})
    with Runtime.create(root=tmp_path) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        def fail(**kwargs):
            raise private_type('synthetic-private-exception')
        monkeypatch.setattr(bridge.runtime_adapter, 'attest_tool_result', fail)
        for _ in range(12):
            assert post(runtime, bridge, request())[0] == 500
        data = (tmp_path / PATH).read_bytes()
        assert len(data) <= 1024
        rows = [json.loads(line) for line in data.splitlines()]
        assert 0 < len(rows) < 12
        assert all(row['exception_class'] == 'Exception' for row in rows)
        assert b'synthetic_private' not in data and b'synthetic-private' not in data
