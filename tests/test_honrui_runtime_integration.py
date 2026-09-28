"""Full-repository integration companions; not executed in the partial audit fixture.

The live test uses real Runtime storage/outcome code. The receipt verifier is
substituted deliberately: this tests persistence identity, not live deployment
attestation. Run on an independent test root, never a production database.
"""
from dataclasses import asdict
from types import SimpleNamespace


def test_full_cli_parser_and_acceptance_dispatch_use_explicit_scope(capsys):
    from eimemory.cli import main as cli
    calls=[]
    def acceptance(**kw):
        calls.append(kw)
        return {'ok':False,'reason':'test_selection_blocked'}
    runtime=SimpleNamespace(run_capability_acceptance=acceptance)
    parsed=cli._build_parser().parse_args(['learn','capability-acceptance','--profile','l5.default:v1',
        '--scope-tenant','tenant-test','--scope-agent','agent-test',
        '--scope-workspace','work::channel::hermes','--scope-user','user-test'])
    result=cli._cmd_learn(parsed,runtime,{'tenant_id':'default','agent_id':'wrong',
                                       'workspace_id':'wrong','user_id':'wrong'})
    assert result==1
    assert calls[0]['scope']==calls[0]['runtime_scope']=={
        'tenant_id':'tenant-test','agent_id':'agent-test',
        'workspace_id':'work::channel::hermes','user_id':'user-test'}
    assert calls[0]['profile_key']=='l5.default'
    capsys.readouterr()


def test_full_runtime_live_same_commit_two_receipts_do_not_share_trace(tmp_path,monkeypatch):
    from eimemory.api.runtime import Runtime
    from eimemory.models.records import RecordEnvelope,ScopeRef
    from eimemory.governance.release import evidence_contract
    from eimemory.governance.l5 import live_task_acceptance as live
    scope=ScopeRef(tenant_id='default',agent_id='audit',workspace_id='test',user_id='operator')
    runtime=Runtime.create(root=tmp_path)
    authority={}
    monkeypatch.setattr(evidence_contract,'current_release_identity',lambda *a,**kw:authority.get('release'))
    # Do not pretend this receipt substitution verifies real host health.
    monkeypatch.setattr(live,'_valid_deployment_receipt',lambda *a,**kw:True)
    outputs=[]
    try:
        for session in ('first-session','second-session'):
            receipt=runtime.store.append(RecordEnvelope.create(kind='promotion_request',
                title='isolated test receipt',scope=scope,source='eimemory.deployment_receipt',status='deployed'))
            authority['release']=evidence_contract.ReleaseIdentity(commit='a'*40,version='1.14.4',
                receipt_id=receipt.record_id,session_id=session)
            identity=dict(commit='a'*40,version='1.14.4',promotion_request_id=receipt.record_id,
                release_session_id=session,release_path='/test/releases/'+'a'*40)
            result=live._execute_and_record_case(runtime,scope=scope,identity=identity,
                definition={'case_id':'store.sqlite_query','task_type':'live.acceptance.store.sqlite_query',
                            'check':lambda:{'passed':True,'query_ok':True}})
            outputs.append(result)
            record=runtime.store.get_by_id(result['record_id'],scope=scope)
            assert record.content['release_session_id']==session
            assert record.content['promotion_request_id']==receipt.record_id
        assert all(item['passed'] is True for item in outputs)
        assert len({item['record_id'] for item in outputs})==2
        assert len({item['trace_id'] for item in outputs})==2
    finally:
        runtime.close()
