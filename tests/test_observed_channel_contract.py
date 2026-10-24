from eimemory.evaluation.real_query_gate import production_real_query_active_channel_contract
from eimemory.evaluation.real_query_schema import production_real_query_policy_payload, production_real_query_policy_digest


def test_single_observed_channel_does_not_require_other_products():
    result = production_real_query_active_channel_contract({'hermes': 3})
    assert result['ok']
    assert result['required_channels'] == ['hermes']
    assert result['required_case_count'] == 3


def test_missing_samples_never_pass():
    assert not production_real_query_active_channel_contract({})['ok']
    assert not production_real_query_active_channel_contract({'hermes': 2})['ok']


def test_observed_channels_have_no_fixed_per_channel_quota():
    result = production_real_query_active_channel_contract({'hermes': 2, 'codex': 1})
    assert result['ok']
    assert result['required_channels'] == ['codex', 'hermes']
    assert result['required_per_channel'] == 0
    assert result['per_channel_case_count'] == {'codex': 1, 'hermes': 2, 'openclaw': 0}


def test_policy_certifies_observed_dataset_channels_only():
    policy = production_real_query_policy_payload()
    assert policy['schema'] == 'production_recall_gate_policy.v6'
    assert policy['required_channels'] == 'observed_dataset_channels'
    assert policy['coverage_scope'] == 'dataset_only_not_all_installed_channels'
    assert policy['required_case_count'] == policy['required_label_count'] == 3


def test_historical_policy_floors_and_digests_are_immutable():
    historical = {
        'production_recall_gate_policy.v5': '638047870e5e68f19f99c1836d39773677b4e201dc94e161e11af3dcf2c35f7c',
        'production_recall_gate_policy.v4': '59d180bbbd9b86077de197d0cd67e317896c67665e89dbbc71914553c9ef524f',
        'production_recall_gate_policy.v3': 'f37796af946bd6af594a9112fcf2079f75edcc366e19e9e0e7834f1b288dadad',
        'production_recall_gate_policy.v2': 'e8290f3bcea544b9a01130144afd1c301c14808e5fa74b725b813cdfbbca95e2',
    }
    for schema, digest in historical.items():
        policy = production_real_query_policy_payload(policy_schema=schema)
        if schema == 'production_recall_gate_policy.v5':
            assert policy['required_case_count'] == policy['required_label_count'] == 3
            assert policy['thresholds']['mrr'] == 0.80
            assert policy['thresholds']['ndcg_at_5'] == 0.80
        else:
            assert policy['required_case_count'] == policy['required_label_count'] == 15
        assert production_real_query_policy_digest(policy_schema=schema) == digest
        if schema != 'production_recall_gate_policy.v5':
            assert not production_real_query_active_channel_contract({'hermes': 3}, policy_schema=schema)['ok']
    current = production_real_query_policy_payload()
    assert current['thresholds']['mrr'] == 0.50
    assert current['thresholds']['ndcg_at_5'] == 0.55
    assert current['per_agent_force_policy'] == 'g2c_traffic_agents_with_samples.v1'
    assert production_real_query_policy_digest() not in historical.values()
