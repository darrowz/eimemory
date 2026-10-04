"""Finite-number contract regression; no runtime, DB, provider or subprocess."""
import math
import pytest
from eimemory.governance.promotion.harness_patch import GateVerdict, HarnessGate, HarnessSurface, ProposalCard

@pytest.fixture
def gate():
    return HarnessGate(ProposalCard(target_surface=HarnessSurface.RUNTIME_POLICY,
        evidence_record_ids=('synthetic',), expected_delta=.1, target_agent='synthetic',
        risk_tier='L1', rollback_plan='local-only', diff_lines=1, diff_tokens=1))

@pytest.mark.parametrize('field', ['held_in', 'held_out', 'baseline_in', 'baseline_out'])
@pytest.mark.parametrize('invalid', [float('nan'),float('inf'),float('-inf'),True,False,'not-a-number',10**1000])
def test_invalid_numeric_evidence_rejected(gate, field, invalid):
    values={'held_in':.9,'held_out':.9,'baseline_in':.8,'baseline_out':.8}
    values[field]=invalid
    result=gate.evaluate(held_in_scores={'accuracy':values['held_in']},held_out_scores={'accuracy':values['held_out']},baseline_held_in=values['baseline_in'],baseline_held_out=values['baseline_out'])
    assert result.verdict is GateVerdict.REJECT
    assert 'finite' in result.reason

@pytest.mark.parametrize('field', ['held_in','held_out','baseline_in'])
def test_explicit_none_invalid_where_not_missing_split_marker(gate,field):
    values={'held_in':.9,'held_out':.9,'baseline_in':.8,'baseline_out':.8}; values[field]=None
    result=gate.evaluate(held_in_scores={'accuracy':values['held_in']},held_out_scores={'accuracy':values['held_out']},baseline_held_in=values['baseline_in'],baseline_held_out=values['baseline_out'])
    assert result.verdict is GateVerdict.REJECT
    assert 'finite' in result.reason

@pytest.mark.parametrize('invalid', [float('nan'),float('inf'),True,'not-a-number'])
def test_invalid_held_in_cannot_warn_when_split_missing(gate,invalid):
    result=gate.evaluate(held_in_scores={'accuracy':invalid},held_out_scores=None,baseline_held_in=.8,baseline_held_out=None)
    assert result.verdict is GateVerdict.REJECT

@pytest.mark.parametrize('field', ['held_out','baseline_out'])
def test_partial_held_out_invalid_evidence_cannot_warn(gate,field):
    scores={'accuracy':float('nan')} if field=='held_out' else None
    baseline=None if field=='held_out' else float('nan')
    result=gate.evaluate(held_in_scores={'accuracy':.9},held_out_scores=scores,baseline_held_in=.8,baseline_held_out=baseline)
    assert result.verdict is GateVerdict.REJECT

@pytest.mark.parametrize('field', ['held_in','held_out'])
def test_finite_inputs_that_overflow_delta_are_rejected(gate,field):
    values={'held_in':.9,'held_out':.9,'baseline_in':.8,'baseline_out':.8}
    values[field]=1e308; values['baseline_in' if field=='held_in' else 'baseline_out']=-1e308
    result=gate.evaluate(held_in_scores={'accuracy':values['held_in']},held_out_scores={'accuracy':values['held_out']},baseline_held_in=values['baseline_in'],baseline_held_out=values['baseline_out'])
    assert result.verdict is GateVerdict.REJECT
    assert 'finite' in result.reason

@pytest.mark.parametrize('score,baseline', [(.9,.8),('0.9','0.8'),(2.,1.),(-1.,-2.)])
def test_finite_compatible_scales_and_numeric_strings_are_preserved(gate,score,baseline):
    result=gate.evaluate(held_in_scores={'accuracy':score},held_out_scores={'accuracy':score},baseline_held_in=baseline,baseline_held_out=baseline)
    assert result.verdict is GateVerdict.ACCEPT
    assert math.isfinite(result.delta)

def test_missing_split_policy_preserved(gate):
    args=dict(held_in_scores={'accuracy':.9},held_out_scores=None,baseline_held_in=.8,baseline_held_out=None)
    assert gate.evaluate(**args).verdict is GateVerdict.WARN
    gate.allow_warn_on_missing=False
    assert gate.evaluate(**args).verdict is GateVerdict.REJECT
