from eimemory.evaluation.recall_latency import summarize_latency


def test_slow_path_does_not_relax_ordinary_latency():
    report = summarize_latency([{'latency_ms':8000,'latency_tier':'assisted'},
                                {'latency_ms':1000,'latency_tier':'ordinary'}])
    assert report['passed'] and report['assisted_count'] == 1
    assert not summarize_latency([{'latency_ms':8000}])['passed']
    assert not summarize_latency([{'latency_ms':10001,'latency_tier':'assisted'}])['passed']
