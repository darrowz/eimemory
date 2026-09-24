"""Independent regression coverage of absorbed audit boundary fixes."""
from contextlib import contextmanager, closing
from types import SimpleNamespace
import tracemalloc
import pytest
from eimemory.api.runtime import Runtime


def test_audit_read_and_verify_share_append_lock_without_recursion(tmp_path, monkeypatch):
    import eimemory.governance.safety.audit as audit
    real_lock = audit.exclusive_file_lock
    held, seen = [], []
    @contextmanager
    def checked_lock(path):
        assert not held
        held.append(path)
        seen.append(path)
        try:
            with real_lock(path):
                yield
        finally:
            held.pop()
    monkeypatch.setattr(audit, 'exclusive_file_lock', checked_lock)
    log = audit.AuditLog(tmp_path / 'audit.jsonl')
    log.append({'event': 'synthetic-test'})
    assert len(log.read_all()) == 1
    log.verify()
    assert seen == [tmp_path / 'audit.jsonl.lock'] * 3


def test_systemd_environment_keeps_all_property_lines(monkeypatch):
    import eimemory.ops.runtime_identity_drift as drift
    monkeypatch.setattr(drift.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0,
        stdout='LoadState=loaded\nEnvironment=A=one\nEnvironment=\nEnvironment=B=two\n'))
    assert drift._systemd_environment('synthetic.service') == 'A=one B=two'


def test_world_watch_does_not_reset_external_tracemalloc_peak(tmp_path, monkeypatch):
    from eimemory.governance.world_watchers import collect_world_signals, SourceWatch
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    monkeypatch.setattr(tracemalloc, 'reset_peak', lambda: pytest.fail('reset external peak'))
    try:
        with closing(Runtime.create(root=tmp_path)) as runtime:
            collect_world_signals(runtime, scope={'agent_id': 'test'}, legacy_compatibility=True,
                watches=[SourceWatch(name='disabled', kind='local_state', enabled=False)], dry_run=True)
        assert tracemalloc.is_tracing()
    finally:
        if started:
            tracemalloc.stop()


@pytest.mark.parametrize('kind', ['skill', 'experience'])
def test_experience_bridge_rejects_secret_before_persistence(tmp_path, kind):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        if kind == 'skill':
            from test_experience_bridge import _trace_payload
            payload = {**_trace_payload(), 'clientSecret': 'synthetic-fixture-value'}
            result = runtime.record_skill_trace(payload, scope={'agent_id': 'test'})
        else:
            result = runtime.record_experience_item({'experience_kind': 'lesson', 'skill_ids': [],
                'confidence': 0.9, 'outcome_delta': 'test', 'clientSecret': 'synthetic-fixture-value'},
                scope={'agent_id': 'test'})
        assert result == {'ok': False, 'error': 'sensitive_payload'}
        assert not runtime.store.list_records(kinds=['reflection'], scope={'agent_id': 'test'}, limit=10)
