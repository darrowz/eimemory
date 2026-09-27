"""Release/governance regressions. Run against a complete checkout after patching."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from eimemory.governance.l5.closure_rehearsal import (
    _acceptance_gate, _capability_replay_gate, _replay_summary_consistent,
)
from eimemory.governance.release.release_closure import _live_acceptance_ok
from eimemory.governance.release.closure_contracts import (
    LIVE_ACCEPTANCE_CASE_IDS, LEGACY_RELEASE_CASE_IDS, acceptance_failure_details,
    acceptance_report_ok, channel_wait_report_ok,
)
from eimemory.governance.promotion.promotion_git_ops import _resolve_patch_command
from eimemory.governance.release import release_closure_pending as pending
from deploy import summarize_release_closure as summary
from deploy import run_with_governance_env as env_wrapper
from deploy.inspect_release_pollution import inspect, main as inspect_main


def acceptance(count=12, *, persist=True):
    ids = LEGACY_RELEASE_CASE_IDS if count == 12 else tuple(f'case-{i}' for i in range(count))
    rows = [{
        'case_id': case, 'capability': ('search.discovery', 'research.synthesis', 'operations.uumit', 'device.control')[i // 3] if count == 12 else 'custom',
        'probe_id': f'probe-{i}', 'probe_record_id': f'probe-{i}' if persist else '',
        'trace_id': f'trace-{i}', 'trace_record_id': f'trace-record-{i}' if persist else '',
        'passed': True, 'validator_passed': True, 'trace_emitted': persist,
        'persisted': persist, 'error': '',
    } for i, case in enumerate(ids)]
    return {'ok': True, 'all_passed': True, 'case_count': count, 'pass_count': count,
            'failed_count': 0, 'probe_count': count, 'trace_count': count if persist else 0,
            'execution_id': 'test-acceptance', 'persisted': persist,
            'distinct_probe_sources': True, 'distinct_trace_ids': True, 'results': rows}


def packs_for(report):
    packs = []
    for capability in sorted({r['capability'] for r in report['results']}):
        rows = [r for r in report['results'] if r['capability'] == capability]
        packs.append({'capability': capability, 'pass_rate': 1.0,
                      'cases': [{'case_id': r['case_id'], 'threshold': 0.8} for r in rows],
                      'case_results': [{'case_id': r['case_id'], 'verdict': 'pass',
                                        'evidence_source_id': r['probe_id'], 'probe_source_id': r['probe_record_id'],
                                        'trace_record_id': r['trace_record_id'], 'trace_id': r['trace_id']} for r in rows]})
    return {'ok': True, 'packs': packs, 'manifest_record_id':'manifest-1',
            'persisted_replay_count':len(report['results']),
            'persisted_replay_ids':[f'replay-{i}' for i in range(len(report['results']))]}


def receipt():
    return {'ok': True, 'commit': 'a' * 40, 'version': '1.14.3', 'release_path': '/opt/eimemory/releases/' + 'a' * 40,
            'promotion_request_id': 'receipt-1', 'release_session_id': 'session-1'}


def live_report():
    identity = receipt()
    cases = []
    for i, case in enumerate(LIVE_ACCEPTANCE_CASE_IDS):
        digest = f'{i + 1:064x}'
        cases.append({'case_id': case, 'task_type': f'live.acceptance.{case}',
                      'passed': True, 'trace_persisted': True, 'record_id': f'live-{i}',
                      'observation_digest': digest,
                      'trace_id': f"live-acceptance:{identity['commit']}:{case}:{digest[:12]}"})
    return {'ok': True, 'case_count': 10, 'pass_count': 10, 'fail_count': 0,
            'distinct_task_types': 10, 'deployment': identity, 'cases': cases}


def wait_report():
    accept = acceptance()
    identity = receipt()
    return {'report_type': 'l5_release_closure', 'ok': False, 'closure_complete': False,
            'data_accumulating': False, 'blocked_stage': 'channel_acceptance',
            'blocked_reason': 'current_release_channel_receipt_not_found',
            'deployment': identity, 'deployment_receipt': identity,
            'storage_migrations': {'ok': True},
            'production_recall_gate': {'ok': True, 'status': 'accepted'},
            'production_recall_strict_state': {'ok': True, 'status': 'strict_activated', 'candidate_commit': identity['commit']},
            'replay_bootstrap': {'ok': True, 'legacy_compatibility': True,
                                 'capability_acceptance': accept, 'capability_replay': packs_for(accept)},
            'live_acceptance': live_report(),
            'pending_checkpoint': {'ok': True, 'status': 'waiting_for_channel_acceptance'},
            'channel_acceptance': {'ok': False, 'error': 'current_release_channel_receipt_not_found'}}


def checkpoint(path):
    return {'schema_version': 'release_closure_pending.v1', 'status': 'waiting_for_channel_acceptance',
            'current_commit': 'a' * 40, 'prior_commit': 'b' * 40, 'deployment_receipt_id': 'receipt-1',
            'release_session_id': 'session-1', 'release_path': str(path.parent / 'release'),
            'scope': {'tenant_id': 'test', 'agent_id': 'test', 'workspace_id': 'test', 'user_id': 'test'},
            'inputs': {'repo_root': str(path.parent / 'repo'), 'current_link': str(path.parent / 'current'), 'health_url': 'http://127.0.0.1:8091/health'},
            'passed_gate_record_ids': {},
            'passed_gate_reports': {'replay_bootstrap': {}, 'live_acceptance': {}, 'bootstrap_pending': {}},
            'created_at': '2026-09-27T15:00:00+00:00'}


@pytest.mark.parametrize('persist', [False, True])
def test_valid_acceptance_is_still_accepted(persist):
    assert _acceptance_gate(acceptance(persist=persist), expected_count=12)


@pytest.mark.parametrize('value', [True, False, '12', 12.0, 12.8, float('nan'), float('inf'), -1, None, []])
def test_acceptance_requires_integer_counts(value):
    r = acceptance(); r['case_count'] = value
    assert _acceptance_gate(r, expected_count=12) is False


@pytest.mark.parametrize('key', ['case_id', 'probe_id', 'trace_id', 'probe_record_id', 'trace_record_id'])
def test_acceptance_rejects_duplicated_identifiers(key):
    r = acceptance(); r['results'][1][key] = r['results'][0][key]
    assert not _acceptance_gate(r, expected_count=12)


@pytest.mark.parametrize('mutation', ['empty', 'missing', 'failed', 'unpersisted', 'trace', 'case', 'malformed', 'error'])
def test_acceptance_checks_actual_rows(mutation):
    r = acceptance()
    if mutation == 'empty': r['results'] = []
    elif mutation == 'missing': r.pop('results')
    elif mutation == 'failed': r['results'][0]['passed'] = False
    elif mutation == 'unpersisted': r['results'][0]['persisted'] = False
    elif mutation == 'trace': r['results'][0]['trace_emitted'] = False
    elif mutation == 'case': r['results'][0]['case_id'] = 'not-a-selected-case'
    elif mutation == 'malformed': r['results'][0] = 'invalid'
    elif mutation == 'error': r['results'][0]['error'] = 'not_successful'
    assert not _acceptance_gate(r, expected_count=12)


def test_dynamic_selection_is_not_forced_to_legacy_taxonomy():
    assert _acceptance_gate(acceptance(2), expected_count=None)


def test_valid_replay_passes():
    assert _capability_replay_gate(packs_for(acceptance()), expected_capabilities=None, reason_prefix='test')['ok']


@pytest.mark.parametrize('mutation', ['all_failed', 'bad_case', 'duplicate_case', 'missing_case', 'extra_case', 'mixed_shape', 'duplicate_pack', 'reused_evidence'])
def test_replay_recomputes_results(mutation):
    r = packs_for(acceptance()); p = r['packs'][0]
    if mutation == 'all_failed':
        for row in p['case_results']: row['verdict'] = 'fail'
    elif mutation == 'bad_case': p['case_results'][0]['case_id'] = 'wrong'
    elif mutation == 'duplicate_case': p['case_results'][1]['case_id'] = p['case_results'][0]['case_id']
    elif mutation == 'missing_case': p['cases'][0].pop('case_id')
    elif mutation == 'extra_case': p['case_results'].append(deepcopy(p['case_results'][0]))
    elif mutation == 'mixed_shape': r['packs'].append(None)
    elif mutation == 'duplicate_pack': r['packs'].append(deepcopy(p))
    elif mutation == 'reused_evidence': r['packs'][1]['case_results'][0]['evidence_source_id'] = p['case_results'][0]['evidence_source_id']
    assert not _capability_replay_gate(r, expected_capabilities=None, reason_prefix='test')['ok']


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), '1', True, {}, -1, 2])
@pytest.mark.parametrize('field', ['threshold', 'pass_rate'])
def test_replay_rejects_invalid_numbers(value, field):
    r = packs_for(acceptance())
    if field == 'threshold': r['packs'][0]['cases'][0][field] = value
    else: r['packs'][0][field] = value
    assert not _capability_replay_gate(r, expected_capabilities=None, reason_prefix='test')['ok']


@pytest.mark.parametrize('values', [(1,2,-1,2.0), (True,True,0,1.0), (1.8,1.8,0,1.0), ('1','1','0',1.0), (float('inf'),1,0,1.0), (2500,1999,501,0.8)])
def test_replay_summary_rejects_invalid_counts_and_rounded_promotion(values):
    r = dict(zip(('executed_count','pass_count','fail_count','pass_rate'), values))
    assert not _replay_summary_consistent(r)


def test_valid_replay_summary():
    assert _replay_summary_consistent({'executed_count':5,'pass_count':4,'fail_count':1,'pass_rate':0.8})


def test_live_requires_real_case_set():
    r = live_report(); r.pop('cases')
    assert not _live_acceptance_ok(r, receipt=receipt())


@pytest.mark.parametrize('mutation', ['failed', 'duplicate', 'wrong_type', 'bad_digest', 'wrong_trace', 'unpersisted', 'cross_session', 'float_count', 'empty_identity'])
def test_live_validates_identity_and_rows(mutation):
    r = live_report(); rec = receipt()
    if mutation == 'failed': r['cases'][0]['passed'] = False
    elif mutation == 'duplicate': r['cases'][1] = deepcopy(r['cases'][0])
    elif mutation == 'wrong_type': r['cases'][0]['task_type'] = 'other'
    elif mutation == 'bad_digest': r['cases'][0]['observation_digest'] = 'invalid'
    elif mutation == 'wrong_trace': r['cases'][0]['trace_id'] = 'other-release-trace'
    elif mutation == 'unpersisted': r['cases'][0]['trace_persisted'] = False
    elif mutation == 'cross_session': r['deployment']['release_session_id'] = 'session-other'
    elif mutation == 'float_count': r['case_count'] = 10.9
    elif mutation == 'empty_identity': r['deployment'] = {}; rec = {}
    assert not _live_acceptance_ok(r, receipt=rec)


def test_complete_live_fixture_remains_valid():
    assert _live_acceptance_ok(live_report(), receipt=receipt())


@pytest.mark.parametrize('component', ['deployment_receipt', 'storage_migrations', 'replay_bootstrap', 'production_recall_gate', 'pending_checkpoint'])
def test_summary_pending_does_not_hide_upstream_failure(component, tmp_path):
    r = wait_report(); r[component]['ok'] = False
    if component == 'production_recall_gate': r[component]['status'] = 'failed'
    path = tmp_path/'report.json'; path.write_text(json.dumps(r))
    assert summary.main(['--path',str(path)]) == 1
    assert summary.summarize_release_closure(r)['business_closure_outcome'] == 'failed'


def test_valid_channel_wait_is_not_closure(tmp_path):
    r = wait_report(); path = tmp_path/'report.json'; path.write_text(json.dumps(r))
    assert summary.main(['--path',str(path)]) == 0
    result = summary.summarize_release_closure(r)
    assert result['business_closure_outcome'] == 'data_accumulating'
    assert result['ok'] is False and result['closure_complete'] is False


@pytest.mark.parametrize('payload', ['{"ok":false,"ok":true}', '{"score":NaN}', '{"score":Infinity}', '{"score":1e999}', '['*65+'0'+']'*65])
def test_summary_rejects_ambiguous_json(payload, tmp_path):
    path = tmp_path/'r.json'; path.write_text(payload)
    with pytest.raises(ValueError): summary._read_report(path)


def test_summary_valid_json(tmp_path):
    path = tmp_path/'r.json'; path.write_text('{"ok":false}')
    assert summary._read_report(path) == {'ok':False}


@pytest.mark.parametrize('flag', ['-I', '-E', '-IE'])
def test_controlled_interpreter_never_writes_bytecode(flag, tmp_path):
    module = tmp_path/'audit_probe.py'; module.write_text('ANSWER=42\n')
    command = _resolve_patch_command([sys.executable, flag, '-c',
        'import sys; sys.path.insert(0, sys.argv[1]); import audit_probe; print(audit_probe.ANSWER)', str(tmp_path)])
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONPYCACHEPREFIX=str(tmp_path/'external-cache'))
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == '42'
    assert not list(tmp_path.rglob('*.pyc'))


def test_console_and_script_arguments_are_not_rewritten():
    argv=['/opt/eimemory/bin/eimemory','learn','--path','python']
    assert _resolve_patch_command(argv) == argv


def test_wrapper_pins_exec_python_flag(tmp_path, monkeypatch):
    path=tmp_path/'governance.env'; path.write_text(''); path.chmod(0o600)
    captured={}
    class Executed(BaseException): pass
    def execute(executable, argv, environment):
        captured.update(argv=argv, environment=environment); raise Executed
    monkeypatch.setattr(env_wrapper.os,'execvpe',execute)
    with pytest.raises(Executed):
        env_wrapper.main(['--env-file',str(path),'--',sys.executable,'-I','-c','pass'])
    assert captured['argv'][1]=='-B'
    assert captured['environment']['PYTHONDONTWRITEBYTECODE']=='1'


@pytest.mark.skipif(os.name != "posix", reason="checkpoint mode is a POSIX permission contract")
def test_checkpoint_private_file_mode(tmp_path):
    path=tmp_path/'pending.json'; old=os.umask(0o022)
    try: assert pending.write_release_closure_pending(checkpoint(path),path=path)['ok']
    finally: os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o600


def test_writer_cannot_replace_while_reconciler_holds_lock(tmp_path):
    path=tmp_path/'pending.json'; first=checkpoint(path)
    assert pending.write_release_closure_pending(first,path=path)['ok']
    changed={**first,'release_session_id':'new-session'}
    with pending._release_closure_reconcile_lock(path):
        with ThreadPoolExecutor(1) as pool:
            result=pool.submit(pending.write_release_closure_pending,changed,path=path).result(timeout=5)
        assert result['ok'] is False and result['status']=='busy'
        assert json.loads(path.read_text())['release_session_id']=='session-1'


def test_clear_cannot_delete_during_reconcile(tmp_path):
    path=tmp_path/'pending.json'; pending.write_release_closure_pending(checkpoint(path),path=path)
    with pending._release_closure_reconcile_lock(path):
        assert pending.clear_release_closure_pending(path=path,expected_commit='a'*40) is False
        assert path.exists()
    assert pending.clear_release_closure_pending(path=path,expected_commit='a'*40)


def test_checkpoint_rejects_hardlinked_lock(tmp_path):
    path=tmp_path/'pending.json'; outside=tmp_path/'outside'; outside.write_bytes(b'')
    os.link(outside,tmp_path/'.pending.json.lock')
    with pytest.raises(ValueError):
        with pending._release_closure_reconcile_lock(path): pass
    assert outside.read_bytes()==b''


def test_checkpoint_body_timeout_is_not_misreported_as_lock_busy(tmp_path):
    with pytest.raises(TimeoutError,match='body failure'):
        with pending._release_closure_reconcile_lock(tmp_path/'pending.json'):
            raise TimeoutError('body failure')


def test_checkpoint_rejects_nonfinite_json(tmp_path):
    path=tmp_path/'pending.json'; data=checkpoint(path); data['passed_gate_reports']['replay_bootstrap']['score']=float('nan')
    with pytest.raises(ValueError): pending.write_release_closure_pending(data,path=path)
    assert not path.exists()


def test_failure_diagnostics_distinguish_phases_without_raw_error():
    rows=acceptance()['results'][:3]
    for row in rows: row['passed']=False; row['error']='Bearer secret-value'
    rows[0]['validator_passed']=False
    rows[1]['trace_record_id']=''
    rows[2]['capability_revision_id']='revision-1'; rows[2]['evaluation_run_id']=''
    details=acceptance_failure_details({'results':rows})
    assert [r['phase'] for r in details['failures']]==['execution_or_contract','trace_persistence','evaluation_persistence']
    assert 'secret-value' not in json.dumps(details)


def test_forensic_inventory_preserves_bytecode_and_reports_no_attribution(tmp_path):
    root=tmp_path/'release'; root.mkdir(); cache=root/'__pycache__'; cache.mkdir()
    file=cache/'probe.pyc'; file.write_bytes(b'fake-bytecode')
    before=file.stat()
    report=inspect(root)
    assert report['bytecode_count']==1 and report['inventory_complete']
    assert report['writer_attribution']=='not_established'
    assert file.read_bytes()==b'fake-bytecode'
    after=file.stat()
    assert (after.st_ino,after.st_mtime_ns,after.st_ctime_ns)==(before.st_ino,before.st_mtime_ns,before.st_ctime_ns)


def test_forensic_collector_refuses_output_inside_release(tmp_path):
    root=tmp_path/'release'; root.mkdir()
    with pytest.raises(SystemExit): inspect_main(['--release-dir',str(root),'--output',str(root/'report.json')])
    assert list(root.iterdir())==[]


@pytest.mark.parametrize('which', ['report', 'governance_environment'])
@pytest.mark.skipif(os.name!='posix',reason='POSIX FIFO contract')
def test_fifo_rejected_without_waiting_for_writer(which, tmp_path):
    pipe=tmp_path/'input.fifo'; os.mkfifo(pipe,0o600)
    module=summary if which=='report' else env_wrapper
    function='_read_report' if which=='report' else 'load_governance_environment'
    code='''import importlib.util, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).resolve().parents[1]))
spec=importlib.util.spec_from_file_location("read_probe",sys.argv[1])
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
try:
    getattr(mod,sys.argv[2])(Path(sys.argv[3]))
except (OSError,ValueError,getattr(mod,"GovernanceEnvironmentError",ValueError)):
    raise SystemExit(0)
raise SystemExit(3)
'''
    process=subprocess.run([sys.executable,'-B','-c',code,str(module.__file__),function,str(pipe)],
                           capture_output=True,text=True,timeout=3)
    assert process.returncode==0,process.stderr
    assert pipe.exists()


def test_honest_incomplete_report_remains_failed(tmp_path):
    r=wait_report()
    r['blocked_stage']='replay_bootstrap';r['blocked_reason']='capability_acceptance_failed'
    r['replay_bootstrap']['ok']=False
    r['live_acceptance']={'ok':False,'status':'not_run','case_count':0,'pass_count':0}
    path=tmp_path/'r.json';path.write_text(json.dumps(r))
    assert summary.main(['--path',str(path)])==1
    result=summary.summarize_release_closure(r)
    assert result['closure_complete'] is False and result['business_closure_outcome']=='failed'


@pytest.mark.parametrize('malformed', ['missing_rows','wrong_probe','wrong_trace','missing_manifest'])
def test_summary_checks_replay_anchors_even_for_completed_report(malformed, tmp_path):
    r=wait_report()
    r.update(ok=True,closure_complete=True,blocked_stage='',blocked_reason='')
    r['channel_acceptance']={'ok':True,'evidence_class':'external_channel_receipt','record_id':'channel-1'}
    r['closure_rehearsal']={'ok':True,'closure_complete':True,'data_accumulating':False}
    r['readiness']={'ok':True,'schema_version':'l5_readiness.v2','current_stage':'L5','readiness_score':1.0,
                    'release_identity':{'release_commit':'a'*40,'deployment_receipt_id':'receipt-1','release_session_id':'session-1'}}
    replay=r['replay_bootstrap']['capability_replay']
    if malformed=='missing_rows':r['replay_bootstrap']['capability_acceptance']['results']=[]
    elif malformed=='wrong_probe':replay['packs'][0]['case_results'][0]['probe_source_id']='wrong'
    elif malformed=='wrong_trace':replay['packs'][0]['case_results'][0]['trace_id']='wrong'
    else:replay.pop('manifest_record_id')
    path=tmp_path/'r.json';path.write_text(json.dumps(r))
    assert summary.main(['--path',str(path)])==1


def accumulation_fixture():
    return {
        'live_task_gate':{'ok':False,'evidence_mode':'current_release','evidence_release_commit':'a'*40,
                         'current_release_commit':'a'*40,'current_deployment_verified_real_tasks':0,
                         'sample_count':0,'distinct_task_types':0,'success_rate':1.0,
                         'current_deployment_operational_probes':10},
        'hard_metrics':{'verified_real_task_success_rate':1.0,'current_deployment_live_task_success_rate':1.0},
        'hard_metric_samples':{'verified_real_tasks':10,'verified_real_task_types':4,
                               'current_deployment_operational_probes':10,'current_deployment_live_task_types':5},
        'hard_metric_quality':{'verified_real_task_success_rate':{'sufficient':True,'sample_count':10},
                               'current_deployment_live_task_success_rate':{'sufficient':True,'sample_count':10}},
        'release_lineage':{'ok':True,'validated':True,'compatible':True,
                          'current_release':{'commit':'a'*40,'version':'1.14.3','receipt_id':'receipt-1','session_id':'session-1'},
                          'domains':{'channel.delivery':{'mode':'inherited','changed':False,'gate_errors':{}}}},
    }


@pytest.mark.parametrize('value',[-1,0.5,float('inf'),float('nan'),'0'])
def test_accumulation_rejects_invalid_task_counts(value):
    from eimemory.governance.l5.closure_rehearsal import _compatible_live_task_accumulation
    from eimemory.governance.release.evidence_contract import ReleaseIdentity
    r=accumulation_fixture();r['live_task_gate']['current_deployment_verified_real_tasks']=value;r['live_task_gate']['sample_count']=value
    release=ReleaseIdentity(commit='a'*40,version='1.14.3',receipt_id='receipt-1',session_id='session-1')
    assert not _compatible_live_task_accumulation(r,release=release)


def test_accumulation_preserves_valid_zero_current_real_tasks():
    from eimemory.governance.l5.closure_rehearsal import _compatible_live_task_accumulation
    from eimemory.governance.release.evidence_contract import ReleaseIdentity
    release=ReleaseIdentity(commit='a'*40,version='1.14.3',receipt_id='receipt-1',session_id='session-1')
    assert _compatible_live_task_accumulation(accumulation_fixture(),release=release)
