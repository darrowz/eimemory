"""Recovery health is metadata-only and uses synthetic durable state."""
import json
from unittest.mock import patch

import pytest

from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.api.runtime import Runtime
from eimemory.knowledge.l1_queue import L1ExtractQueue


@pytest.fixture
def fixture(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        queue = L1ExtractQueue(tmp_path / 'state/l1_extract_queue.json')
        queue._save({'jobs': [], 'dead': [dict(job_id='original', episode_id='synthetic',
                     status='dead', attempts=5, last_error='SYNTHETIC_PRIVATE')]})
        log = tmp_path / 'logs/l1-extract.jsonl'
        log.parent.mkdir(exist_ok=True)
        log.write_text('{"ok": true}\n')
        yield queue, EIBrainRPCBridge(runtime), log


def status(bridge):
    reply = bridge.handle({'method': 'adapter.status', 'params': {'channel': 'hermes', 'scope': {
        'tenant_id': 'synthetic', 'agent_id': 'synthetic',
        'workspace_id': 'synthetic', 'user_id': 'synthetic'}}})
    assert reply['ok'] is True
    return reply['result']


def complete(queue):
    queue.recover_dead(['original'], operator_intent='recover-selected-dead-l1')
    queue.drain_report(lambda job: None)


def test_completed_recovery_clears_false_degraded_without_erasing_history(fixture):
    queue, bridge, _ = fixture
    assert status(bridge)['degraded'] is True
    original = queue._load()['dead']
    complete(queue)
    result = status(bridge)
    assert result['degraded'] is False
    health = result['l1_extract']
    assert health['ok'] is True and health['dead'] == 1 and health['pending'] == 0
    assert health['unresolved_dead'] == 0 and health['completed_recoveries'] == 1
    assert queue._load()['dead'] == original
    assert 'SYNTHETIC_PRIVATE' not in json.dumps(health)


def test_active_and_exhausted_recovery_remain_unhealthy(fixture):
    queue, bridge, _ = fixture
    queue.recover_dead(['original'], operator_intent='recover-selected-dead-l1')
    health = status(bridge)['l1_extract']
    assert health['ok'] is False and health['active_recoveries'] == 1
    def fail(job):
        raise RuntimeError('SYNTHETIC_PRIVATE')
    for _ in range(5):
        queue.drain_report(fail)
    health = status(bridge)['l1_extract']
    assert health['ok'] is False and health['failed_recoveries'] == 1
    assert health['dead'] == 2 and health['completed_recoveries'] == 0


@pytest.mark.parametrize('worker', [None, '', '[]', '{}', '{"ok": false}', '{"ok": 1}', '{'])
def test_completed_recovery_requires_healthy_worker_observation(fixture, worker):
    queue, bridge, log = fixture
    complete(queue)
    if worker is None:
        log.unlink()
    else:
        log.write_text(worker)
    assert status(bridge)['l1_extract']['ok'] is False


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'orphan', 'unreadable'])
def test_unknown_queue_never_becomes_empty_healthy(fixture, damage):
    queue, bridge, _ = fixture
    complete(queue)
    if damage == 'missing':
        queue.path.unlink()
    elif damage == 'corrupt':
        queue.path.write_text('{')
    elif damage == 'orphan':
        state = queue._load()
        state['dead'] = []
        queue._save(state)
    if damage == 'unreadable':
        with patch.object(L1ExtractQueue, '_load', side_effect=OSError('SYNTHETIC_PRIVATE')):
            health = status(bridge)['l1_extract']
    else:
        health = status(bridge)['l1_extract']
    assert health['ok'] is False and health['pending'] is None and health['dead'] is None
    assert health['completed_recoveries'] is None


def test_summary_reads_one_locked_snapshot_and_does_not_write(fixture):
    queue, _, _ = fixture
    complete(queue)
    before = queue.path.read_bytes()
    with patch.object(queue, '_load', wraps=queue._load) as load:
        summary = queue.health_summary()
    assert load.call_count == 1
    assert summary['completed_recoveries'] == 1
    assert queue.path.read_bytes() == before


def test_null_completion_is_still_active(fixture):
    queue, bridge, _ = fixture
    queue.recover_dead(['original'], operator_intent='recover-selected-dead-l1')
    state = queue._load()
    state['recoveries']['original']['completed_at'] = None
    queue._save(state)
    health = status(bridge)['l1_extract']
    assert health['ok'] is False and health['active_recoveries'] == 1
    assert health['completed_recoveries'] == 0


def test_disappearing_queue_is_unknown(fixture, monkeypatch):
    queue, bridge, _ = fixture
    require = L1ExtractQueue._require_recovery_queue
    def remove_after_check(self):
        require(self)
        self.path.unlink()
    monkeypatch.setattr(L1ExtractQueue, '_require_recovery_queue', remove_after_check)
    health = status(bridge)['l1_extract']
    assert health['ok'] is False and health['dead'] is None


def test_invalid_lock_is_unknown(fixture):
    queue, bridge, _ = fixture
    queue.lock_path.unlink(missing_ok=True)
    queue.lock_path.symlink_to(queue.path)
    health = status(bridge)['l1_extract']
    assert health['ok'] is False and health['dead'] is None


@pytest.mark.parametrize('replacement', ['symlink', 'hardlink', 'fifo'])
def test_queue_replaced_between_check_and_open_is_unknown(fixture, monkeypatch, replacement):
    import io
    import os
    import signal
    from pathlib import Path
    queue, bridge, _ = fixture
    complete(queue)
    other = queue.path.with_name('other.json')
    other.write_text('{"jobs": [], "dead": []}')
    original_os_open, original_io_open = os.open, io.open
    replaced = False
    def replace(path):
        nonlocal replaced
        if not isinstance(path, int) and Path(path) == queue.path and not replaced:
            replaced = True
            queue.path.unlink()
            if replacement == 'symlink':
                queue.path.symlink_to(other)
            elif replacement == 'hardlink':
                os.link(other, queue.path)
            else:
                os.mkfifo(queue.path)
    def os_open(path, *args, **kwargs):
        replace(path)
        return original_os_open(path, *args, **kwargs)
    def io_open(path, *args, **kwargs):
        replace(path)
        return original_io_open(path, *args, **kwargs)
    monkeypatch.setattr(os, 'open', os_open)
    monkeypatch.setattr(io, 'open', io_open)
    def blocked(*_):
        raise AssertionError('queue read blocked')
    previous = signal.signal(signal.SIGALRM, blocked)
    signal.setitimer(signal.ITIMER_REAL, 1)
    try:
        health = status(bridge)['l1_extract']
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    assert replaced
    assert health['ok'] is False and health['dead'] is None


@pytest.mark.parametrize('worker', [b'{"ok":false,"ok":true}', b'{"ok":true,"ts":"\xff"}',
                                    b'{"ok":true,"processed":NaN}',
                                    b'{"ok":true,"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}'])
def test_invalid_worker_observation_never_healthy(fixture, worker):
    queue, bridge, log = fixture
    complete(queue)
    log.write_bytes(worker + b'\n')
    assert status(bridge)['l1_extract']['ok'] is False


def _worker_record(size):
    prefix = b'{"ok":true,"x":"'
    suffix = b'"}'
    return prefix + b'a' * (size - len(prefix) - len(suffix)) + suffix


def test_worker_record_without_terminal_lf_is_not_healthy(fixture):
    queue, bridge, log = fixture
    complete(queue)
    log.write_bytes(b'{"ok":true}')
    assert status(bridge)['l1_extract']['ok'] is False


@pytest.mark.parametrize('prior', [b'{"ok":true}\n', b''])
def test_max_size_worker_record_with_lf_is_healthy(fixture, prior):
    queue, bridge, log = fixture
    complete(queue)
    log.write_bytes(prior + _worker_record(32 * 1024) + b'\n')
    assert status(bridge)['l1_extract']['ok'] is True


def test_oversize_worker_record_is_not_healthy(fixture):
    queue, bridge, log = fixture
    complete(queue)
    log.write_bytes(_worker_record(32 * 1024 + 1) + b'\n')
    assert status(bridge)['l1_extract']['ok'] is False
