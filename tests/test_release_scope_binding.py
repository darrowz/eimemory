from dataclasses import asdict
from hashlib import sha256
import json

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.governance.evidence_contract import current_release_identity
from eimemory.models.records import ScopeRef
from eimemory.evaluation.production_query_dataset import collect_pending_production_queries
from eimemory.evaluation.real_query_gate import _persist_bootstrap_state, verify_current_bootstrap_data_pending
from eimemory.governance.evidence_contract import ReleaseIdentity
from test_governance_evidence_contract import SCOPE, RELEASE, _deployment_receipt


TARGET = resolve_channel_scope('hermes', {
    'tenant_id': SCOPE.tenant_id, 'agent_id': 'default',
    'workspace_id': 'hermes', 'user_id': 'logical-user',
})


def configure(tmp_path, monkeypatch, receipt, target=TARGET):
    payload = [{
        'scope': target,
        'receipt_id': receipt.record_id,
        'receipt_sha256': sha256(json.dumps(receipt.to_dict(), sort_keys=True,
                                          ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
    }]
    path = tmp_path / 'bindings.json'
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    monkeypatch.setenv('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE', str(path))
    return path


def receipt_for_service():
    receipt = _deployment_receipt()
    effect = receipt.content['side_effect']
    effect['verification']['prior_commit'] = 'b' * 40
    effect['deployment']['current_link'] = '/opt/eimemory/current'
    effect['post_deploy_health']['current_link'] = '/opt/eimemory/current'
    effect['post_deploy_health']['url'] = 'http://127.0.0.1:8091/health'
    return receipt


def test_bound_service_identity_keeps_request_scope(tmp_path, monkeypatch):
    runtime = Runtime.create(root=tmp_path / 'store')
    runtime._test_runtime_commit = RELEASE.commit
    try:
        receipt = runtime.store.append(receipt_for_service())
        assert current_release_identity(runtime, SCOPE).receipt_id == receipt.record_id
        assert current_release_identity(runtime, TARGET) is None
        configure(tmp_path, monkeypatch, receipt)
        identity = current_release_identity(runtime, TARGET)
        assert identity is not None
        assert identity.receipt_id == receipt.record_id
        result = AgentRuntimeMemoryService(runtime).proactive_prefetch(
            channel='hermes', scope=TARGET, source_ids=['alpha'], session_id='session',
            turn_id='turn', query='Remember the project constraints', acceptance_generated=True,
        )
        assert result['decision_id']
        stored = runtime.store.load_proactive_decision(result['decision_id'])
        assert result['acceptance_generated'] is True
        assert result['release_identity']['deployment_receipt_id'] == receipt.record_id
        assert stored['scope'] == TARGET
        assert stored['acceptance_generated'] is True
        assert stored['release_identity']['deployment_receipt_id'] == receipt.record_id
        assert runtime.store.get_by_id(receipt.record_id).scope == SCOPE
        assert runtime.store.get_by_id(receipt.record_id, scope=ScopeRef.from_dict(TARGET)) is None
        capture = collect_pending_production_queries(runtime, scope=TARGET, channel='hermes')
        assert capture['created'] == 0
        _persist_bootstrap_state(
            runtime, scope=ScopeRef.from_dict(TARGET), state='bootstrap_data_pending',
            candidate_commit=RELEASE.commit,
            prior_release=ReleaseIdentity('b' * 40, 'old', 'prior-receipt', 'prior-session'),
            reason='isolated regression', progress={},
        )
        assert verify_current_bootstrap_data_pending(runtime, scope=TARGET, release=identity)['ok']
        assert current_release_identity(runtime, {**TARGET, 'user_id': 'unauthorized'}) is None
        runtime._test_runtime_commit = 'c' * 40
        denied = AgentRuntimeMemoryService(runtime).proactive_terminal(
            channel='hermes', scope={**TARGET, 'user_id': 'unauthorized'}, source_ids=['alpha'],
            session_id='session', turn_id='turn', decision_id=result['decision_id'], used_citations=[],
        )
        assert denied['ok'] is False
        terminal = AgentRuntimeMemoryService(runtime).proactive_terminal(
            channel='hermes', scope=TARGET, source_ids=['alpha'], session_id='session',
            turn_id='turn', decision_id=result['decision_id'], used_citations=[],
        )
        assert terminal['ok'] is True
        runtime._test_runtime_commit = RELEASE.commit
        monkeypatch.delenv('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
        assert not verify_current_bootstrap_data_pending(runtime, scope=TARGET, release=identity)['ok']
        assert current_release_identity(runtime, TARGET) is None
        assert current_release_identity(runtime, {**TARGET, 'user_id': 'unauthorized'}) is None
    finally:
        runtime.close()


@pytest.mark.parametrize('invalid', [
    'commit', 'untrusted', 'service', 'tenant', 'digest', 'public_file', 'missing_scope',
    'strict_ledger', 'source', 'release_path', 'bare_env',
])
def test_binding_fails_closed(tmp_path, monkeypatch, invalid):
    runtime = Runtime.create(root=tmp_path / 'store')
    runtime._test_runtime_commit = RELEASE.commit
    try:
        receipt = receipt_for_service()
        if invalid == 'untrusted':
            receipt.content['gate']['receipt_verified'] = False
        if invalid == 'service':
            receipt.content['side_effect']['post_deploy_health']['url'] = 'http://other-service/health'
        if invalid == 'source':
            receipt.source = 'untrusted.client'
        if invalid == 'strict_ledger':
            receipt.content['side_effect']['code_evolution'] = {'strict': True}
        if invalid == 'release_path':
            for key in ('release', 'deployment', 'post_deploy_health'):
                receipt.content['side_effect'][key]['release_path'] = '/other/releases/' + RELEASE.commit
        if invalid == 'tenant':
            receipt.scope = ScopeRef(**{**asdict(SCOPE), 'tenant_id': 'other-tenant'})
        receipt = runtime.store.append(receipt)
        target = {k: v for k, v in TARGET.items() if k != 'user_id'} if invalid == 'missing_scope' else TARGET
        path = configure(tmp_path, monkeypatch, receipt, target)
        if invalid == 'bare_env':
            runtime._test_runtime_commit = ''
            monkeypatch.setenv('EIMEMORY_RUNTIME_COMMIT', RELEASE.commit)
        if invalid == 'commit':
            runtime._test_runtime_commit = 'c' * 40
        if invalid == 'digest':
            path.write_text(path.read_text().replace('receipt_sha256', 'wrong_digest'))
        if invalid == 'public_file':
            path.chmod(0o644)
        assert current_release_identity(runtime, TARGET) is None
    finally:
        runtime.close()
