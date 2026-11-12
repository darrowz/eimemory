"""Run the real HTTP parser/router with in-memory streams, without sockets."""
from email.message import Message
from io import BytesIO
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

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
        assert set(diagnostic) == {'ts', 'method', 'error', 'exception_class', 'stage', 'exception_family'}
        assert diagnostic['stage'] == 'bridge_dispatch'
        assert diagnostic['exception_family'] == ('io' if isinstance(error, OSError) else 'runtime')
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


@pytest.mark.parametrize('base,family', [(Exception, 'unknown'), (RuntimeError, 'runtime'),
                                        (sqlite3.Error, 'sqlite')])
def test_diagnostics_are_bounded_and_do_not_record_custom_exception_names(tmp_path, monkeypatch, base, family):
    from eimemory.adapters.eibrain import rpc_server
    monkeypatch.setattr(rpc_server, 'MAX_RPC_ERROR_LOG_BYTES', 1024)
    private_type = type('synthetic_private_exception_name', (base,), {})
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
        assert all(row['exception_class'] == 'Exception' and row['stage'] == 'bridge_dispatch'
                   and row['exception_family'] == family for row in rows)
        assert b'synthetic_private' not in data and b'synthetic-private' not in data


def test_attestation_insert_is_rolled_back_when_eligibility_update_fails(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'synthetic-receipt-signing-key-0123456789-ABCDEFG')
    with Runtime.create(root=tmp_path) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        conn = runtime.store.sqlite.conn
        # The real second persistence statement fails, after INSERT succeeded.
        conn.set_authorizer(lambda operation, table, *_args: (
            sqlite3.SQLITE_DENY if operation == sqlite3.SQLITE_UPDATE
            and table == 'adapter_tool_receipts' else sqlite3.SQLITE_OK
        ))
        try:
            assert post(runtime, bridge, request()) == (500, {'ok': False, 'error': 'internal_error'})
        finally:
            conn.set_authorizer(None)
        assert not conn.in_transaction
        assert conn.execute('SELECT COUNT(*) FROM adapter_tool_receipts').fetchone()[0] == 0
        diagnostic = json.loads((tmp_path / PATH).read_text().splitlines()[-1])
        assert diagnostic['exception_class'] == 'DatabaseError'
        assert 'synthetic-private' not in (tmp_path / PATH).read_text()
        # A failed attempt must not become an idempotent success from a dirty connection.
        status, body = post(runtime, bridge, request())
        assert status == 200 and body['result']['idempotent'] is False
        assert post(runtime, bridge, request())[1]['result']['idempotent'] is True
        assert conn.execute('SELECT COUNT(*) FROM adapter_tool_receipts').fetchone()[0] == 1


def test_attestation_does_not_commit_or_rollback_callers_transaction(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'synthetic-receipt-signing-key-0123456789-ABCDEFG')
    with Runtime.create(root=tmp_path) as runtime:
        bridge = EIBrainRPCBridge(runtime)
        with runtime.store.locked() as sqlite:
            sqlite.execute('BEGIN IMMEDIATE')
            try:
                assert post(runtime, bridge, request()) == (500, {'ok': False, 'error': 'internal_error'})
                assert sqlite.in_transaction
                assert sqlite.conn.execute('SELECT COUNT(*) FROM adapter_tool_receipts').fetchone()[0] == 0
            finally:
                sqlite.rollback()


@pytest.mark.parametrize('separate_connections', [False, True])
def test_duplicate_concurrent_attestations_publish_one_receipt(tmp_path, monkeypatch, separate_connections):
    from eimemory.adapters.runtime import service
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'synthetic-receipt-signing-key-0123456789-ABCDEFG')
    first = Runtime.create(root=tmp_path)
    second = Runtime.create(root=tmp_path) if separate_connections else first
    barriers = Barrier(2)
    original_sign = service.sign_tool_receipt

    def sign(receipt):
        signed = original_sign(receipt)
        barriers.wait(timeout=5)
        return signed

    monkeypatch.setattr(service, 'sign_tool_receipt', sign)
    try:
        bridges = [EIBrainRPCBridge(first), EIBrainRPCBridge(second)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda pair: post(*pair, request()), zip([first, second], bridges)))
        assert [status for status, _ in replies] == [200, 200]
        results = [body['result'] for _, body in replies]
        assert sorted(result['idempotent'] for result in results) == [False, True]
        assert results[0]['receipt'] == results[1]['receipt']
        assert first.store.sqlite.conn.execute('SELECT COUNT(*) FROM adapter_tool_receipts').fetchone()[0] == 1
        assert not first.store.sqlite.in_transaction and not second.store.sqlite.in_transaction
    finally:
        if second is not first:
            second.close()
        first.close()


@pytest.mark.parametrize('token,payload', [('invalid', request()), (PUBLIC, request()),
                                         (HOST, {'method': 'adapter.status', 'params': {}})])
def test_denial_write_failure_has_response_write_stage(tmp_path, monkeypatch, token, payload):
    original_post = post
    # Reuse the real router and request builder, injecting a failed denial write.
    original_do_post = _RPCHandler.do_POST
    def do_post(handler):
        send = handler._send_json
        def fail_denial(status, body):
            if status == 401:
                raise OSError('synthetic-private-denial-write')
            send(status, body)
        handler._send_json = fail_denial
        original_do_post(handler)
    monkeypatch.setattr(_RPCHandler, 'do_POST', do_post)
    with Runtime.create(root=tmp_path) as runtime:
        assert original_post(runtime, EIBrainRPCBridge(runtime), payload, token) == (
            500, {'ok': False, 'error': 'internal_error'})
    data = (tmp_path / PATH).read_text()
    diagnostic = json.loads(data.splitlines()[-1])
    assert diagnostic['stage'] == 'response_write'
    assert 'synthetic-private' not in data and PUBLIC not in data and HOST not in data
