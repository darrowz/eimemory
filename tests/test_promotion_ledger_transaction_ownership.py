import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.promotion.promotion_manager import _ensure_promotion_rollout_ledger
from eimemory.models.records import RecordEnvelope, ScopeRef


@pytest.mark.parametrize('failure', ['caller', 'write', 'none'])
def test_rollout_ledger_owns_only_its_transaction(tmp_path, monkeypatch, failure):
    with Runtime.create(root=tmp_path) as runtime:
        scope = ScopeRef(user_id='fake-ledger-user')
        record = runtime.store.append(RecordEnvelope.create(
            kind='promotion_request', title='Fake promotion', scope=scope,
            content={'action': 'blocked'}, status='blocked'))
        sqlite = runtime.store.sqlite
        if failure == 'caller':
            with runtime.store.locked():
                sqlite.execute('SAVEPOINT caller')
                sqlite.execute('CREATE TABLE caller_pending(value TEXT)')
            with pytest.raises(RuntimeError, match='promotion_rollout_ledger_requires_own_transaction'):
                _ensure_promotion_rollout_ledger(runtime, promotion_record=record, scope=scope)
            with runtime.store.locked():
                assert sqlite.in_transaction
                assert sqlite.execute("SELECT name FROM sqlite_master WHERE name='caller_pending'").fetchone()
                sqlite.execute('ROLLBACK TO caller')
                sqlite.execute('RELEASE caller')
        else:
            original = sqlite._record_policy_rollout_ledger
            if failure == 'write':
                def fail(**kwargs):
                    original(**kwargs)
                    raise RuntimeError('fake-ledger-write-failed')
                monkeypatch.setattr(sqlite, '_record_policy_rollout_ledger', fail)
                with pytest.raises(RuntimeError, match='fake-ledger-write-failed'):
                    _ensure_promotion_rollout_ledger(runtime, promotion_record=record, scope=scope)
            else:
                result = _ensure_promotion_rollout_ledger(runtime, promotion_record=record, scope=scope)
                assert result['created'] is True
                assert _ensure_promotion_rollout_ledger(runtime, promotion_record=record, scope=scope)['created'] is False
            with runtime.store.locked():
                assert not sqlite.in_transaction
                count = sqlite.execute('SELECT count(*) FROM policy_rollout_ledger WHERE promotion_id=?', (record.record_id,)).fetchone()[0]
                assert count == int(failure == 'none')
            runtime.store.append(RecordEnvelope.create(kind='memory', title='Writer remains usable', scope=scope))
