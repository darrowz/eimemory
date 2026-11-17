"""Explicit synthetic producer authority for learning tests that need credit.

Legacy/unsigned fixtures intentionally remain unknown elsewhere. These helpers
exercise the real private-producer bridge and receipt signing, not a loader flag.
"""
from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.retrieval.input_origin import SCHEMA, EXTERNAL, text_digest


def configure_test_origin(monkeypatch, scope):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'effect-origin-authority-0123456789-ABCDEFGH')
    exact = resolve_channel_scope('hermes', scope)
    def binding(*, channel, scope, source_ids, claim=None):
        if channel != 'hermes' or resolve_channel_scope('hermes', scope) != exact or sorted(source_ids) != ['hermes']:
            raise ValueError('synthetic_origin_scope_mismatch')
        return dict(profile_name='synthetic-learning', hermes_home='/synthetic/hermes',
                    natural_sample_eligible=True, binding_kind='business', topology_digest='test')
    monkeypatch.setattr('eimemory.evaluation.hermes_profile_coverage.request_sampling_binding', binding)


def attest_test_input(runtime, params):
    decision = runtime.store.load_proactive_decision(params['decision_id'])
    claim = dict(schema=SCHEMA, origin_kind=EXTERNAL, profile_name='synthetic-learning', platform='feishu',
        home_digest=text_digest('/synthetic/hermes'), actor_digest=text_digest(params['scope']['user_id']),
        chat_digest=text_digest(params['session_id']), message_digest=text_digest(params['turn_id']),
        host_turn_digest=text_digest(params['turn_id']), host_query_digest=decision['query_digest'],
        query_digest=decision['query_digest'], native_receipt_digest=text_digest(params['session_id']+'\0'+params['turn_id']))
    result = EIBrainRPCBridge(runtime).handle(dict(method='adapter.attest_input_origin', params={**params,'origin':claim}),
                                            attestation_producer='hermes')
    assert result['ok'], result
