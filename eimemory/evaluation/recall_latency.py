"""User-approved 3 s ordinary / 10 s assisted recall acceptance contract."""
from math import isfinite
from .metrics import percentile

POLICY = 'recall-latency-fast3s-assisted10s.v1'


def tier(explanation):
    selector = (explanation or {}).get('relevance_selector', {})
    return 'assisted' if selector.get('caller_assistance', {}).get('calls') == 1 else 'ordinary'


def summarize_latency(samples):
    ordinary, assisted = [], []
    for sample in samples:
        value = sample.get('latency_ms')
        if type(value) not in (float,int) or not isfinite(value) or value < 0:
            return {'policy':POLICY, 'passed':False, 'reason':'latency_invalid'}
        (assisted if sample.get('latency_tier') == 'assisted' else ordinary).append(value)
    fast_p95 = percentile(ordinary,95) if ordinary else 0
    slow_max = max(assisted) if assisted else 0
    return {'policy':POLICY, 'ordinary_count':len(ordinary), 'assisted_count':len(assisted),
        'ordinary_p95_ms':fast_p95, 'assisted_max_ms':slow_max,
        'ordinary_limit_ms':3000, 'assisted_limit_ms':10000,
        'passed':fast_p95 <= 3000 and slow_max <= 10000}
