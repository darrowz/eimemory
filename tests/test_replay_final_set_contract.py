"""Pure AST contracts only; no repository import, Runtime, store, or replay."""
from __future__ import annotations
import ast
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any
import unittest

ROOT=Path(__file__).resolve().parents[1]
DATASET=ROOT/'eimemory/governance/learning/replay_dataset.py'

def load(path,ns,names=None,constants=False):
    tree=ast.parse(path.read_text(),filename=str(path))
    nodes=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and (names is None or n.name in names))
           or (constants and isinstance(n,(ast.Assign,ast.AnnAssign)))]
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),str(path),'exec'),ns)

NS={'Any':Any,'Counter':Counter,'re':re,'json':json,'sha256':sha256}
load(ROOT/'eimemory/governance/learning/replay_quality.py',NS,constants=True)
load(ROOT/'eimemory/governance/learning/learning_state.py',NS,{'stable_semantic_key'})
load(DATASET,NS,{'_prepare_replay_dataset_cases','_finalize_replay_candidates','_deduplicate_replay_cases','_dedupe_cases','_case_identity_key','_exact_case_digest','_case_fingerprint','_first_text'},constants=True)

class FinalSetContracts(unittest.TestCase):
    def catalog(self,**updates):
        return {'source':'capability_evaluation_catalog','case_id':'case-A','target_capability':'inventory.check',
                'capability_revision_id':'revision-A','provider_binding_id':'binding-A','eval_spec_id':'spec-A',
                'evaluation_case_digest':'a'*64,'query':'Inspect inventory','expected':'Check counts',
                'expected_text':['Check counts'],'task_type':'inventory.check',**updates}

    def finish(self,cases,limit=50,normalize_limit=150):
        quality=NS['govern_replay_cases'](cases,limit=normalize_limit)
        return NS['_finalize_replay_candidates'](quality,limit=limit)

    def test_every_catalog_identity_dimension_is_exact(self):
        base=self.catalog()
        for field in ('case_id','target_capability','capability_revision_id','provider_binding_id','eval_spec_id','evaluation_case_digest'):
            for value in (base[field]+'-different',base[field].swapcase()):
                with self.subTest(field=field,value=value):
                    other={**base,field:value}
                    self.assertEqual(len(NS['_dedupe_cases']([base,other])),2)

    def test_catalog_duplicate_and_conflict(self):
        a=self.catalog()
        duplicate=NS['_deduplicate_replay_cases']([a,dict(a)])
        self.assertEqual(len(duplicate['cases']),1)
        self.assertEqual(duplicate['duplicate_dropped_count'],1)
        conflict=NS['_deduplicate_replay_cases']([a,dict(a),self.catalog(expected='Different immutable fixture')])
        self.assertEqual(conflict['cases'],[])
        self.assertEqual(conflict['identity_filter_reasons'],{'catalog_identity_conflict':3})
        self.assertEqual(conflict['duplicate_dropped_count'],0)

    def test_missing_catalog_identity_never_text_fallback(self):
        for field in ('case_id','target_capability','capability_revision_id','provider_binding_id','eval_spec_id','evaluation_case_digest'):
            for value in ('',None,True,{}):
                with self.subTest(field=field,value=value):
                    got=NS['_deduplicate_replay_cases']([self.catalog(**{field:value})])
                    self.assertEqual(got['cases'],[])
                    self.assertEqual(got['identity_filter_reasons'],{'catalog_identity_missing':1})

    def test_source_ids_preserve_distinct_traces_and_legacy_remains_compatible(self):
        a={'source':'outcome_trace','source_record_id':'ref_A','query':'Inspect inventory','expected':'Count bins'}
        self.assertEqual(len(NS['_dedupe_cases']([a,{**a,'source_record_id':'ref_a'}])),2)
        self.assertEqual(len(NS['_dedupe_cases']([a,dict(a)])),1)
        legacy={'source':'operator_correction','query':'Inspect inventory','expected':'Count bins'}
        self.assertEqual(len(NS['_dedupe_cases']([legacy,{**legacy,'query':'INSPECT INVENTORY'}])),1)

    def test_final_breakdown_and_quality_are_one_retained_set(self):
        a=self.catalog()
        got=self.finish([a,dict(a),dict(a)],limit=1)
        breakdown=got['case_quality_breakdown']
        self.assertEqual(breakdown['accepted'],1)
        self.assertEqual(sum(breakdown[k] for k in ('high_quality','medium_quality','low_quality')),1)
        self.assertEqual(got['quality_score'],got['cases'][0]['quality_score'])
        self.assertEqual(got['selection_breakdown']['duplicate_dropped'],2)
        self.assertNotIn('real_provenance_ok',got['cases'][0])

    def test_filter_and_budget_counts_are_separate(self):
        cases=[self.catalog(case_id=f'case-{i}') for i in range(4)] + [{'query':'timeout'}]
        got=self.finish(cases,limit=1,normalize_limit=3)
        self.assertEqual(got['case_quality_breakdown']['filtered'],1)
        self.assertEqual(got['selection_breakdown'],{'normalization_budget_dropped':1,'duplicate_dropped':0,'identity_rejected':0,'final_budget_dropped':2})
        self.assertEqual(got['quality_score'],round(got['cases'][0]['quality_score']-0.01,3))

    def test_empty_final_has_zero_statistics(self):
        got=self.finish([self.catalog(case_id='')])
        self.assertEqual(got['cases'],[])
        self.assertEqual(got['quality_score'],0)
        self.assertEqual(got['case_quality_breakdown']['accepted'],0)

    def test_full_fingerprint_changes_for_tail_identity_content_and_case(self):
        cases=[self.catalog(case_id=f'case-{i}') for i in range(6)]
        before=NS['_case_fingerprint'](cases)
        self.assertEqual(before,NS['_case_fingerprint']([dict(c) for c in cases]))
        for field,value in [('case_id','tail-other'),('expected','new expected'),('expected','CHECK COUNTS')]:
            changed=[*cases[:5],{**cases[5],field:value}]
            with self.subTest(field=field,value=value):
                self.assertNotEqual(before,NS['_case_fingerprint'](changed))
        self.assertNotEqual(before,NS['stable_semantic_key'](*[c['case_id'] for c in cases[:5]]))

    def test_fixed_tail_conflict_is_checked_before_dataset_budget(self):
        # Exact independent fixture shape, previously hidden after the 3x cut.
        def case(**updates):
            return {'source':'capability_evaluation_catalog','case_id':'case-A','target_capability':'synthetic.inventory',
                'capability_revision_id':'revision-A','provider_binding_id':'binding-A','eval_spec_id':'spec-A',
                'evaluation_case_digest':'a'*64,'query':'Inspect the inventory counts.','expected':'Check all bins.',
                'expected_text':['Check all bins.'],'task_type':'synthetic.inventory',**updates}
        cases=[case(),case(case_id='case-b'),case(case_id='case-c'),case(expected='Contradictory tail fixture.')]
        quality,got=NS['_prepare_replay_dataset_cases'](cases,limit=1)
        self.assertEqual(len(quality['cases']),4)
        self.assertEqual([c['case_id'] for c in got['cases']],['case-b'])
        self.assertEqual(got['identity_filter_reasons'],{'catalog_identity_conflict':2})
        self.assertEqual(got['selection_breakdown'],{'normalization_budget_dropped':0,'duplicate_dropped':0,
                                                   'identity_rejected':2,'final_budget_dropped':1})
        self.assertEqual(got['case_quality_breakdown']['accepted'],1)
        self.assertEqual(sum(got['case_quality_breakdown'][k] for k in ('high_quality','medium_quality','low_quality')),1)
        self.assertEqual(got['quality_score'],got['cases'][0]['quality_score'])

    def test_full_dataset_same_identity_duplicate_and_distinct_identity_controls(self):
        a=self.catalog()
        quality,got=NS['_prepare_replay_dataset_cases']([a,self.catalog(case_id='b'),self.catalog(case_id='c'),dict(a)],limit=1)
        self.assertEqual(got['cases'][0]['case_id'],'case-A')
        self.assertEqual(got['identity_filter_reasons'],{})
        self.assertEqual(got['selection_breakdown'],{'normalization_budget_dropped':0,'duplicate_dropped':1,
                                                   'identity_rejected':0,'final_budget_dropped':2})
        quality,got=NS['_prepare_replay_dataset_cases']([self.catalog(case_id=str(i)) for i in range(4)],limit=1)
        self.assertEqual(len(got['cases']),1)
        self.assertEqual(got['identity_filter_reasons'],{})
        self.assertEqual(got['selection_breakdown']['final_budget_dropped'],3)
        self.assertEqual(got['selection_breakdown']['duplicate_dropped'],0)

    def test_actual_persistence_and_return_wiring_is_static_only(self):
        tree=ast.parse(DATASET.read_text())
        build=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_replay_dataset')
        text=ast.unparse(build)
        self.assertIn('_prepare_replay_dataset_cases(cases, limit=budget)',text)
        self.assertIn('_case_fingerprint(deduped_cases)',text)
        self.assertNotIn('deduped_cases[:5]',text)
        self.assertEqual(text.count("'quality_score': finalized['quality_score']"),2)
        self.assertEqual(text.count("'selection_breakdown': finalized['selection_breakdown']"),2)
        self.assertIn('REPLAY_DATASET_IDENTITY_VERSION',text)

if __name__=='__main__':
    unittest.main(verbosity=2)
