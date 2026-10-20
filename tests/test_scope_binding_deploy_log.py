import json
import pytest
from deploy.refresh_release_scope_bindings import deployment_receipt_id


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
