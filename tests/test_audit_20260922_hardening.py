"""Boundary regressions for the 2e57f59 audit; no network/production side effects."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import errno
import gc
import http.client
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
import traceback
from urllib.error import HTTPError, URLError

import pytest

from eimemory.storage import atomic_file as state
from eimemory.storage.bounded_jsonl import append_bounded_jsonl
from eimemory.adapters.runtime.circuit_breaker import CircuitBreaker
from eimemory.adapters.runtime import http_client as rpc
from eimemory.intake import safe_transport as transport
from eimemory.scheduler import result_contract as nightly


@pytest.mark.parametrize('raw', [
    '{"approved":false,"approved":true}', '{"x":NaN}', '{"x":Infinity}',
    '{"x":-Infinity}', '{"x":1e309}', '{"nested":{"x":1,"x":2}}',
    '{"x":', b'{"x":"\xff"}',
])
def test_state_rejects_ambiguous_json(tmp_path, raw):
    path = tmp_path / 'state.json'
    path.write_bytes(raw.encode() if isinstance(raw, str) else raw)
    with pytest.raises(ValueError):
        state.read_json_strict(path, dict)


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), object(), '\ud800'])
def test_invalid_write_preserves_prior_state(tmp_path, value):
    path = tmp_path / 'state.json'
    path.write_bytes(b'{"old":true}\n')
    with pytest.raises(ValueError):
        state.atomic_write_json(path, {'value': value})
    assert path.read_bytes() == b'{"old":true}\n'
    assert not list(tmp_path.glob('.state.json.*'))


@pytest.mark.parametrize('limits', [dict(max_bytes=0), dict(max_bytes=True), dict(max_depth=0), dict(max_depth=1.5)])
def test_invalid_state_limits_have_no_filesystem_side_effect(tmp_path, limits):
    path = tmp_path / 'missing' / 'state.json'
    with pytest.raises(ValueError):
        state.atomic_write_json(path, {}, **limits)
    assert not path.parent.exists()


def test_state_roundtrip_unicode_permissions_and_default(tmp_path):
    path = tmp_path / 'state.json'
    default = {'items': []}
    state.locked_json_update(path, lambda x: {'items': x['items'] + ['中文']}, default=default)
    assert default == {'items': []}
    assert state.read_json_strict(path, dict) == {'items': ['中文']}
    if os.name == 'posix':
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.with_name('state.json.lock').stat().st_mode) == 0o600


def test_state_byte_and_depth_caps(tmp_path):
    path = tmp_path / 'state.json'
    path.write_text('{"中文":"内容"}')
    with pytest.raises(ValueError):
        state.read_json_strict(path, dict, max_bytes=len(path.read_text()))
    with pytest.raises(ValueError):
        state.atomic_write_json(path, {'a': {'b': {}}}, max_depth=2)
    state.atomic_write_json(path, {'a': {'b': {}}}, max_depth=3)
    with pytest.raises(ValueError):
        state.read_json_strict(path, dict, max_depth=2)
    prior = path.read_bytes()
    with pytest.raises(ValueError):
        state.atomic_write_json(path, {'x': 'x' * 200}, max_bytes=32)
    assert path.read_bytes() == prior


def test_state_size_cap_checked_before_read(tmp_path, monkeypatch):
    from contextlib import contextmanager
    path = tmp_path / 'large.json'
    path.write_bytes(b' ' * 128)
    real = state.open_regular_binary
    class NoRead:
        def __init__(self, handle): self.handle = handle
        def fileno(self): return self.handle.fileno()
        def read(self, *a): raise AssertionError('oversized input was read')
    @contextmanager
    def wrapped(p):
        with real(p) as h:
            yield NoRead(h)
    monkeypatch.setattr(state, 'open_regular_binary', wrapped)
    with pytest.raises(ValueError):
        state.read_json_strict(path, dict, max_bytes=64)


def test_state_wrong_type_and_bad_mutation_are_not_published(tmp_path):
    path = tmp_path / 'state.json'
    state.atomic_write_json(path, [])
    with pytest.raises(ValueError): state.read_json_strict(path, dict)
    state.atomic_write_json(path, {})
    with pytest.raises(TypeError): state.locked_json_update(path, lambda _: [])
    assert state.read_json_strict(path, dict) == {}


@pytest.mark.skipif(os.name != 'posix', reason='POSIX link and FIFO contracts')
@pytest.mark.parametrize('kind', ['symlink', 'dangling_symlink', 'hardlink', 'fifo', 'directory'])
def test_unsafe_state_and_ledger_leaves_are_rejected(tmp_path, kind):
    victim = tmp_path / 'victim'
    victim.write_bytes(b'{"sentinel":true}\n')
    path = tmp_path / 'unsafe'
    if kind == 'symlink': path.symlink_to(victim)
    elif kind == 'dangling_symlink': path.symlink_to(tmp_path / 'absent')
    elif kind == 'hardlink': os.link(victim, path)
    elif kind == 'fifo': os.mkfifo(path)
    else: path.mkdir()
    with pytest.raises((OSError, ValueError)): state.read_json_strict(path, dict)
    with pytest.raises((OSError, ValueError)): state.atomic_write_json(path, {'bad': True})
    with pytest.raises((OSError, ValueError)): append_bounded_jsonl(path, {'bad': True}, max_bytes=1024)
    with pytest.raises((OSError, ValueError)):
        with state.interprocess_lock(path): pytest.fail('unsafe lock acquired')
    assert victim.read_bytes() == b'{"sentinel":true}\n'
    assert not (tmp_path / 'absent').exists()


@pytest.mark.skipif(os.name != 'posix', reason='POSIX links')
def test_dangling_state_link_does_not_run_mutator(tmp_path):
    path = tmp_path / 'state.json'
    path.symlink_to(tmp_path / 'absent')
    calls = []
    with pytest.raises(ValueError):
        state.locked_json_update(path, lambda x: calls.append(x) or {}, default={})
    assert not calls


@pytest.mark.skipif(os.name != 'posix', reason='POSIX links')
def test_leaf_substitution_during_open_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    path.write_text('{}')
    victim = tmp_path / 'victim'; victim.write_text('{"secret":1}')
    real_open = state.os.open
    def switched(p, flags, mode=0o777, **kwargs):
        if Path(p) == path:
            path.unlink(); path.symlink_to(victim)
        return real_open(p, flags, mode, **kwargs)
    monkeypatch.setattr(state.os, 'open', switched)
    with pytest.raises(ValueError): state.read_json_strict(path, dict)
    assert victim.read_text() == '{"secret":1}'


def test_replace_failure_keeps_old_state_and_cleans_temporary(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'; path.write_bytes(b'{}\n')
    def fail(*args): raise OSError(errno.ENOSPC, 'test disk full')
    monkeypatch.setattr(state.os, 'replace', fail)
    with pytest.raises(OSError): state.atomic_write_json(path, {'new': 1})
    assert path.read_bytes() == b'{}\n'
    assert not list(tmp_path.glob('.state.json.*'))


def test_directory_sync_failure_is_after_publication(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'; path.write_bytes(b'{}\n')
    def fail(*args): raise OSError(errno.EIO, 'test directory sync')
    monkeypatch.setattr(state, '_fsync_directory', fail)
    with pytest.raises(OSError): state.atomic_write_json(path, {'new': 1})
    assert json.loads(path.read_text()) == {'new': 1}
    assert not list(tmp_path.glob('.state.json.*'))


def test_lock_registry_does_not_retain_historical_paths(tmp_path):
    gc.collect(); before = len(state._LOCAL_LOCKS)
    for i in range(200):
        with state.interprocess_lock(tmp_path / f'{i}.lock'): pass
    gc.collect()
    assert len(state._LOCAL_LOCKS) <= before
    lock1 = state._local_lock(tmp_path / 'same')
    lock2 = state._local_lock(tmp_path / 'same')
    assert lock1 is lock2


def test_threaded_json_updates_do_not_lose_increments(tmp_path):
    path = tmp_path / 'counter.json'
    def update(_):
        state.locked_json_update(path, lambda x: {'n': x['n'] + 1}, default={'n': 0})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(80)))
    assert state.read_json_strict(path, dict) == {'n': 80}


@pytest.mark.parametrize('timeout', [-1, float('nan'), float('inf')])
def test_lock_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError):
        with state.interprocess_lock(tmp_path / 'lock', timeout=timeout): pass


def test_local_lock_timeout_does_not_deadlock(tmp_path):
    path = tmp_path / 'lock'; entered = threading.Event(); release = threading.Event()
    def holder():
        with state.interprocess_lock(path):
            entered.set(); assert release.wait(5)
    t = threading.Thread(target=holder); t.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            with state.interprocess_lock(path, timeout=.03): pytest.fail('lock stolen')
        assert time.monotonic() - started < 1.0
    finally:
        release.set(); t.join(5)
    with state.interprocess_lock(path, timeout=0): pass


@pytest.mark.skipif(os.name != 'posix', reason='independent POSIX flock process')
def test_interprocess_lock_timeout_and_release(tmp_path):
    path = tmp_path / 'lock'
    script = '''import fcntl, sys
p = open(sys.argv[1], 'a+b')
fcntl.flock(p.fileno(), fcntl.LOCK_EX)
print('locked', flush=True)
sys.stdin.read(1)
'''
    child = subprocess.Popen([sys.executable, '-c', script, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'locked'
        with pytest.raises(TimeoutError):
            with state.interprocess_lock(path, timeout=.03): pytest.fail('cross-process lock stolen')
    finally:
        child.communicate('x', timeout=5)
    assert child.returncode == 0
    with state.interprocess_lock(path, timeout=.1): pass


def test_bounded_ledger_retains_complete_unicode_lines(tmp_path):
    path = tmp_path / 'ledger.jsonl'
    for i in range(60):
        assert append_bounded_jsonl(path, {'i': i, 'text': '中文'}, max_bytes=160)
        assert len(path.read_bytes()) <= 160
        assert path.read_bytes().endswith(b'\n')
    values = [json.loads(line) for line in path.read_bytes().splitlines()]
    assert values[-1]['i'] == 59
    assert all(v['text'] == '中文' for v in values)
    assert len(values) >= 2
    if os.name == 'posix': assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_bounded_ledger_exact_line_boundary_and_torn_tail(tmp_path):
    path = tmp_path / 'ledger'
    line0 = (json.dumps({'i': 0}, sort_keys=True) + '\n').encode()
    line1 = (json.dumps({'i': 1}, sort_keys=True) + '\n').encode()
    path.write_bytes(line0 + line1)
    append_bounded_jsonl(path, {'i': 2}, max_bytes=len(line0) * 2)
    assert [json.loads(x)['i'] for x in path.read_bytes().splitlines()] == [1, 2]
    path.write_bytes(line0 + b'{"torn"')
    append_bounded_jsonl(path, {'i': 2}, max_bytes=100)
    assert [json.loads(x)['i'] for x in path.read_bytes().splitlines()] == [0, 2]


def test_bounded_ledger_oversized_entry_is_not_written(tmp_path):
    path = tmp_path / 'new' / 'ledger'
    assert not append_bounded_jsonl(path, {'x': 'big' * 100}, max_bytes=10)
    assert not path.parent.exists()


def test_threaded_ledger_updates_across_instances(tmp_path):
    path = tmp_path / 'ledger'
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: append_bounded_jsonl(path, {'i': i}, max_bytes=100000), range(80)))
    values = [json.loads(x)['i'] for x in path.read_bytes().splitlines()]
    assert sorted(values) == list(range(80))


def test_multiprocess_ledger_no_lost_updates(tmp_path):
    path = tmp_path / 'ledger'
    module_root = str(Path(state.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=module_root + os.pathsep + os.environ.get('PYTHONPATH', ''))
    script = '''import sys
from eimemory.storage.bounded_jsonl import append_bounded_jsonl
for i in range(20):
    append_bounded_jsonl(sys.argv[1], {'process': int(sys.argv[2]), 'i': i}, max_bytes=100000)
'''
    children = [subprocess.Popen([sys.executable, '-c', script, str(path), str(i)], env=env, stderr=subprocess.PIPE) for i in range(3)]
    for child in children:
        _, err = child.communicate(timeout=15)
        assert child.returncode == 0, err.decode()
    values = [json.loads(x) for x in path.read_bytes().splitlines()]
    assert len(values) == 60
    assert {(v['process'], v['i']) for v in values} == {(p, i) for p in range(3) for i in range(20)}


def make_client(**kwargs):
    return rpc.AgentRuntimeRPCClient(base_url='http://127.0.0.1:9', auth_token='private-test-token', **kwargs)


class Response:
    def __init__(self, raw=b'{"ok":true}'): self.raw = raw; self.closed = False
    def read(self, n): return self.raw[:n]
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True


@pytest.mark.parametrize('exception,reason', [
    (http.client.BadStatusLine('private-server-line'), 'invalid_response'),
    (http.client.IncompleteRead(b'private-partial-body', 200), 'invalid_response'),
    (http.client.RemoteDisconnected('private-url'), 'invalid_response'),
    (HTTPError('https://secret.invalid/?token=private', 503, 'private-reason', {}, None), 'http_error'),
    (URLError(TimeoutError('private-timeout')), 'timeout'),
    (OSError('private-os-error'), 'connection_error'),
    (transport.UnsafeURL('private-url'), 'connection_error'),
    (ValueError('private-header'), 'configuration_invalid'),
])
def test_rpc_failures_are_sanitized_and_bypassed(monkeypatch, exception, reason):
    def fail(*args, **kwargs): raise exception
    monkeypatch.setattr(rpc, 'safe_urlopen', fail)
    c = make_client()
    result = c.call_or_bypass('adapter.prefetch', {})
    assert result['ok'] is False and result['bypassed'] is True
    assert result['diagnostic']['reason'] == reason
    try: c.call('adapter.prefetch', {})
    except rpc.AgentRuntimeTransportError as exc:
        rendered = ''.join(traceback.format_exception(exc))
        assert 'private-' not in rendered and 'secret.invalid' not in rendered
    else: pytest.fail('exception expected')


@pytest.mark.parametrize('raw', [b'[]', b'{"ok":true,"ok":false}', b'{"x":NaN}', b'{"x":1e309}', b'{"x":"\xff"}', b'{"broken"'])
def test_rpc_malformed_response_is_rejected_and_closed(monkeypatch, raw):
    response = Response(raw)
    monkeypatch.setattr(rpc, 'safe_urlopen', lambda *a, **k: response)
    result = make_client().call_or_bypass('x', {})
    assert result['diagnostic']['reason'] == 'invalid_response'
    assert response.closed


@pytest.mark.parametrize('params', [{'x': float('nan')}, {'x': float('inf')}, {'x': object()}, {'x': '\ud800'}, None, {'x': 'x'*2048}])
def test_rpc_invalid_request_never_performs_io(monkeypatch, params):
    def unexpected(*a, **kw): pytest.fail('network called for invalid request')
    monkeypatch.setattr(rpc, 'safe_urlopen', unexpected)
    c = make_client(max_request_bytes=1024, circuit_failure_threshold=1)
    result = c.call_or_bypass('x', params)
    assert result['diagnostic']['reason'] == 'invalid_request'
    assert not c._circuit_is_open()


def test_rpc_deep_request_is_invalid(monkeypatch):
    p = {}; current = p
    for _ in range(70): current['x'] = {}; current = current['x']
    monkeypatch.setattr(rpc, 'safe_urlopen', lambda *a, **k: pytest.fail('network called'))
    assert make_client().call_or_bypass('x', p)['diagnostic']['reason'] == 'invalid_request'


def test_rpc_valid_response_and_security_options_unchanged(monkeypatch):
    seen = {}; response = Response()
    def fake(*a, **k): seen.update(k); return response
    monkeypatch.setattr(rpc, 'safe_urlopen', fake)
    assert make_client().call('adapter.prefetch', {}) == {'ok': True, 'bypassed': False}
    assert seen['max_redirects'] == 0
    assert seen['allow_loopback'] is True and seen['allow_cgnat'] is True
    assert seen['headers']['Authorization'] == 'Bearer private-test-token'
    assert response.closed


def test_rpc_oversized_response_is_bounded(monkeypatch):
    response = Response(b' ' * 2048)
    monkeypatch.setattr(rpc, 'safe_urlopen', lambda *a, **k: response)
    result = make_client(max_response_bytes=1024).call_or_bypass('x', {})
    assert result['diagnostic']['reason'] == 'response_too_large'
    assert response.closed


@pytest.mark.skipif(os.name != 'posix', reason='POSIX symlink contract')
def test_rpc_diagnostic_failure_does_not_damage_symlink_victim(tmp_path):
    victim = tmp_path / 'victim'; victim.write_text('sentinel\n')
    ledger = tmp_path / 'ledger'; ledger.symlink_to(victim)
    c = make_client(failure_ledger_path=ledger)
    c._record_failure(method='x', error='adapter_unavailable')
    assert victim.read_text() == 'sentinel\n'
    assert c.last_failure_ledger_error == 'ledger_write_failed'


def test_rpc_logging_wait_is_bounded(tmp_path):
    ledger = tmp_path / 'ledger'; lock = ledger.with_name('ledger.lock')
    entered = threading.Event(); release = threading.Event()
    def holder():
        with state.interprocess_lock(lock): entered.set(); assert release.wait(5)
    t = threading.Thread(target=holder); t.start()
    try:
        assert entered.wait(5)
        c = make_client(failure_ledger_path=ledger)
        start = time.monotonic(); c._record_failure(method='x', error='circuit_open')
        assert time.monotonic() - start < 1
        assert c.last_failure_ledger_error == 'ledger_write_failed'
    finally:
        release.set(); t.join(5)
    c._record_failure(method='x', error='circuit_open')
    assert c.last_failure_ledger_error is None
    assert len(ledger.read_bytes().splitlines()) == 1


def test_circuit_stale_success_cannot_close_new_opening():
    clock = [0.0]
    circuit = CircuitBreaker(failure_threshold=1, reset_seconds=1, clock=lambda: clock[0])
    stale = circuit.acquire(); failure = circuit.acquire()
    circuit.failed(failure); circuit.succeeded(stale)
    assert circuit.acquire() is None
    assert circuit.is_open()


def test_circuit_single_half_open_probe_and_recovery():
    clock = [0.0]
    circuit = CircuitBreaker(failure_threshold=1, reset_seconds=1, clock=lambda: clock[0])
    stale = circuit.acquire(); circuit.failed(circuit.acquire())
    clock[0] = 2
    with ThreadPoolExecutor(max_workers=12) as pool:
        tickets = list(pool.map(lambda _: circuit.acquire(), range(40)))
    probes = [t for t in tickets if t is not None]
    assert len(probes) == 1 and probes[0].probe
    circuit.failed(stale)  # old completion cannot steal/renew the new probe
    assert circuit.acquire() is None
    circuit.succeeded(probes[0])
    assert circuit.acquire() is not None and not circuit.is_open()


def test_circuit_failed_probe_reopens_and_abandon_releases():
    clock = [0.0]
    circuit = CircuitBreaker(failure_threshold=2, reset_seconds=1, clock=lambda: clock[0])
    circuit.failed(circuit.acquire()); assert not circuit.is_open()
    circuit.failed(circuit.acquire()); assert circuit.is_open()
    clock[0] = 2
    ticket = circuit.acquire(); circuit.abandon(ticket)
    ticket = circuit.acquire(); assert ticket.probe
    circuit.failed(ticket); assert circuit.acquire() is None
    clock[0] = 4; assert circuit.acquire().probe


def test_rpc_old_inflight_success_cannot_reset_new_circuit(monkeypatch):
    c = make_client(circuit_failure_threshold=1)
    entered = threading.Event(); release = threading.Event()
    def call(method, params):
        if method == 'old': entered.set(); assert release.wait(5); return {'ok': True}
        raise rpc.AgentRuntimeTransportError('timeout')
    monkeypatch.setattr(c, 'call', call)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(c.call_or_bypass, 'old', {})
        try:
            assert entered.wait(5)
            c.call_or_bypass('fail', {})
            assert c._circuit_is_open()
        finally: release.set()
        assert old.result(5)['ok'] is True
    assert c._circuit_is_open()


def test_rpc_cancellation_releases_probe(monkeypatch):
    clock = [0.0]; monkeypatch.setattr(rpc, 'monotonic', lambda: clock[0])
    c = make_client(circuit_failure_threshold=1, circuit_reset_seconds=1)
    def fail(*args): raise rpc.AgentRuntimeTransportError('timeout')
    monkeypatch.setattr(c, 'call', fail); c.call_or_bypass('x', {})
    clock[0] = 2
    def cancel(*args): raise KeyboardInterrupt()
    monkeypatch.setattr(c, 'call', cancel)
    with pytest.raises(KeyboardInterrupt): c.call_or_bypass('x', {})
    monkeypatch.setattr(c, 'call', lambda *args: {'ok': True})
    assert c.call_or_bypass('x', {})['ok'] is True


def test_connect_uses_one_budget_for_all_addresses(monkeypatch):
    clock = [0.0]; calls = []
    monkeypatch.setattr(transport, 'monotonic', lambda: clock[0])
    def fail(address, *, timeout):
        calls.append(timeout); clock[0] += .3 if len(calls) == 1 else .6
        raise TimeoutError('test')
    monkeypatch.setattr(transport.socket, 'create_connection', fail)
    with pytest.raises(TimeoutError):
        transport._connect_pinned(('8.8.8.8', '1.1.1.1', '9.9.9.9'), port=80, timeout=.8)
    assert calls == pytest.approx([.8, .5])


def test_connect_success_preserves_peer_verification(monkeypatch):
    class Sock:
        closed = False
        def getpeername(self): return ('8.8.8.8', 80)
        def close(self): self.closed = True
    sock = Sock()
    monkeypatch.setattr(transport.socket, 'create_connection', lambda *a, **k: sock)
    got, peer = transport._connect_pinned(('8.8.8.8',), port=80, timeout=1)
    assert got is sock and peer == '8.8.8.8' and not sock.closed
    with pytest.raises(transport.UnsafeURL):
        transport._connect_pinned(('1.1.1.1',), port=80, timeout=1)
    assert sock.closed


@pytest.mark.parametrize('value', ['false', 0, 1, {}, [], {'not_ok': True}, {'ok': None}, {'ok': 'true'}, {'ok': 1}])
def test_nightly_malformed_present_report_is_not_success(value):
    assert nightly._aggregate_nightly_ok({'memory_quality': value}, []) is False


@pytest.mark.parametrize('part', ['root', 'prompt_safety', 'assessment'])
@pytest.mark.parametrize('field,value', [('error','disk_io_failed'), ('errors',['failed']), ('blocking_metrics',{'safety':0.1})])
def test_evidence_wait_cannot_hide_explicit_execution_failure(part, field, value):
    report = {'ok': False, 'awaiting_evidence': True}
    target = report if part == 'root' else report.setdefault(part, {})
    target[field] = value
    assert nightly._aggregate_nightly_ok({'l5_loop': report}, []) is False


def test_quality_wait_cannot_hide_error():
    gate = {'ok': False, 'blocked_reason': 'recall_quality_evidence_incomplete', 'error': 'disk_io_failed'}
    assert nightly._aggregate_nightly_ok({'recall_quality_gate': gate}, []) is False


@pytest.mark.parametrize('step', [None, 0, 'true', {}, {'ok': 'true'}])
def test_nightly_malformed_step_fails_closed(step):
    assert nightly._aggregate_nightly_ok({}, [step]) is False


def test_nightly_execution_and_readiness_compatibility():
    # Mirrors published SCH-01 compatibility cases, without importing Runtime.
    steps = []
    for raw in [[], [{'rule': 'r1'}], {'ok': True}]:
        assert nightly._nightly_step(steps, 'replay_rules', lambda: raw)['ok'] is True
    for raw in ['done', {}, {'ok': None}]:
        failed = []
        assert nightly._nightly_step(failed, 'x', lambda: raw)['ok'] is False
        assert failed[-1]['ok'] is False
    gate = {'ok':False,'blocked_reason':'recall_quality_evidence_incomplete','blocking_metrics':{}}
    assert nightly._aggregate_nightly_ok({'recall_quality_gate': gate}, [{'step':'x','ok':True}]) is True
    l5 = {'ok':False,'awaiting_evidence':True,'blocked_reason':'tip_safety_not_ready',
          'prompt_safety':{'ok':True,'status':'not_ready','awaiting_evidence':True},
          'assessment':{'ok':True,'missing_evidence':['prompt_safety:awaiting_evidence']}}
    assert nightly._aggregate_nightly_ok({'l5_loop': l5}, [{'step':'x','ok':True}]) is True
    assert nightly._aggregate_nightly_ok({'roi': {'ok': True}, 'l5_loop': None}, []) is True
    assert nightly._aggregate_nightly_ok({'l5_loop': {'ok':False,'blocked_reason':'l5_loop_timeout_exceeded'}}, []) is False
    assert nightly._aggregate_nightly_ok({'roi': {'ok': True}}, [{'step':'x'}]) is False


@pytest.mark.parametrize('module', [nightly, sys.modules[CircuitBreaker.__module__], sys.modules[append_bounded_jsonl.__module__]])
def test_extracted_components_have_no_runtime_import(module):
    import ast
    tree = ast.parse(Path(module.__file__).read_text())
    imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
    imported += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    assert not any(name == 'eimemory.api' or name.startswith('eimemory.api.') for name in imported)


def test_jobs_reexports_contract_without_duplicate_implementations():
    import ast
    tree = ast.parse(Path(nightly.__file__).with_name('jobs.py').read_text())
    names = {'_nightly_step', '_aggregate_nightly_ok', '_quality_wait_is_non_actionable', '_l5_awaiting_evidence_is_non_actionable'}
    aliases = {alias.name for node in tree.body if isinstance(node, ast.ImportFrom) and node.module == 'eimemory.scheduler.result_contract' for alias in node.names}
    assert names <= aliases
    assert not names & {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}


def test_multiprocess_state_updates_no_lost_increments(tmp_path):
    path = tmp_path / 'counter.json'
    module_root = str(Path(state.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=module_root + os.pathsep + os.environ.get('PYTHONPATH', ''))
    script = '''import sys
from eimemory.storage.atomic_file import locked_json_update
for _ in range(20):
    locked_json_update(sys.argv[1], lambda x: {'n': x['n']+1}, default={'n':0})
'''
    children = [subprocess.Popen([sys.executable, '-c', script, str(path)], env=env, stderr=subprocess.PIPE) for _ in range(3)]
    for child in children:
        _, err = child.communicate(timeout=15)
        assert child.returncode == 0, err.decode()
    assert state.read_json_strict(path, dict) == {'n': 60}
