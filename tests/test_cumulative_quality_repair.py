import copy
from contextlib import closing
from dataclasses import asdict, replace

import pytest

from eimemory.api.evolution import EvolutionAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id='fixture', agent_id='fixture', workspace_id='quality', user_id='owner')
TEXT = 'Synthetic runtime memory policy uses explicit scoped evidence.'


def row(store, record_id, *, source='alpha', scope=SCOPE):
    record = RecordEnvelope.create(kind='memory', title='Synthetic quality', scope=scope,
        source_id=source, content={'text': TEXT}, meta={'force_capture': True})
    record.record_id = record_id
    return store.append(record)


def current(store, record):
    return store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)


def test_native_repair_keeps_scope_and_source_partitions(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        a = row(store, 'mem_a')
        b = row(store, 'mem_b')
        other = row(store, 'mem_other', source='beta')
        shared = row(store, 'mem_shared', scope=replace(SCOPE, user_id=''))
        shared_before = current(store, shared).to_dict()
        report = EvolutionAPI(store).repair_memory_quality(scope=asdict(SCOPE), apply=True)
        assert report['ok'] is True and report['rejected_count'] == 1
        assert current(store, a).status == 'active'
        assert current(store, b).status == 'rejected'
        assert current(store, b).meta['duplicate_of_ref']['source_id'] == 'alpha'
        assert current(store, other).status == 'active'
        assert current(store, shared).to_dict() == shared_before
        again = EvolutionAPI(store).repair_memory_quality(scope=asdict(SCOPE), apply=True)
        assert again['committed_action_count'] == 0 and again['applied'] is False


@pytest.mark.parametrize('role', ['target', 'keeper'])
def test_native_full_version_conflict_preserves_external_update(tmp_path, monkeypatch, role):
    with closing(RuntimeStore(tmp_path)) as store:
        a = row(store, 'mem_a')
        b = row(store, 'mem_b')
        target = b if role == 'target' else a
        owner = store.mutate_records_atomically
        def race(callback):
            changed = current(store, target)
            changed.provenance['external'] = 'must survive'
            store.rewrite(changed)
            return owner(callback)
        monkeypatch.setattr(store, 'mutate_records_atomically', race)
        report = EvolutionAPI(store).repair_memory_quality(scope=asdict(SCOPE), apply=True)
        assert report['ok'] is False and report['rejected_count'] == 0
        assert current(store, b).status == 'active'
        assert current(store, target).provenance['external'] == 'must survive'
        assert any(x.get('skip_reason') == f'{role}_changed' for x in report['actions'])


def test_preview_leaves_native_rows_and_input_objects_unchanged(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        a, b = row(store, 'mem_a'), row(store, 'mem_b')
        records = [a, b]
        for record in records:
            record.meta = {}
        before = copy.deepcopy([x.to_dict() for x in records])
        api = EvolutionAPI(store)
        monkeypatch.setattr(api, '_list_memory_records', lambda **kwargs: records)
        report = api.repair_memory_quality(scope=asdict(SCOPE), apply=False)
        assert report['applied'] is False and report['committed_action_count'] == 0
        assert [x.to_dict() for x in records] == before
        assert current(store, b).status == 'active'


def test_commit_then_raise_reports_unknown_and_stops_remaining_writes(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        a, b, c = [row(store, 'mem_'+x) for x in 'abc']
        owner = store.mutate_records_atomically
        calls = []
        def uncertain(callback):
            calls.append(1)
            owner(callback)
            raise RuntimeError('synthetic postcommit failure')
        monkeypatch.setattr(store, 'mutate_records_atomically', uncertain)
        report = EvolutionAPI(store).repair_memory_quality(scope=asdict(SCOPE), apply=True)
        assert report['ok'] is False and report['effects_unknown'] is True
        assert report['unknown_action_count'] > 0
        assert report['committed_action_count'] == 0
        assert len(calls) == 1
        assert current(store, b).status == 'rejected' and current(store, c).status == 'active'
        assert report['unconfirmed_record_refs']


def test_native_write_failure_preserves_rows_and_outbox(tmp_path, monkeypatch):
    with closing(RuntimeStore(tmp_path)) as store:
        a, b = row(store, 'mem_a'), row(store, 'mem_b')
        before = current(store, b).to_dict()
        with store._lock:
            count = store.sqlite.conn.execute('SELECT count(*) FROM export_outbox').fetchone()[0]
        def fail(*args, **kwargs):
            raise RuntimeError('synthetic rewrite failure')
        monkeypatch.setattr(store.sqlite, 'rewrite', fail)
        report = EvolutionAPI(store).repair_memory_quality(scope=asdict(SCOPE), apply=True)
        assert report['ok'] is False and report['effects_unknown'] is True
        assert current(store, b).to_dict() == before
        with store._lock:
            assert store.sqlite.conn.execute('SELECT count(*) FROM export_outbox').fetchone()[0] == count
            assert not store.sqlite.in_transaction


def test_cli_quality_failure_propagates_exit_status(capsys):
    import json
    from types import SimpleNamespace
    from eimemory.cli.main import _cmd_quality
    failed = {'ok': False, 'effects_unknown': True, 'unknown_action_count': 1}
    runtime = SimpleNamespace(evolution=SimpleNamespace(repair_memory_quality=lambda **kwargs: failed))
    parsed = SimpleNamespace(quality_command='repair', apply=True)
    assert _cmd_quality(parsed, runtime, asdict(SCOPE)) == 1
    assert json.loads(capsys.readouterr().out) == failed


def test_nonfinite_or_overflow_salience_is_not_keeper_priority():
    from eimemory.api.evolution import _record_salience
    record = RecordEnvelope.create(kind='memory', title='Synthetic quality', scope=SCOPE)
    for value in (float('nan'), float('inf'), 10**400):
        record.meta['quality'] = {'salience_score': value}
        assert _record_salience(record) == 0.


def test_native_keeper_own_quality_commit_allows_following_duplicates(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        records = [row(store, 'mem_'+x) for x in 'abc']
        keeper = current(store, records[0])
        keeper.meta.pop('scoring', None)
        store.rewrite(keeper)
        report = EvolutionAPI(store).repair_memory_quality(scope=asdict(SCOPE), apply=True)
        assert report['ok'] is True
        assert report['updated_record_count'] == 3
        assert report['rejected_count'] == 2
        assert current(store, records[0]).status == 'active'
        assert all(current(store, r).status == 'rejected' for r in records[1:])
