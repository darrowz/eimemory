"""Finite AST-extracted method tests with inert models, evaluator, storage and outbox.
No application import, Runtime construction, SQLite connection, filesystem storage,
provider, hook, adapter, real maintenance, or environment-helper execution.
The actual existing atomic owner method is AST extracted into an inert fixture.
"""
import ast
import copy
import hashlib
import json
import math
import re
import sys
import threading
import unittest
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parents[1]
SOURCE = BASE / 'eimemory/api/evolution.py'
if '--support-root' in sys.argv:
    at = sys.argv.index('--support-root')
    BASE = Path(sys.argv[at + 1])
    del sys.argv[at:at + 2]
if '--source' in sys.argv:
    at = sys.argv.index('--source')
    SOURCE = Path(sys.argv[at + 1])
    del sys.argv[at:at + 2]

@dataclass
class Scope:
    tenant_id: str = 'tenant'
    agent_id: str = 'agent'
    workspace_id: str = 'workspace'
    user_id: str = 'user'
    @classmethod
    def from_dict(cls, value):
        return cls(**value)

@dataclass
class Time:
    created_at: str = '2026-01-01'
    updated_at: str = '2026-01-01'
    occurred_at: str = '2026-01-01'

@dataclass
class Record:
    record_id: str
    source_id: str = 'default'
    scope: Scope = field(default_factory=Scope)
    kind: str = 'memory'
    status: str = 'active'
    title: str = 'A useful observation'
    summary: str = 'Repeated useful observation text.'
    detail: str = ''
    content: dict = field(default_factory=dict)
    meta: dict = field(default_factory=lambda: {'quality': {'quality_tier': 'confirmed', 'salience_score': .8}, 'has_score': True})
    time: Time = field(default_factory=Time)
    source: str = 'synthetic'
    links: list = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    def to_dict(self):
        return asdict(self)
    @classmethod
    def from_dict(cls, data):
        data = copy.deepcopy(data)
        data['scope'] = Scope.from_dict(data['scope'])
        data['time'] = Time(**data['time'])
        return cls(**data)
    def touch(self):
        self.time.updated_at += ':touch'

def extract_definitions(path, names, class_name=None):
    tree = ast.parse(path.read_text())
    nodes = tree.body
    if class_name:
        cls = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == class_name)
        selected = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in names]
        nodes = [ast.ClassDef(name=class_name, bases=[], keywords=[], body=selected, decorator_list=[])]
    else:
        nodes = [n for n in nodes if isinstance(n, ast.FunctionDef) and n.name in names]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    return compile(ast.fix_missing_locations(module), str(path), 'exec')

def quality(**kwargs):
    return {'quality_tier': 'confirmed', 'salience_score': .8}

def with_score(meta, score, preserve_quality=True):
    return {**copy.deepcopy(meta), 'has_score': True}

ns = {'Mapping': Mapping, 'asdict': asdict, 'is_dataclass': is_dataclass,
      'sha256': hashlib.sha256, 'json': json, 'SCOPE_FIELDS': ('tenant_id','agent_id','workspace_id','user_id')}
exec(extract_definitions(BASE/'eimemory/contracts/recall_boundary.py', {'scope_tuple','exact_ref','authority_digest'}), ns)
exact_ref, authority_digest = ns['exact_ref'], ns['authority_digest']
ns.update({'ScopeRef': Scope, 'RecordEnvelope': Record, 'RuntimeStore': object,
           'math': math, 're': re, 'evaluate_memory_quality': quality,
           'extract_memory_score': lambda meta: object() if meta.get('has_score') else None,
           'score_from_legacy_quality': lambda **kwargs: SimpleNamespace(tier='confirmed', final_score=.8),
           'with_score_metadata': with_score})
exec(extract_definitions(SOURCE, {'_memory_text','_duplicate_text_key','_record_salience','_is_mojibake_or_noisy_memory','_repair_record_ref'}), ns)
exec(extract_definitions(SOURCE, {'__init__','repair_memory_quality','_duplicate_repair_actions','_list_memory_records'}, 'EvolutionAPI'), ns)
API = ns['EvolutionAPI']
owner_ns = {}
exec(extract_definitions(BASE/'eimemory/storage/runtime_store.py', {'mutate_records_atomically'}, 'RuntimeStore'), owner_ns)
REAL_OWNER_METHOD = owner_ns['RuntimeStore'].mutate_records_atomically

class InertSQLite:
    def __init__(self, store):
        self.store = store
        self.in_transaction = False
    def execute(self, statement):
        assert statement == 'BEGIN IMMEDIATE'
        assert not self.in_transaction
        self.in_transaction = True
        self.pending = copy.deepcopy(self.store.rows)
        self.pending_outbox = list(self.store.outbox)
    def get_by_exact_ref(self, record_id, *, scope, source_id):
        self.store.reads.append((record_id, asdict(scope), source_id))
        key = (record_id, *ns['scope_tuple'](scope), source_id)
        return copy.deepcopy(self.pending.get(key))
    def rewrite(self, record, *, previous_scope=None, commit=True):
        assert commit is False and self.in_transaction
        self.pending[exact_ref(record)] = copy.deepcopy(record)
        if record.record_id in self.store.fail_write_ids:
            raise RuntimeError('synthetic_write_after_mutation_failure')
    def upsert_memory_edges(self, edges, *, commit=True):
        assert not edges and commit is False and self.in_transaction
    def commit(self):
        self.store.rows = self.pending
        self.store.outbox = self.pending_outbox
        self.store.commits += 1
        self.in_transaction = False
    def rollback(self):
        self.store.rollbacks += 1
        self.in_transaction = False

class InertStore:
    def __init__(self, records):
        self.rows = {exact_ref(item): copy.deepcopy(item) for item in records}
        self.outbox = []
        self.projections = []
        self.owner_calls = self.commits = self.rollbacks = self.legacy_writes = 0
        self.fail_write_ids = set()
        self.fail_outbox_ids = set()
        self.before_owner = None
        self.race_injections = 0
        self.reads = []
        self._lock = threading.RLock()
        self.sqlite = InertSQLite(self)
    def list_records(self, *, kinds=None, scope=None, status=None, limit=100, offset=0):
        # Visibility-expanded fixture. The API must enforce mutation scope itself.
        values = [x for x in self.rows.values() if (not kinds or x.kind in kinds) and (not status or x.status == status)]
        return copy.deepcopy(values[offset:offset+limit])
    def mutate_records_atomically(self, callback):
        self.owner_calls += 1
        if self.before_owner:
            self.race_injections += 1
            self.before_owner(self)
        return REAL_OWNER_METHOD(self, callback)
    def _enqueue_record_exports(self, record):
        if record.record_id in self.fail_outbox_ids:
            raise RuntimeError('synthetic_outbox_failure')
        self.sqlite.pending_outbox.append(record.to_dict())
        return [{'operation_id': str(len(self.sqlite.pending_outbox))}]
    def _safe_post_commit_projection(self, exports, records):
        assert not self.sqlite.in_transaction
        self.projections.extend(record.record_id for record in records)
    # Legacy methods exist to expose unsafe baseline writes in red controls.
    def rewrite(self, record):
        if self.before_owner:
            self.race_injections += 1
            self.before_owner(self)
        self.legacy_writes += 1
        self.rows[exact_ref(record)] = copy.deepcopy(record)
        return record
    append = rewrite
    def record(self, record_id, source_id='default', scope=None):
        key = (record_id, *ns['scope_tuple'](scope or Scope()), source_id)
        return self.rows[key]

class RepairIdentityTests(unittest.TestCase):
    def run_repair(self, records, *, apply=True, store=None, limit=None):
        store = store or InertStore(records)
        return store, API(store).repair_memory_quality(scope=asdict(Scope()), apply=apply, limit=limit)
    def pair(self):
        return [Record('a'), Record('b')]
    def test_same_partition_dedupe_and_repeat(self):
        store, result = self.run_repair(self.pair())
        self.assertEqual(store.record('b').status, 'rejected')
        self.assertEqual(result['rejected_count'], 1)
        self.assertEqual(store.record('b').meta['duplicate_of'], 'a')
        self.assertEqual(store.record('b').meta['duplicate_of_ref']['source_id'], 'default')
        before = copy.deepcopy(store.rows)
        again = API(store).repair_memory_quality(scope=asdict(Scope()), apply=True)
        self.assertEqual(again['committed_action_count'], 0)
        self.assertEqual(store.rows, before)
    def test_distinct_sources_never_compete(self):
        store, result = self.run_repair([Record('a', source_id='alpha'), Record('b', source_id='beta')])
        self.assertEqual(store.record('b', 'beta').status, 'active')
        self.assertEqual(result['duplicate_count'], 0)
    def test_shared_and_legacy_scope_not_writable(self):
        shared = Scope(user_id='')
        legacy = Scope(workspace_id='legacy')
        rows = [Record('a'), Record('b',scope=shared), Record('c',scope=legacy)]
        store, result = self.run_repair(rows)
        self.assertEqual(store.record('b',scope=shared).status, 'active')
        self.assertEqual(store.record('c',scope=legacy).status, 'active')
        self.assertEqual(result['duplicate_count'], 0)
    def test_same_id_across_sources_has_distinct_action_identity(self):
        # Defensive pure fixture; current SQLite key also forbids same physical
        # record_id under two sources. No claim this fixture was stored in SQLite.
        rows = [Record('a',source_id='alpha'), Record('b',source_id='alpha'),
                Record('a',source_id='beta'), Record('b',source_id='beta')]
        store, result = self.run_repair(rows)
        self.assertEqual(store.record('b','alpha').status, 'rejected')
        self.assertEqual(store.record('b','beta').status, 'rejected')
        self.assertEqual(result['rejected_count'], 2)
    def test_inactive_targets_not_rejected_again(self):
        rows = [Record('a'), Record('b',status='superseded'), Record('c',status='archived')]
        store, result = self.run_repair(rows)
        self.assertEqual(store.record('b').status, 'superseded')
        self.assertEqual(store.record('c').status, 'archived')
    def test_dry_run_has_no_owner_or_input_mutation(self):
        store = InertStore(self.pair())
        before = copy.deepcopy(store.rows)
        _, result = self.run_repair([], store=store, apply=False)
        self.assertEqual(store.rows,before)
        self.assertEqual(store.owner_calls + store.legacy_writes,0)
        self.assertEqual(result['committed_action_count'],0)
        self.assertFalse(result['applied'])
    def test_keeper_changed_rejects_target(self):
        store = InertStore(self.pair())
        store.before_owner = lambda s: s.record('a').meta.update(external='change')
        _, result = self.run_repair([],store=store)
        self.assertGreater(store.race_injections,0)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(result['actions'][0]['skip_reason'],'keeper_changed')
    def test_target_changed_rejects_overwrite(self):
        store = InertStore(self.pair())
        store.before_owner = lambda s: s.record('b').links.append({'new':'link'})
        _, result = self.run_repair([],store=store)
        self.assertGreater(store.race_injections,0)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(result['actions'][0]['skip_reason'],'target_changed')
    def test_missing_target_is_reported(self):
        store = InertStore(self.pair())
        store.before_owner = lambda s: s.rows.pop(exact_ref(s.record('b')))
        _, result = self.run_repair([],store=store)
        self.assertEqual(result['actions'][0]['skip_reason'],'target_missing')
        self.assertEqual(result['updated_record_count'],0)
    def test_keeper_inactive_after_plan(self):
        store = InertStore(self.pair())
        store.before_owner = lambda s: setattr(s.record('a'),'status','rejected')
        _, result = self.run_repair([],store=store)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(result['actions'][0]['skip_reason'],'keeper_ineligible')
    def test_missing_atomic_owner_is_closed(self):
        store = InertStore(self.pair()); store.mutate_records_atomically = None
        _, result = self.run_repair([],store=store)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(store.legacy_writes,0)
        self.assertEqual(result['actions'][0]['skip_reason'],'atomic_owner_unavailable')
    def test_missing_exact_mutation_is_closed(self):
        store = InertStore(self.pair()); store.sqlite.get_by_exact_ref = None
        _, result = self.run_repair([],store=store)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(result['actions'][0]['skip_reason'],'exact_mutation_unavailable')
    def test_actual_owner_rolls_back_write_failure(self):
        store = InertStore(self.pair()); store.fail_write_ids={'b'}
        _, result = self.run_repair([],store=store)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(store.outbox,[])
        self.assertEqual(store.rollbacks,1)
        # The fixture establishes rollback; an arbitrary API owner exception
        # does not carry that confirmation back to the caller.
        self.assertTrue(result['effects_unknown'])
        self.assertEqual(result['unknown_action_count'],1)
        self.assertEqual({x['record_id'] for x in result['unconfirmed_record_refs']},{'b'})
        self.assertEqual(result['uncommitted_record_refs'],[])
        self.assertEqual(result['committed_action_count'],0)
    def test_actual_owner_rolls_back_outbox_failure(self):
        store = InertStore(self.pair()); store.fail_outbox_ids={'b'}
        _, result = self.run_repair([],store=store)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(store.outbox,[])
        self.assertEqual(store.projections,[])
        self.assertEqual(store.rollbacks,1)
        # The fixture establishes rollback; an arbitrary API owner exception
        # does not carry that confirmation back to the caller.
        self.assertTrue(result['effects_unknown'])
        self.assertEqual(result['unknown_action_count'],1)
        self.assertEqual({x['record_id'] for x in result['unconfirmed_record_refs']},{'b'})
        self.assertEqual(result['uncommitted_record_refs'],[])
        self.assertFalse(result['ok'])
    def test_partial_failure_lists_uncommitted_and_retry(self):
        store = InertStore([Record(x) for x in 'abcd']); store.fail_outbox_ids={'c'}
        _, result = self.run_repair([],store=store)
        self.assertEqual([store.record(x).status for x in 'bcd'],['rejected','active','active'])
        self.assertEqual({x['record_id'] for x in result['unconfirmed_record_refs']},{'c'})
        self.assertEqual({x['record_id'] for x in result['uncommitted_record_refs']},{'d'})
        self.assertTrue(result['effects_unknown'])
        self.assertEqual(result['committed_action_count'],1)
        self.assertEqual(len(store.outbox),1)
        store.fail_outbox_ids.clear()
        again=API(store).repair_memory_quality(scope=asdict(Scope()),apply=True)
        self.assertEqual(again['committed_action_count'],2)
        self.assertEqual(len(store.outbox),3)
    def test_concurrent_keeper_change_between_targets(self):
        store=InertStore([Record(x) for x in 'abcd'])
        def change(s):
            if s.owner_calls==2: s.record('a').summary='Externally changed keeper'
        store.before_owner=change
        _,result=self.run_repair([],store=store)
        self.assertEqual([store.record(x).status for x in 'bcd'],['rejected','active','active'])
        self.assertEqual([a.get('skip_reason') for a in result['actions'][1:]],['keeper_changed','keeper_changed'])
    def test_own_keeper_quality_commit_advances_only_own_digest(self):
        rows=[Record(x) for x in 'abc']; rows[0].meta.pop('has_score')
        store,result=self.run_repair(rows)
        self.assertEqual([store.record(x).status for x in 'bc'],['rejected','rejected'])
        self.assertEqual(result['updated_record_count'],3)
        self.assertEqual(result['skipped_action_count'],0)
        self.assertEqual(len(store.outbox),3)
    def test_full_payload_cas_includes_provenance(self):
        store=InertStore(self.pair())
        store.before_owner=lambda s:s.record('a').provenance.update(version='new')
        _,result=self.run_repair([],store=store)
        self.assertGreater(store.race_injections,0)
        self.assertEqual(store.record('b').status,'active')
        self.assertEqual(result['actions'][0]['skip_reason'],'keeper_changed')
    def test_nonfinite_payload_rejected_without_writes(self):
        row=Record('a'); row.meta['bad']=float('nan')
        store,result=self.run_repair([row])
        self.assertEqual(store.owner_calls,0)
        self.assertFalse(result['ok'])
        self.assertEqual(result['skipped_records'][0]['reason'],'invalid_payload')
    def test_same_id_different_scope_noise_does_not_hide_local_dedupe(self):
        rows=[Record('a'),Record('b'),Record('b',scope=Scope(user_id=''),summary='????')]
        store,result=self.run_repair(rows)
        self.assertEqual(store.record('b').status,'rejected')
        self.assertEqual(store.record('b',scope=Scope(user_id='')).status,'active')
        self.assertEqual(result['rejected_count'],1)
    def test_invalid_owner_result_is_unknown_not_uncommitted(self):
        store=InertStore([Record(x) for x in 'abc'])
        original_owner=store.mutate_records_atomically
        def bad_owner(callback):
            original_owner(callback)
            return None
        store.mutate_records_atomically=bad_owner
        _,result=self.run_repair([],store=store)
        self.assertTrue(result['effects_unknown'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['committed_action_count'],0)
        self.assertEqual(result['unknown_action_count'],1)
        self.assertEqual({x['record_id'] for x in result['unconfirmed_record_refs']},{'b'})
        self.assertEqual({x['record_id'] for x in result['uncommitted_record_refs']},{'c'})
        self.assertEqual(store.record('c').status,'active')

    def test_owner_postcommit_raise_matches_independent_counterexample(self):
        store=InertStore([Record(x) for x in 'abc'])
        original_owner=store.mutate_records_atomically
        def throws_after_commit(callback):
            original_owner(callback)
            raise RuntimeError('synthetic_exception_after_commit')
        store.mutate_records_atomically=throws_after_commit
        _,result=self.run_repair([],store=store)
        self.assertEqual(store.record('b').status,'rejected')
        self.assertEqual(store.record('c').status,'active')
        self.assertEqual(len(store.outbox),1)
        self.assertTrue(result['effects_unknown'])
        self.assertFalse(result['ok'])
        self.assertEqual(result['unknown_action_count'],1)
        self.assertEqual(result['committed_action_count'],0)
        self.assertEqual({x['record_id'] for x in result['unconfirmed_record_refs']},{'b'})
        self.assertEqual({x['record_id'] for x in result['uncommitted_record_refs']},{'c'})
        self.assertEqual(store.owner_calls,1)
        self.assertEqual(store.rollbacks,0)

    def test_owner_postcommit_raise_preserves_prior_confirmed_commit(self):
        store=InertStore([Record(x) for x in 'abcd'])
        original_owner=store.mutate_records_atomically
        def second_throws_after_commit(callback):
            result=original_owner(callback)
            if store.owner_calls==2:
                raise RuntimeError('synthetic_second_exception_after_commit')
            return result
        store.mutate_records_atomically=second_throws_after_commit
        _,result=self.run_repair([],store=store)
        self.assertEqual([store.record(x).status for x in 'bcd'],['rejected','rejected','active'])
        self.assertEqual(len(store.outbox),2)
        self.assertEqual(result['committed_action_count'],1)
        self.assertEqual(result['updated_record_count'],1)
        self.assertEqual(result['unknown_action_count'],1)
        self.assertEqual({x['record_id'] for x in result['unconfirmed_record_refs']},{'c'})
        self.assertEqual({x['record_id'] for x in result['uncommitted_record_refs']},{'d'})
        self.assertEqual(store.owner_calls,2)

    def test_limit_is_reported_as_listed_window(self):
        store,result=self.run_repair([Record(x) for x in 'abc'],limit=2)
        self.assertEqual(result['scanned_count'],2)
        self.assertEqual(result['scan_limit'],2)
        self.assertEqual(result['scan_coverage'],'listed_records')
        self.assertEqual(store.record('c').status,'active')
    def test_large_legal_payload_not_arbitrarily_refused(self):
        rows=self.pair(); rows[1].meta['large']='x'*(300*1024)
        store,result=self.run_repair(rows)
        self.assertEqual(store.record('b').status,'rejected')
        self.assertTrue(result['ok'])
    def test_large_duplicate_group_uses_only_pair_dependencies(self):
        rows=[Record(f'r{x:03}') for x in range(102)]
        store,result=self.run_repair(rows)
        self.assertEqual(result['duplicate_count'],101)
        self.assertEqual(store.owner_calls,101)
        self.assertEqual(len(store.reads),202)

if __name__=='__main__':
    unittest.main(verbosity=2)
