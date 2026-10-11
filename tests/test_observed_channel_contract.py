from eimemory.evaluation.real_query_gate import production_real_query_active_channel_contract
from eimemory.evaluation.real_query_schema import production_real_query_policy_payload


def test_single_observed_channel_does_not_require_other_products():
    result = production_real_query_active_channel_contract({'hermes': 15})
    assert result['ok']
    assert result['required_channels'] == ['hermes']
    assert result['required_case_count'] == 15


def test_missing_samples_never_pass():
    assert not production_real_query_active_channel_contract({})['ok']
    assert not production_real_query_active_channel_contract({'hermes': 4})['ok']


def test_observed_channels_have_no_fixed_per_channel_quota():
    result = production_real_query_active_channel_contract({'hermes': 15, 'codex': 1})
    assert result['ok']
    assert result['required_channels'] == ['codex', 'hermes']
    assert result['required_per_channel'] == 0
    assert result['per_channel_case_count'] == {'codex': 1, 'hermes': 15, 'openclaw': 0}


def test_policy_certifies_observed_dataset_channels_only():
    policy = production_real_query_policy_payload()
    assert policy['schema'] == 'production_recall_gate_policy.v4'
    assert policy['required_channels'] == 'observed_dataset_channels'
    assert policy['coverage_scope'] == 'dataset_only_not_all_installed_channels'
    assert policy['required_case_count'] == policy['required_label_count'] == 15
