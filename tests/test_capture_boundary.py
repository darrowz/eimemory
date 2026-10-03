import pytest
from eimemory.api.runtime import Runtime
from eimemory.evaluation import query_input_vault as vault
from test_query_input_vault import seed

@pytest.mark.parametrize('enabled,status', [('0','disabled'), ('1','captured')])
def test_persisted_status_and_caller_transaction(tmp_path, monkeypatch, enabled, status):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', enabled)
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    runtime = Runtime.create(root=tmp_path)
    try:
        query, exact = seed(runtime)
        conn = runtime.store.sqlite.conn
        conn.execute('BEGIN IMMEDIATE')
        result = vault.capture_query_input(runtime, decision_id='decision', query=query,
            effective_query=query, explanation={}, host_query=query)
        assert conn.in_transaction
        assert result['status'] == status
        assert result['policy_fingerprint'] and result['recorded_at']
        assert vault.query_input_capture_status(runtime, decision_id='decision', scope=exact, channel='codex', source_ids=['codex'])['status'] == status
        conn.rollback()
        assert vault.query_input_capture_status(runtime, decision_id='decision', scope=exact, channel='codex', source_ids=['codex'])['status'] == 'historical_unknown'
    finally:
        runtime.close()


def test_failure_and_deleted_input(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    runtime = Runtime.create(root=tmp_path)
    try:
        query, exact = seed(runtime)
        kwargs = dict(decision_id='decision', query=query, effective_query=query, explanation={})
        vault.capture_query_input(runtime, **kwargs)
        conn = runtime.store.sqlite.conn
        conn.execute('DELETE FROM proactive_query_input_vault')
        conn.commit()
        assert vault.query_input_capture_status(runtime, decision_id='decision', scope=exact, channel='codex', source_ids=['codex'])['status'] == 'input_unavailable'
        conn.execute("CREATE TRIGGER fail_capture BEFORE INSERT ON proactive_query_input_vault BEGIN SELECT RAISE(ABORT,'fixture'); END")
        conn.commit()
        assert vault.capture_query_input(runtime, **kwargs)['status'] == 'capture_unavailable'
        assert vault.query_input_capture_status(runtime, decision_id='decision', scope=exact, channel='codex', source_ids=['codex'])['status'] == 'capture_unavailable'
    finally:
        runtime.close()

@pytest.mark.parametrize('failure', [False, True])
def test_proactive_atomic_and_idempotent(tmp_path, monkeypatch, failure):
    from eimemory.retrieval.proactive import ProactiveRecallService
    from eimemory.models.records import RecallBundle
    from test_query_input_vault import BASE
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    runtime = Runtime.create(root=tmp_path)
    try:
        monkeypatch.setattr(runtime.memory, 'recall', lambda **kw: RecallBundle(
            items=[], rules=[], reflections=[], confidence=0, next_action_hint='', explanation={}))
        conn = runtime.store.sqlite.conn
        if failure:
            original = vault._capture_query_input
            def fail(*args, **kwargs):
                assert conn.in_transaction
                assert conn.execute('SELECT count(*) FROM proactive_decisions').fetchone()[0] == 1
                raise RuntimeError('synthetic capture write failure')
            monkeypatch.setattr(vault, '_capture_query_input', fail)
        service = ProactiveRecallService(runtime, control_percent=0, release_identity={
            'release_commit':'a'*40, 'release_version':'1.0',
            'deployment_receipt_id':'receipt', 'release_session_id':'release'})
        kwargs = dict(channel='codex', scope=BASE, source_ids=['codex'], session_id='s',
                      query_id='t', query='original question', task_type='code.task')
        result = service.decide(**kwargs)
        assert not conn.in_transaction
        assert result['input_capture']['status'] == ('capture_unavailable' if failure else 'captured')
        repeat = service.decide(**kwargs)
        assert repeat['idempotent']
        assert repeat['input_capture'] == result['input_capture']
    finally:
        runtime.close()

@pytest.mark.parametrize('field', ['query', 'effective_query', 'host_query'])
def test_tampered_input_rejected(tmp_path, monkeypatch, field):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    runtime = Runtime.create(root=tmp_path)
    try:
        query, exact = seed(runtime)
        kwargs = dict(decision_id='decision', query=query, effective_query=query,
                      host_query=query, explanation={})
        kwargs[field] = 'tampered input'
        result = vault.capture_query_input(runtime, **kwargs)
        assert result['status'] in {'decision_identity_mismatch', 'host_input_identity_mismatch'}
        assert not result['evaluable']
    finally:
        runtime.close()


def test_denied_capture_cannot_load_old_private_input(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        query, exact = seed(runtime)
        kwargs = dict(decision_id='decision', query=query, effective_query=query, explanation={})
        vault.capture_query_input(runtime, **kwargs)
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', '[]')
        assert vault.capture_query_input(runtime, **kwargs)['status'] == 'scope_not_enabled'
        with pytest.raises(ValueError, match='original_query_input_unavailable'):
            vault.load_query_input(runtime, decision_id='decision', scope=exact, channel='codex', source_id='codex')


def test_status_scope_and_policy_privacy(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        query, exact = seed(runtime)
        vault.capture_query_input(runtime, decision_id='decision', query=query, effective_query=query, explanation={})
        own = vault.query_input_capture_status(runtime, decision_id='decision', scope=exact,
                                               channel='codex', source_ids=['codex'])
        assert own['status'] == 'captured' and 'policy_fingerprint' not in own
        foreign = vault.query_input_capture_status(runtime, decision_id='decision',
            scope={**exact, 'user_id': 'foreign'}, channel='codex', source_ids=['codex'])
        assert foreign == {'status': 'historical_unknown', 'evaluable': False}


def test_status_write_failure_rolls_back_capture_only(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        query, exact = seed(runtime)
        conn = runtime.store.sqlite.conn
        conn.execute('CREATE TABLE proactive_query_capture_status (decision_id TEXT PRIMARY KEY,payload TEXT NOT NULL)')
        conn.execute("CREATE TRIGGER fail_status BEFORE INSERT ON proactive_query_capture_status BEGIN SELECT RAISE(ABORT,'status fixture'); END")
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        conn.execute("UPDATE proactive_decisions SET policy_version='caller' WHERE decision_id='decision'")
        with pytest.raises(Exception, match='status fixture'):
            vault.capture_query_input(runtime, decision_id='decision', query=query, effective_query=query, explanation={})
        assert conn.in_transaction
        assert conn.execute('SELECT policy_version FROM proactive_decisions').fetchone()[0] == 'caller'
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='proactive_query_input_vault'").fetchone()
        conn.rollback()


@pytest.mark.parametrize('action', ['expired', 'deleted'])
def test_repeated_capture_reports_actual_vault(tmp_path, monkeypatch, action):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        query, exact = seed(runtime)
        kwargs = dict(decision_id='decision', query=query, effective_query=query, explanation={})
        vault.capture_query_input(runtime, **kwargs)
        conn = runtime.store.sqlite.conn
        if action == 'expired':
            conn.execute("UPDATE proactive_query_input_vault SET created_at=datetime('now','-31 days')")
        else:
            conn.execute('DELETE FROM proactive_query_input_vault')
        conn.commit()
        status = vault.query_input_capture_status(runtime, decision_id='decision', scope=exact,
                                                 channel='codex', source_ids=['codex'])
        assert status['status'] == 'input_unavailable' and not status['evaluable']
        if action == 'expired':
            assert vault.capture_query_input(runtime, **kwargs)['status'] == 'input_unavailable'


@pytest.mark.parametrize('commit', [False, True])
def test_nested_decision_outbox_waits_for_caller(tmp_path, monkeypatch, commit):
    from eimemory.models.records import RecordEnvelope, ScopeRef
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        query, exact = seed(runtime)
        payload = runtime.store.load_proactive_decision('decision')
        payload.update(decision_id='nested', session_id='nested', turn_id='nested', query_id='nested')
        record = RecordEnvelope.create(kind='feedback', title='Local audit fixture', summary='fixture',
            scope=ScopeRef.from_dict(exact), source='test', source_id='codex', content={})
        conn = runtime.store.sqlite.conn
        before = len(runtime.store.sqlite.pending_exports(limit=1000))
        conn.execute('BEGIN IMMEDIATE')
        runtime.store.record_proactive_decision(payload, [], [record], capture_input=lambda:
            vault.capture_query_input(runtime, decision_id='nested', query=query, effective_query=query, explanation={}))
        assert conn.in_transaction
        assert len(runtime.store.sqlite.pending_exports(limit=1000)) > before
        with pytest.raises(RuntimeError, match='flush_exports_requires_own_transaction'):
            runtime.store.flush_exports()
        if commit:
            conn.commit()
            assert runtime.store.flush_exports()['remaining'] == 0
            assert runtime.store.load_proactive_decision('nested') is not None
        else:
            conn.rollback()
            assert runtime.store.load_proactive_decision('nested') is None
            assert len(runtime.store.sqlite.pending_exports(limit=1000)) == before
            assert vault.query_input_capture_status(runtime, decision_id='nested', scope=exact,
                channel='codex', source_ids=['codex'])['status'] == 'historical_unknown'


def test_status_rejects_tampered_vault_payload(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    with Runtime.create(root=tmp_path) as runtime:
        query, exact = seed(runtime)
        vault.capture_query_input(runtime, decision_id='decision', query=query, effective_query=query, explanation={})
        conn = runtime.store.sqlite.conn
        conn.execute("UPDATE proactive_query_input_vault SET payload='{}'")
        conn.commit()
        status = vault.query_input_capture_status(runtime, decision_id='decision', scope=exact,
                                                 channel='codex', source_ids=['codex'])
        assert status['status'] == 'input_unavailable' and not status['evaluable']
