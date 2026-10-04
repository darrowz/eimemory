"""Finite pure step-parsing tests. No skill text is executed or evaluated."""
from __future__ import annotations
import ast
from itertools import product
from pathlib import Path
import re
from typing import Any
import unittest

ROOT=Path(__file__).resolve().parents[1]


def helpers(root=ROOT):
    ns={'Any':Any,'re':re}
    names={'_unit_text','_dict_get','_clean','_extract_steps','_section_after','_sentences','_sentence','_summary','_status_for_candidate'}
    path=root/'eimemory/governance/learning/skill_candidate.py'
    nodes=[n for n in ast.parse(path.read_text(),filename=str(path)).body if isinstance(n,ast.FunctionDef) and n.name in names]
    module=ast.Module(body=[ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0),*nodes],type_ignores=[])
    exec(compile(ast.fix_missing_locations(module),str(path),'exec'),ns)
    return ns


def parse(text,root=ROOT):
    ns=helpers(root)
    return ns['_extract_steps'](ns['_unit_text']({'detail':text}))


class StepBoundaryContract(unittest.TestCase):
    def test_newline_numbered_procedure(self):
        self.assertEqual(parse('Steps:\n1. Inspect input\n2. Write draft\n3. Verify output\nAcceptance: output exists'),['Inspect input','Write draft','Verify output'])

    def test_terminal_punctuation_and_crlf(self):
        self.assertEqual(parse('Steps:\r\n1. Inspect input.\r\n2. Write draft.\r\n3. Verify output.'),['Inspect input','Write draft','Verify output'])

    def test_parenthesis_and_inline_items(self):
        for text in ('Steps: 1) Inspect input; 2) Write draft; 3) Verify output','Steps: 1. Inspect input 2. Write draft 3. Verify output'):
            self.assertEqual(parse(text),['Inspect input','Write draft','Verify output'])

    def test_item_continuation_and_internal_periods_preserved(self):
        self.assertEqual(parse('Procedure:\n1. Inspect package.module\n   and compare output\n2. Record output'),['Inspect package.module and compare output','Record output'])

    def test_adjacent_sections_do_not_become_steps(self):
        for section in ('Acceptance criteria:','Acceptance:','Tools:','Dependencies:','Failure handling:','Trigger:'):
            self.assertEqual(parse('Steps:\n1. Inspect input\n2. Verify output\n'+section+' SECTION_SENTINEL'),['Inspect input','Verify output'])

    def test_other_procedure_markers(self):
        for label in ('Step:','Procedure:','Workflow:'):
            self.assertEqual(parse(label+'\n1) Inspect input\n2) Verify output'),['Inspect input','Verify output'])

    def test_versions_and_decimals_are_not_list_markers(self):
        self.assertEqual(parse('Steps: Inspect package v1.2.3. Verify value 2.0.'),['Inspect package v1.2.3','Verify value 2.0'])
        self.assertEqual(parse('Steps: Inspect a1.2 configuration. Verify output.'),['Inspect a1.2 configuration','Verify output'])

    def test_no_number_fallback_and_empty_input(self):
        self.assertEqual(parse('Steps:\nInspect input\nVerify output'),['Inspect input','Verify output'])
        self.assertEqual(parse(''),[])

    def test_existing_limits_and_summary_contract_remain(self):
        ns=helpers()
        text='Steps:\n'+'\n'.join(f'{i}. Inspect item {i}' for i in range(1,9))
        self.assertEqual(len(parse(text)),6)
        self.assertLessEqual(len(parse('Steps:\n1. '+'x'*400)[0]),240)
        self.assertEqual(ns['_summary']('  a\n b  '),'a b')
        self.assertEqual(len(ns['_summary']('x'*400)),240)

    def test_unit_boundaries_and_readiness_step_count(self):
        ns=helpers()
        unit={'title':'title','summary':'summary','detail':'Steps:\n1. Inspect input\n2. Write draft\n3. Verify output','content':{'text':'Acceptance: output exists'}}
        text=ns['_unit_text'](unit)
        self.assertIn('title\nsummary\nSteps:\n',text)
        steps=ns['_extract_steps'](text)
        self.assertEqual(steps,['Inspect input','Write draft','Verify output'])
        self.assertEqual(ns['_status_for_candidate'](steps=steps,acceptance=['exists'],source_trust=0.9,risk_level='low'),'sandbox_ready')

    def test_fixed_sentence_final_integer_and_version_regressions(self):
        cases=[
            ('Steps: 1. Set retry limit to 3. 2. Verify output.',['Set retry limit to 3','Verify output']),
            ('Steps: 1. Verify Python 3. 2. Inspect output.',['Verify Python 3','Inspect output']),
            ('Steps:\n1. Set retry limit to 3.\n2. Verify output.',['Set retry limit to 3','Verify output']),
        ]
        for text,expected in cases:
            self.assertEqual(parse(text),expected)

    def test_fixed_parenthetical_reference_stays_in_its_step(self):
        text='Steps: 1. Compare equation (2) with the reference. 2. Verify output.'
        expected=['Compare equation (2) with the reference','Verify output']
        self.assertEqual(parse(text),expected)

    def test_bounded_32_marker_format_controls(self):
        count=0
        for label,separator,marker,ending,indent in product(('Steps:','Workflow:'),('\n',' '),('.',')'),('','.'),('','  ')):
            text=label+separator+separator.join(f'{indent}{index}{marker} {item}{ending}' for index,item in enumerate(('Inspect input','Write draft','Verify output'),1))
            self.assertEqual(parse(text),['Inspect input','Write draft','Verify output'])
            count+=1
        self.assertEqual(count,32)



if __name__=='__main__':unittest.main(verbosity=2)
