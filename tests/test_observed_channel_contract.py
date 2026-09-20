from eimemory.evaluation.real_query_gate import production_real_query_active_channel_contract


def test_single_observed_channel_does_not_require_other_products():
    result = production_real_query_active_channel_contract({'hermes': 15})
    assert result['ok']
    assert result['required_channels'] == ['hermes']
    assert result['required_case_count'] == 15


def test_missing_samples_never_pass():
    assert not production_real_query_active_channel_contract({})['ok']
    assert not production_real_query_active_channel_contract({'hermes': 4})['ok']


def test_each_observed_channel_still_needs_coverage():
    result = production_real_query_active_channel_contract({'hermes': 15, 'codex': 1})
    assert not result['ok']
    assert 'required_channel_coverage_missing' in result['blocked_reasons']
