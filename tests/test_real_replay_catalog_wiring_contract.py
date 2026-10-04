"""AST wiring and pure failure-shape checks only; never run replay or Runtime."""
from __future__ import annotations
import ast
from pathlib import Path
from typing import Any
import unittest

ROOT=Path(__file__).resolve().parents[1]
GATE=ROOT/'eimemory/governance/l5/real_replay_gate.py'
READINESS=ROOT/'eimemory/governance/l5/l5_readiness.py'
TASK=ROOT/'eimemory/evaluation/task_replay.py'

def function(path,name):
    return next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.FunctionDef) and n.name==name)

def calls(node,name):
    return [n for n in ast.walk(node) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id==name]

def pure_load(path,name,ns,constants=False):
    nodes=[n for n in ast.parse(path.read_text()).body if (isinstance(n,ast.FunctionDef) and n.name==name)
           or (constants and isinstance(n,ast.Assign))]
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),str(path),'exec'),ns)

class CatalogWiringContracts(unittest.TestCase):
    def test_summary_accepts_only_explicit_optional_catalog(self):
        node=function(GATE,'build_verified_real_replay_summary')
        defaults={a.arg:d for a,d in zip(node.args.kwonlyargs,node.args.kw_defaults)}
        self.assertIn('catalog',defaults)
        self.assertIsInstance(defaults['catalog'],ast.Constant)
        self.assertIsNone(defaults['catalog'].value)
        call=calls(node,'validate_real_replay_source')[0]
        kwargs={k.arg:ast.unparse(k.value) for k in call.keywords}
        self.assertEqual(kwargs['catalog'],'catalog')
        self.assertNotIn('legacy_compatibility',kwargs)
        text=ast.unparse(node)
        self.assertNotIn('resolve_application_capability_catalog',text)
        self.assertNotIn('capability_catalog',text)
        self.assertNotIn("get('catalog')",text)

    def test_builder_forwards_the_already_resolved_authority(self):
        node=function(READINESS,'build_l5_readiness_report')
        call=calls(node,'build_verified_real_replay_summary')[0]
        self.assertEqual({k.arg:ast.unparse(k.value) for k in call.keywords}['catalog'],'active_catalog')
        resolved=calls(node,'_resolve_readiness_catalog')
        self.assertEqual(len(resolved),1)
        self.assertIn('active_catalog',ast.unparse(node))

    def test_legacy_status_recheck_gets_no_invented_authority(self):
        node=function(READINESS,'_readiness_gate_evaluate')
        call=calls(node,'build_verified_real_replay_summary')[0]
        self.assertNotIn('catalog',{k.arg for k in call.keywords})
        self.assertNotIn('capability_catalog',ast.unparse(node))

    def test_existing_validator_keeps_missing_catalog_rejection(self):
        node=function(TASK,'validate_real_replay_source')
        call=calls(node,'build_outcome_trace_record')[0]
        self.assertEqual({k.arg:ast.unparse(k.value) for k in call.keywords}['catalog'],'catalog')
        builder=function(ROOT/'eimemory/experience/outcome.py','build_outcome_trace_record')
        text=ast.unparse(builder)
        self.assertIn('if catalog is None and (not legacy_compatibility)',text)
        self.assertIn('trusted capability catalog is required for a dynamic capability contract',text)
        ns={'Any':Any}
        pure_load(TASK,'_outcome_trace_build_failure_reason',ns)
        self.assertEqual(ns['_outcome_trace_build_failure_reason'](ValueError('trusted capability catalog is required for a dynamic capability contract')),'dynamic_capability_catalog_required')
        self.assertEqual(ns['_outcome_trace_build_failure_reason'](ValueError('malformed catalog')),'invalid_outcome_trace_payload')

    def test_thresholds_and_empty_failure_shape_are_unchanged(self):
        ns={'Any':Any,'REAL_PROVENANCE_CONTRACT':'verified_real_replay.v1'}
        pure_load(GATE,'_empty_summary',ns,constants=True)
        self.assertEqual(ns['MIN_VERIFIED_REAL_REPLAY_SAMPLES'],10)
        # Current evidence summary counts task types; dynamic catalog coverage
        # is checked by readiness, so do not reinstall the old fixed-five gate.
        self.assertNotIn('MIN_VERIFIED_REAL_REPLAY_TASK_TYPES', ns)
        self.assertEqual(ns['MIN_VERIFIED_REAL_REPLAY_PASS_RATE'],0.8)
        got=ns['_empty_summary'](package_tree_digest='synthetic-digest',reason='current_code_replay_missing')
        self.assertIs(got['ok'],False)
        self.assertEqual(got['sample_count'],0)
        self.assertEqual(got['pass_rate'],0)

if __name__=='__main__':
    unittest.main(verbosity=2)
