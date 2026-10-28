import json
import pytest
from deploy.refresh_release_scope_bindings import (
    already_current_deploy_log,
    deployment_receipt_id,
    resolve_receipt_id,
)


@pytest.mark.parametrize('fault',['none','denied','release'])
def test_binding_refresh_uses_the_authenticated_process_observation(monkeypatch,fault):
    from deploy.refresh_release_scope_bindings import rpc_process_environment
    from eimemory.ops import runtime_service_observation, controlled_stability
    from pathlib import Path

    commit='a'*40
    environment={'EIMEMORY_RUNTIME_COMMIT':'b'*40 if fault=='release' else commit,
                 'EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE':'/private/bindings.json'}
    monkeypatch.setattr(runtime_service_observation,'service_state',lambda unit,props:{'MainPID':'123'})
    def observed(state):
        assert state=={'Id':'eimemory-rpc.service','MainPID':'123'}
        if fault=='denied':raise RuntimeError('actual_host_observation_denied')
        return 123,Path('/proc/123'),environment
    monkeypatch.setattr(controlled_stability,'_process_environment',observed)
    if fault=='none':assert rpc_process_environment(commit)==environment
    else:
        with pytest.raises((RuntimeError,ValueError)):rpc_process_environment(commit)


def test_pretty_deployment_receipt_does_not_require_successful_l5_closure():
    row = {'report_type':'deployment_receipt','ok':True,'commit':'abc','promotion_request_id':'rec_real'}
    text = 'installer output\n' + json.dumps(row, indent=2) + '\npost_deploy_validation=degraded\n'
    assert deployment_receipt_id(text, 'abc') == 'rec_real'

@pytest.mark.parametrize('rows', [[], [{'report_type':'deployment_receipt','ok':False,'commit':'abc','promotion_request_id':'bad'}], [{'report_type':'deployment_receipt','ok':True,'commit':'other','promotion_request_id':'bad'}]])
def test_missing_failed_or_wrong_commit_receipt_stays_closed(rows):
    with pytest.raises(ValueError, match='unique_deployment_receipt_required'):
        deployment_receipt_id('\n'.join(json.dumps(x) for x in rows), 'abc')


def test_conflicting_receipts_stay_closed():
    rows = [{'report_type':'deployment_receipt','ok':True,'commit':'abc','promotion_request_id':value} for value in ['one','two']]
    with pytest.raises(ValueError, match='unique_deployment_receipt_required'):
        deployment_receipt_id('\n'.join(json.dumps(x) for x in rows), 'abc')


def test_already_current_marker_is_exact_line():
    assert already_current_deploy_log('release=/x\nalready_current=1\n')
    assert not already_current_deploy_log('note already_current=1 extra\n')
    assert not already_current_deploy_log('already_current=0\n')


class _Identity:
    def __init__(self, commit, receipt_id):
        self.commit = commit
        self.receipt_id = receipt_id


def test_already_current_reuses_live_release_identity(monkeypatch):
    import deploy.refresh_release_scope_bindings as mod
    commit = 'a' * 40
    monkeypatch.setattr(
        'eimemory.governance.release.evidence_contract.current_release_identity',
        lambda runtime, scope: _Identity(commit, 'rec_live'),
    )
    receipt_id, source = resolve_receipt_id('already_current=1\n', commit, object())
    assert receipt_id == 'rec_live'
    assert source == 'already_current_runtime'


def test_already_current_without_live_identity_stays_closed(monkeypatch):
    monkeypatch.setattr(
        'eimemory.governance.release.evidence_contract.current_release_identity',
        lambda runtime, scope: None,
    )
    with pytest.raises(ValueError, match='unique_deployment_receipt_required'):
        resolve_receipt_id('already_current=1\n', 'a' * 40, object())


def test_non_already_current_missing_receipt_stays_closed():
    with pytest.raises(ValueError, match='unique_deployment_receipt_required'):
        resolve_receipt_id('commit=abc\n', 'abc', object())


def test_deploy_log_receipt_wins_over_already_current(monkeypatch):
    monkeypatch.setattr(
        'eimemory.governance.release.evidence_contract.current_release_identity',
        lambda runtime, scope: (_ for _ in ()).throw(AssertionError('should not fall back')),
    )
    row = {'report_type': 'deployment_receipt', 'ok': True, 'commit': 'abc', 'promotion_request_id': 'rec_log'}
    text = json.dumps(row) + '\nalready_current=1\n'
    receipt_id, source = resolve_receipt_id(text, 'abc', object())
    assert receipt_id == 'rec_log'
    assert source == 'deploy_log'
