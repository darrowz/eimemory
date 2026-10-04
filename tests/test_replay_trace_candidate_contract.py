"""Pure AST contracts only; no eimemory import, Runtime, store, replay or evaluator."""
from __future__ import annotations
import ast
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'eimemory/governance/learning/replay_dataset.py'

def pure_functions(path, names, namespace, constants=False):
    tree = ast.parse(path.read_text(), filename=str(path))
    nodes = [node for node in tree.body if (isinstance(node, ast.FunctionDef) and node.name in names)
             or (constants and isinstance(node, (ast.Assign, ast.AnnAssign)))]
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), 'exec'), namespace)

NS = {'Any': Any, 'Mapping': Mapping}
pure_functions(ROOT / 'eimemory/metadata.py', {'business_metadata','split_metadata','_dict_value','_has_runtime_value'}, NS, constants=True)
pure_functions(DATASET, {'_outcome_trace_source_record_id','_outcome_trace_replay_fields','_trace_text','_trace_expected_points'}, NS)

class TraceCandidateContracts(unittest.TestCase):
    def scope(self, **updates):
        return SimpleNamespace(**({'tenant_id':'tenant','agent_id':'agent','workspace_id':'workspace','user_id':'user'} | updates))

    def record(self, **updates):
        return SimpleNamespace(**({'kind':'reflection','source':'eimemory.experience.outcome_trace','status':'active',
            'record_id':'ref_actual_stored','scope':self.scope(),
            'meta':{'report_type':'outcome_trace'},'content':{'schema_version':'outcome_trace.v1','payload':{}}} | updates))

    def fields(self, content, **kwargs):
        return NS['_outcome_trace_replay_fields'](content, **kwargs)

    def test_canonical_fields_win(self):
        got=self.fields({'input_summary':'wrong wrapper','correction':'wrong wrapper', 'task_type':'wrong',
            'payload':{'input_summary':'Inspect the inventory','task_type':'inventory.check','correction_from_user':'Use counted inventory',
                       'expected_text':['Count confirmed','Explain discrepancies']}})
        self.assertEqual(got['query'],'Inspect the inventory')
        self.assertEqual(got['task_type'],'inventory.check')
        self.assertEqual(got['correction_from_user'],'Use counted inventory')
        self.assertEqual(got['expected_text'],['Count confirmed','Explain discrepancies'])
        self.assertEqual(got['correction_source'],'correction_from_user')
        self.assertNotIn('real_provenance_ok',got)

    def test_bad_payload_never_falls_back(self):
        for payload in (None,[],True,'bad',{}):
            for legacy in (False,True):
                with self.subTest(payload=payload,legacy=legacy):
                    self.assertIsNone(self.fields({'payload':payload,'input_summary':'Wrapper task'},legacy_compatibility=legacy))

    def test_absent_payload_requires_explicit_legacy(self):
        content={'input_summary':'Legacy task','correction':'Legacy correction'}
        self.assertIsNone(self.fields(content))
        self.assertEqual(self.fields(content,legacy_compatibility=True)['query'],'Legacy task')

    def test_types_do_not_become_repr(self):
        got=self.fields({'payload':{'input_summary':{'not':'text'},'query':'Inspect inventory',
                                    'expected':['Count bins',{'not':'text'},False], 'correction':{'not':'text'},'feedback':['not text']}})
        self.assertEqual(got['query'],'Inspect inventory')
        self.assertEqual(got['expected_text'],['Count bins'])
        self.assertEqual(got['expected'],'Count bins')
        self.assertEqual(got['correction_from_user'],'')
        self.assertEqual(got['correction_source'],'')

    def test_feedback_alias_is_marked_not_verified(self):
        got=self.fields({'payload':{'input_summary':'Inspect inventory','feedback':'Check all bins','outcome':{'success':False}}})
        self.assertEqual(got['correction_source'],'feedback')
        self.assertNotIn('verified',got)
        self.assertIs(got['payload']['outcome']['success'],False)

    def test_formal_nested_feedback_aliases_keep_text_and_origin(self):
        for key in ("correction_from_user", "correction"):
            with self.subTest(key=key):
                got=self.fields({'payload':{'input_summary':'Inspect inventory',
                    'feedback':{key:'Count every bin'},'outcome':{'success':False,'status':'failed'}}})
                self.assertEqual(got['correction_from_user'],'Count every bin')
                self.assertEqual(got['correction_source'],'feedback.'+key)
                self.assertEqual(got['expected'],'Count every bin')
                self.assertIs(got['payload']['outcome']['success'],False)
                self.assertNotIn('real_provenance_ok',got)
        got=self.fields({'payload':{'input_summary':'Inspect inventory','feedback':{'unrelated':'not a correction'}}})
        self.assertEqual(got['correction_from_user'],'')
        self.assertEqual(got['correction_source'],'')
        got=self.fields({'payload':{'input_summary':'Inspect inventory','feedback':{'correction':{'not':'text'}}}})
        self.assertEqual(got['correction_from_user'],'')

    def test_direct_correction_precedes_nested_feedback(self):
        got=self.fields({'payload':{'input_summary':'Inspect inventory','correction':'Direct correction',
                                   'feedback':{'correction_from_user':'Nested correction','correction':'Other'}}})
        self.assertEqual(got['correction_from_user'],'Direct correction')
        self.assertEqual(got['correction_source'],'correction')
        got=self.fields({'payload':{'input_summary':'Inspect inventory',
                                   'feedback':{'correction_from_user':'Explicit user correction','correction':'Other'}}})
        self.assertEqual(got['correction_from_user'],'Explicit user correction')
        self.assertEqual(got['correction_source'],'feedback.correction_from_user')

    def test_source_id_is_stored_not_payload_and_legacy_id_is_preserved(self):
        for stored_id in ('ref_actual_stored','ref_historical_stored'):
            record=self.record(record_id=stored_id,content={'schema_version':'outcome_trace.v1','payload':{'source_record_id':'forged'}})
            self.assertEqual(NS['_outcome_trace_source_record_id'](record,scope=self.scope()),stored_id)

    def test_source_requires_exact_envelope_and_scope(self):
        invalid=[{'kind':'memory'},{'source':'operator.correction'},{'status':'inactive'},{'record_id':''},
                 {'content':{'schema_version':'other'}},{'meta':{'report_type':'other'}}, {'scope':SimpleNamespace()}]
        invalid += [{'scope':self.scope(**{field:'wrong'})} for field in ('tenant_id','agent_id','workspace_id','user_id')]
        for updates in invalid:
            with self.subTest(updates=updates):
                self.assertEqual(NS['_outcome_trace_source_record_id'](self.record(**updates),scope=self.scope()),'')

    def test_actual_producer_wiring_is_static_only(self):
        tree=ast.parse(DATASET.read_text())
        producer=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_cases_from_outcome_traces')
        text=ast.unparse(producer)
        self.assertIn("'source_record_id': source_record_id",text)
        self.assertNotIn('record.title',text)
        self.assertNotIn('record.summary',text)
        self.assertNotIn("'real_provenance_ok'",text)
        self.assertNotIn('content.get(\'payload\', {})',text)
        validator=ast.parse((ROOT/'eimemory/evaluation/task_replay.py').read_text())
        source_validator=next(n for n in validator.body if isinstance(n,ast.FunctionDef) and n.name=='validate_real_replay_source')
        self.assertIn('unsuccessful_source',ast.unparse(source_validator))

if __name__=='__main__':
    unittest.main(verbosity=2)
