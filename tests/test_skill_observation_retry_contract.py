"""Finite sequential observation tests using inert in-memory persistence only.

No eimemory imports, Runtime/store construction, evaluator, command, network,
adapter, hook, or real data. These tests do NOT establish concurrency atomicity.
"""
from __future__ import annotations
import ast
from copy import deepcopy
from dataclasses import dataclass,asdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
import unittest

ROOT=Path(__file__).resolve().parents[1]
BASE=Path('/workspace/shared/eimemory-validated-phase23-scope-types-d555-20261002')
BV1=Path('/workspace/shared/eimemory-candidate-learning-observation-B-phase23-20261002')

@dataclass
class Scope:
    tenant_id:str='default'
    agent_id:str=''
    workspace_id:str=''
    user_id:str=''
    @classmethod
    def from_dict(cls,value):return cls(**(value or {}))

class Record:
    touches=0
    def __init__(self,**kwargs):
        self.__dict__.update(kwargs)
        for key,default in [('meta',{}),('content',{}),('tags',[]),('source_id','actual-source'),('source','synthetic'),('title',''),('summary',''),('detail','')]:
            if key not in self.__dict__:setattr(self,key,default)
    def touch(self):Record.touches+=1

class Time:
    def __init__(self,**kwargs):self.__dict__.update(kwargs)

class Store:
    def __init__(self,record):
        self.records={self.key(record.record_id,record.scope):deepcopy(record)}
        self.writes=[];self.reads=[];self.fail=None;self.fail_reads=False;self.hide_reads=False
    @staticmethod
    def key(record_id,scope):return (*asdict(scope).values(),record_id)
    def get_by_id(self,record_id,*,scope,exact_scope=False):
        self.reads.append((record_id,exact_scope))
        if self.fail_reads:raise RuntimeError('synthetic_read_failure')
        if self.hide_reads:return None
        record=self.records.get(self.key(record_id,scope))
        if record is None and not exact_scope:
            record=next((r for r in self.records.values() if r.record_id==record_id and r.scope.tenant_id==scope.tenant_id and r.scope.user_id==''),None)
        return deepcopy(record)
    def write(self,operation,record):
        self.writes.append(operation)
        if self.fail==(operation,'before'):raise RuntimeError('synthetic_before_write')
        if self.fail==(operation,'before_unreadable'):
            self.fail_reads=True
            raise RuntimeError('synthetic_ambiguous_write')
        self.records[self.key(record.record_id,record.scope)]=deepcopy(record)
        if self.fail==(operation,'after'):raise RuntimeError('synthetic_after_write')
        if self.fail==(operation,'after_hide'):
            self.hide_reads=True
            raise OSError('synthetic_after_commit_hidden_readback')
        if self.fail==(operation,'after_raise'):
            self.fail_reads=True
            raise OSError('synthetic_after_commit_raising_readback')
        return record
    def rewrite(self,record):return self.write('rewrite',record)
    def append(self,record):return self.write('append',record)


def functions(root=ROOT):
    clock={'calls':0}
    def now():
        clock['calls']+=1
        return f'2000-01-01T00:00:{clock["calls"]:03d}+00:00'
    ns={'Any':Any,'json':json,'sha256':sha256,'asdict':asdict,'ScopeRef':Scope,'RecordEnvelope':Record,'TimeRef':Time,'LinkRef':Record,'now_iso':now,'VALIDATION_SOURCE':'eimemory.skill_validation','REPORT_TYPE':'skill_candidate_validation','PERSISTENCE_RECEIPT_SCHEMA':'skill_observation_persistence.v2','REQUIRED_GOOD_OBSERVATIONS':3,'FAILURE_RATE_THRESHOLD':0.34,'REAL_OBSERVATION_KINDS':{'real','operator'},'GOOD_OUTCOMES':{'good','success','pass','passed','improved','better'},'BAD_OUTCOMES':{'bad','fail','failed','regressed','unsafe','error'}}
    names={'record_skill_candidate_observation','_observation_request_key','_observation_receipt_exists','_observation_retry_report','_observation_write_failure','_load_candidate_record','_scope','_validation_state','_observation_id','_is_real_observation','_last_bad_at','_rewrite_candidate_with_validation','_validation_result_record','_validation_record_id','_json_safe','_retag'}
    path=root/'eimemory/governance/learning/skill_validation.py'
    nodes=[n for n in ast.parse(path.read_text(),filename=str(path)).body if isinstance(n,ast.FunctionDef) and n.name in names]
    mod=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(mod),str(path),'exec'),ns)
    return ns,clock


def setup(root=ROOT,scope=None):
    ns,clock=functions(root)
    record=Record(record_id='synthetic-candidate',kind='skill_candidate',status='canary',scope=scope or Scope(),meta={},content={},tags=[],source_id='source.nondefault')
    return ns,clock,Store(record),record


def call(ns,store,record,observation_id='obs-1',outcome='good',**kwargs):
    return ns['record_skill_candidate_observation'](store,candidate_id=record.record_id,scope=record.scope,outcome=outcome,observation_id=observation_id,**kwargs)


def freeze(value):
    if isinstance(value,(Record,Time)):return freeze(vars(value))
    if isinstance(value,Scope):return asdict(value)
    if isinstance(value,dict):return {key:freeze(item) for key,item in value.items()}
    if isinstance(value,list):return [freeze(item) for item in value]
    return value

def snapshot(store,clock):return freeze(store.records),list(store.writes),clock['calls'],Record.touches


class ObservationRetryContract(unittest.TestCase):
    def test_exact_retry_is_zero_side_effect_and_returns_current_state(self):
        ns,clock,store,r=setup()
        for i in range(3):self.assertTrue(call(ns,store,r,f'good-{i}')['persisted'])
        before=snapshot(store,clock)
        retry=call(ns,store,r,'good-0',outcome=' GOOD ',observation_kind=' REAL ')
        self.assertEqual(snapshot(store,clock),before)
        self.assertTrue(retry['ok']);self.assertTrue(retry['duplicate']);self.assertFalse(retry['persisted'])
        self.assertEqual(retry['status_transition'],{'from':'active','to':'active'})
        self.assertTrue(retry['original_validation_record_present'])

    def test_conflicting_unsafe_does_not_rewrite_stored_good(self):
        ns,clock,store,r=setup()
        for i in range(3):call(ns,store,r,f'good-{i}')
        before=snapshot(store,clock)
        report=call(ns,store,r,'good-0',outcome='unsafe')
        self.assertEqual(snapshot(store,clock),before)
        self.assertEqual(report['error'],'observation_id_conflict')
        self.assertEqual(report['proposal_status'],'active')
        saved=store.get_by_id(r.record_id,scope=r.scope,exact_scope=True)
        self.assertEqual(saved.meta['skill_validation']['real_bad_count'],0)

    def test_changed_kind_reason_and_details_conflict_with_types_preserved(self):
        ns,clock,store,r=setup()
        call(ns,store,r,reason='original',details={'a':True,'b':2})
        before=snapshot(store,clock)
        same=call(ns,store,r,reason='original',details={'b':2,'a':True})
        self.assertTrue(same['ok'])
        for kwargs in ({'reason':'different','details':{'a':True,'b':2}},{'reason':'original','details':{'a':1,'b':2}},{'reason':'original','details':{'a':True,'b':2},'observation_kind':'synthetic'}):
            self.assertEqual(call(ns,store,r,**kwargs)['error'],'observation_id_conflict')
        self.assertEqual(snapshot(store,clock),before)

    def test_original_unsafe_retries_and_later_good_retry_preserve_rollback(self):
        ns,clock,store,r=setup()
        for i in range(3):call(ns,store,r,f'good-{i}')
        original=call(ns,store,r,'unsafe-1',outcome='unsafe')
        self.assertEqual(original['proposal_status'],'rolled_back')
        before=snapshot(store,clock)
        self.assertEqual(call(ns,store,r,'unsafe-1',outcome='unsafe')['proposal_status'],'rolled_back')
        self.assertEqual(call(ns,store,r,'good-0')['proposal_status'],'rolled_back')
        self.assertEqual(call(ns,store,r,'unsafe-1',outcome='good')['error'],'observation_id_conflict')
        self.assertEqual(snapshot(store,clock),before)

    def test_fresh_ids_preserve_three_good_and_two_bad_lifecycle(self):
        ns,clock,store,r=setup()
        self.assertEqual([call(ns,store,r,f'good-{i}')['proposal_status'] for i in range(3)],['canary','canary','active'])
        self.assertEqual(call(ns,store,r,'bad-1',outcome='bad')['proposal_status'],'active')
        self.assertEqual(call(ns,store,r,'bad-2',outcome='bad')['proposal_status'],'rolled_back')
        self.assertTrue(all(exact for _,exact in store.reads))
        self.assertEqual(store.get_by_id(r.record_id,scope=r.scope,exact_scope=True).source_id,'source.nondefault')

    def test_exact_scope_prevents_global_or_other_user_candidate_mutation(self):
        ns,clock,store,r=setup(scope=Scope(user_id=''))
        other=deepcopy(r);other.scope=Scope(user_id='some-user')
        before=snapshot(store,clock)
        with self.assertRaises(ValueError):call(ns,store,other)
        self.assertEqual(snapshot(store,clock),before)
        self.assertTrue(store.reads[-1][1])

    def test_unobserved_append_result_is_uncertain_not_successful_retry(self):
        ns,clock,store,r=setup();store.fail=('append','before')
        failed=call(ns,store,r)
        self.assertFalse(failed['ok']);self.assertIsNone(failed['partial']);self.assertIsNone(failed['persisted'])
        self.assertTrue(failed['candidate_observation_present']);self.assertIsNone(failed['original_validation_record_present'])
        self.assertTrue(failed['commit_uncertain']);self.assertEqual(failed['retry_safety'],'not_established')
        before=snapshot(store,clock);store.fail=None
        retry=call(ns,store,r)
        self.assertEqual(retry['error'],'observation_prior_write_unverified')
        self.assertIsNone(retry['partial']);self.assertIsNone(retry['original_persisted'])
        self.assertTrue(retry['commit_uncertain']);self.assertFalse(retry['write_attempted'])
        self.assertEqual(snapshot(store,clock),before)

    def test_failure_readback_reports_before_after_and_unknown_honestly(self):
        for stage in [('rewrite','before'),('rewrite','after'),('append','after'),('append','before_unreadable')]:
            ns,clock,store,r=setup();store.fail=stage
            failed=call(ns,store,r)
            self.assertFalse(failed['ok']);self.assertEqual(failed['error'],'observation_persistence_failed')
            if stage==('rewrite','before'):
                self.assertIsNone(failed['candidate_observation_present']);self.assertIsNone(failed['original_validation_record_present']);self.assertIsNone(failed['partial'])
                self.assertIsNone(failed['persisted']);self.assertTrue(failed['commit_uncertain'])
            elif stage==('rewrite','after'):
                self.assertTrue(failed['candidate_observation_present']);self.assertIsNone(failed['original_validation_record_present']);self.assertTrue(failed['partial'])
                self.assertFalse(failed['validation_write_attempted']);self.assertFalse(failed['commit_uncertain'])
            elif stage==('append','after'):
                self.assertTrue(failed['persisted']);self.assertFalse(failed['partial'])
                before=snapshot(store,clock);store.fail=None
                self.assertTrue(call(ns,store,r)['ok']);self.assertEqual(snapshot(store,clock),before)
            else:
                self.assertTrue(failed['candidate_observation_present']);self.assertIsNone(failed['original_validation_record_present']);self.assertIsNone(failed['partial'])
                self.assertTrue(failed['write_acknowledged']['candidate']);self.assertIsNone(failed['readback_observed']['candidate_observation'])

    def test_legacy_observation_without_receipt_remains_explicitly_unverified(self):
        ns,clock,store,r=setup()
        stored=store.records[store.key(r.record_id,r.scope)]
        stored.meta['skill_validation']={'observations':[{'observation_id':'old','outcome':'good','observation_kind':'real','reason':'','details':{},'good':True,'bad':False}]}
        before=snapshot(store,clock)
        report=call(ns,store,r,'old')
        self.assertEqual(report['error'],'observation_prior_write_unverified')
        self.assertIsNone(report['partial']);self.assertEqual(snapshot(store,clock),before)

    def test_fixed_hidden_readback_does_not_erase_two_durable_writes(self):
        ns,clock,store,r=setup()
        store.records[store.key(r.record_id,r.scope)].status='active'
        store.fail=('append','after_hide')
        failed=call(ns,store,r,'hidden',outcome='unsafe')
        self.assertEqual(store.writes,['rewrite','append'])
        self.assertEqual(len(store.records),2)
        self.assertIsNone(failed['persisted']);self.assertIsNone(failed['partial'])
        self.assertTrue(failed['write_attempted']);self.assertTrue(failed['commit_uncertain'])
        self.assertEqual(failed['write_acknowledged'],{'candidate':True,'validation':False})
        self.assertEqual(failed['readback_observed'],{'candidate_observation':False,'validation_record':False})
        self.assertTrue(failed['candidate_observation_present'])
        self.assertIsNone(failed['original_validation_record_present'])
        self.assertEqual(failed['retry_safety'],'not_established')
        before=snapshot(store,clock);store.fail=None;store.hide_reads=False
        retry=call(ns,store,r,'hidden',outcome='unsafe')
        self.assertTrue(retry['ok']);self.assertTrue(retry['original_persisted'])
        self.assertFalse(retry['write_attempted']);self.assertFalse(retry['persisted'])
        self.assertFalse(retry['commit_uncertain'])
        self.assertEqual(snapshot(store,clock),before)

    def test_acknowledged_candidate_survives_throwing_readback_after_append(self):
        ns,clock,store,r=setup();store.fail=('append','after_raise')
        failed=call(ns,store,r)
        self.assertEqual(len(store.records),2)
        self.assertTrue(failed['candidate_observation_present'])
        self.assertIsNone(failed['readback_observed']['candidate_observation'])
        self.assertIsNone(failed['persisted']);self.assertIsNone(failed['partial'])
        self.assertTrue(failed['commit_uncertain'])
        before=snapshot(store,clock);store.fail=None;store.fail_reads=False
        self.assertTrue(call(ns,store,r)['original_persisted'])
        self.assertEqual(snapshot(store,clock),before)

    def test_lookup_errors_before_any_write_keep_exception_contract_and_zero_side_effect(self):
        ns,clock,store,r=setup();store.fail_reads=True
        before=snapshot(store,clock)
        with self.assertRaises(RuntimeError):call(ns,store,r)
        self.assertEqual(snapshot(store,clock),before)
        store.fail_reads=False;store.hide_reads=True
        with self.assertRaises(ValueError):call(ns,store,r)
        self.assertEqual(snapshot(store,clock),before)
        self.assertEqual(store.writes,[]);self.assertEqual(clock['calls'],0)

    def test_bv1_fixed_negative_control_false_readback_misreports_commit(self):
        ns,clock,store,r=setup(BV1)
        store.records[store.key(r.record_id,r.scope)].status='active'
        store.fail=('append','after_hide')
        failed=call(ns,store,r,'hidden',outcome='unsafe')
        self.assertEqual(len(store.records),2)
        self.assertIs(failed['persisted'],False);self.assertIs(failed['partial'],False)
        before=snapshot(store,clock);store.fail=None;store.hide_reads=False
        self.assertTrue(call(ns,store,r,'hidden',outcome='unsafe')['ok'])
        self.assertEqual(snapshot(store,clock),before)

    def test_phase23_negative_control_duplicate_unsafe_changes_state(self):
        ns,clock,store,r=setup(BASE)
        for i in range(3):call(ns,store,r,f'good-{i}')
        result=call(ns,store,r,'good-0',outcome='unsafe')
        self.assertEqual(result['proposal_status'],'rolled_back')
        self.assertEqual(result['real_bad_count'],0)
        self.assertEqual(result['rollback_evidence_ids'],[])


if __name__=='__main__':unittest.main(verbosity=2)
