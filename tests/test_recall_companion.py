from copy import deepcopy
from eimemory.evaluation.recall_companion import quality_reasons


def reports():
    sample = {'online_context_reconstructed':True, 'unavailable':False, 'latency_ms':10,
              'rerun':{'recall_at_5':1, 'reciprocal_rank':1}}
    positive = {'samples':[{**deepcopy(sample), 'channel':channel} for channel in ('codex','hermes','openclaw') for _ in range(5)]}
    negative = {'samples':[{**deepcopy(sample), 'rerun_false_recall':False} for _ in range(20)]}
    return positive, negative


def test_companion_requires_each_channel_original_context_and_availability():
    p, n = reports()
    assert quality_reasons(p, n) == []
    p['samples'][0]['online_context_reconstructed'] = False
    n['samples'][0]['unavailable'] = True
    assert set(quality_reasons(p, n)) == {'original_context_not_reconstructed','retrieval_unavailable'}


def test_companion_recalculates_quality_and_rejects_missing_or_nonfinite_evidence():
    p, n = reports()
    p['samples'][0]['rerun']['recall_at_5'] = float('nan')
    n['samples'][0]['latency_ms'] = float('inf')
    n['samples'].pop()
    reasons = quality_reasons(p, n)
    assert 'original_query_quality_failed:codex:recall_at_5' in reasons
    assert 'latency_invalid' in reasons and 'natural_negative_coverage_missing' in reasons


def test_companion_measures_observed_channels_without_fixed_quotas():
    p, n = reports()
    for sample in p['samples']:
        sample['channel'] = 'codex'
    p['samples'][-1]['channel'] = 'hermes'
    assert quality_reasons(p, n) == []
    p['samples'] = p['samples'][:1]
    assert 'original_query_sample_count_insufficient' in quality_reasons(p, n)
    p['samples'] = []
    assert 'original_query_sample_count_insufficient' in quality_reasons(p, n)


def test_companion_unknown_channel_cannot_escape_quality_checks():
    p, n = reports()
    for sample in p['samples']:
        sample['channel'] = 'foreign'
        sample['rerun']['recall_at_5'] = 0
    assert 'original_query_channel_invalid' in quality_reasons(p, n)
