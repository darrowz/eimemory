import json
import pytest
from deploy.refresh_release_scope_bindings import deployment_receipt_id


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
