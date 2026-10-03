"""Shared service version proof must not confer memory/capture authority."""
from dataclasses import asdict
import socket

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.governance.evidence_contract import current_release_identity
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.evaluation.explicit_recall import load_explicit_capture, accept_explicit_query
from test_release_scope_binding import receipt_for_service, RELEASE, SCOPE


@pytest.mark.parametrize('name,tenant', [('hongtu', 'default'), ('hongtai', 'default'),
                                        ('hongxin', 'default'), ('xiaomage', 'other-tenant')])
def test_shared_release_keeps_caller_memory_and_capture_authority(tmp_path, monkeypatch, name, tenant):
    def no_network(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz')
    monkeypatch.delenv('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE', raising=False)
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime._test_runtime_commit = RELEASE.commit
        receipt = runtime.store.append(receipt_for_service())
        receipt.summary = 'PRIVATE deployment receipt marker'
        runtime.store.append(receipt)
        before = receipt.to_dict()
        scope = dict(tenant_id=tenant, agent_id=name, workspace_id='embodied', user_id=name+'-user')
        exact = resolve_channel_scope('hermes', scope)
        private = runtime.store.append(RecordEnvelope.create(kind='memory', title='Private orchid secret',
            summary='Orchid private password marker', scope=SCOPE, source='fixture.private',
            meta={'memory_type': 'durable_fact', 'force_capture': True}))
        own = runtime.store.append(RecordEnvelope.create(kind='memory', title='Orchid service connection',
            summary='Orchid uses rpc.example:7443 and /srv/orchid/data.',
            scope=ScopeRef.from_dict(exact), source='fixture.own',
            meta={'memory_type': 'durable_fact', 'force_capture': True}))
        assert current_release_identity(runtime, exact).receipt_id == receipt.record_id
        assert runtime.store.get_by_id(private.record_id, scope=ScopeRef.from_dict(exact)) is None
        assert runtime.store.get_by_id(receipt.record_id, scope=ScopeRef.from_dict(exact)) is None
        service = AgentRuntimeMemoryService(runtime)
        runtime.proactive.control_percent = 0
        auto = service.proactive_prefetch(channel='hermes', scope=scope, source_ids=['default'],
            session_id=name, turn_id='1', query='Orchid service connection', acceptance_generated=False)
        assert auto['decision_id']
        stored = runtime.store.load_proactive_decision(auto['decision_id'])
        assert stored['scope'] == exact
        result = service.prefetch(channel='hermes', scope=scope, query='Orchid service connection',
            explicit_request={'session_id': name, 'request_id': '2', 'acceptance_generated': False})
        assert 'private password marker' not in str(result)
        assert 'PRIVATE deployment receipt marker' not in str(result)
        capture = load_explicit_capture(runtime, result['capture']['record_id'], scope=exact)
        assert capture.content['release_reference']['scope'] == asdict(receipt.scope)
        assert any(ref['record_ref'] == own.record_id for ref in capture.content['references'])
        assert all(ref['record_ref'] != private.record_id for ref in capture.content['references'])
        old = capture.to_dict()
        wrong = {**exact, 'tenant_id': 'intruder', 'user_id': 'intruder'}
        with pytest.raises(ValueError):
            load_explicit_capture(runtime, capture.record_id, scope=wrong)
        with pytest.raises(ValueError):
            accept_explicit_query(runtime, capture_record_id=capture.record_id, operator_scope=wrong,
                                  labels=[], packet_evidence={})
        runtime._test_runtime_commit = 'c' * 40
        assert current_release_identity(runtime, exact) is None
        assert load_explicit_capture(runtime, capture.record_id, scope=exact).to_dict() == old
        assert runtime.store.get_by_id(receipt.record_id).to_dict() == before
        receipt.summary = 'tampered receipt'
        runtime.store.append(receipt)
        with pytest.raises(ValueError, match='release'):
            load_explicit_capture(runtime, capture.record_id, scope=exact)
    finally:
        runtime.close()


@pytest.mark.parametrize('invalid', ['unknown', 'source', 'gate', 'health', 'service', 'release', 'strict', 'expired', 'wrong_commit'])
def test_shared_release_fails_closed(tmp_path, monkeypatch, invalid):
    monkeypatch.delenv('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE', raising=False)
    runtime = Runtime.create(root=tmp_path)
    try:
        runtime._test_runtime_commit = RELEASE.commit
        receipt = receipt_for_service()
        effect = receipt.content['side_effect']
        if invalid == 'source': receipt.source = 'untrusted'
        elif invalid == 'gate': receipt.content['gate']['receipt_verified'] = False
        elif invalid == 'health': effect['post_deploy_health']['ok'] = False
        elif invalid == 'service': effect['post_deploy_health']['url'] = 'http://other/health'
        elif invalid == 'release': effect['release']['release_path'] = '/other/' + RELEASE.commit
        elif invalid == 'strict': effect['code_evolution'] = {'strict': True}
        elif invalid == 'expired': receipt.status = 'expired'
        elif invalid == 'wrong_commit': runtime._test_runtime_commit = 'c' * 40
        if invalid != 'unknown': runtime.store.append(receipt)
        assert current_release_identity(runtime, {'tenant_id': 'other', 'agent_id': 'hongtai'}) is None
    finally:
        runtime.close()
