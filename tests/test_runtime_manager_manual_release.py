from pathlib import Path
from types import SimpleNamespace
from hashlib import sha256
import sys
import pytest

from eimemory.contracts import runtime_manager as c
from test_runtime_manager_handoff import module


@pytest.mark.parametrize('fault',['none','registry','current','automatic','unclean','no_resume','failed_unit','candidate','candidate_unresumed','quiesced_activation'])
def test_failed_manual_recovery_restores_capture_without_forging_success(tmp_path,monkeypatch,fault):
    m=module();key='a'*64;commit='b'*40
    state=tmp_path/'state';(state/'jobs').mkdir(parents=True)
    source=tmp_path/'releases'/commit;source.mkdir(parents=True)
    registry={'runtime_commit':commit};raw=c.canonical(registry)
    reg=tmp_path/'registry';reg.write_bytes(raw)
    transition=c.canonical({'job_key':key,'complete':False});(state/'active-transition.json').write_bytes(transition)
    record={'manual_release':fault!='automatic','request':{'base_commit':commit},'resume_requested':fault!='no_resume',
        'active_before':['eimemory-rpc.service','eimemory-nightly.timer'],
        'registry':{'units':{'eimemory-rpc.service':{'manager':'system'},'eimemory-nightly.timer':{'manager':'system'}}}}
    if fault=='quiesced_activation':record.update(resume_requested=False,quiesced=True,phase='activate')
    if fault.startswith('candidate'):
        record.update(request={'base_commit':'c'*40,'candidate_commit':commit},resumed=fault=='candidate',phase='resumed_awaiting_business')
    job=state/'jobs'/(key+'.json');job.write_bytes(c.canonical(record));job_original=job.read_bytes()
    args=SimpleNamespace(job_key=key,expected_current_commit='c'*40 if fault=='current' else commit,
        expected_registry_sha256='0'*64 if fault=='registry' else sha256(raw).hexdigest(),expected_transition_sha256=sha256(transition).hexdigest())
    monkeypatch.setattr(m,'STATE',state);monkeypatch.setattr(m,'CURRENT',source)
    monkeypatch.setattr(m,'RELEASES',source.parent);monkeypatch.setattr(m,'REGISTRY',reg)
    monkeypatch.setattr(m,'read',lambda p,**_:Path(p).read_bytes())
    monkeypatch.setattr(m,'load_contract',lambda:SimpleNamespace(validate_registry=lambda x:x,strict_json=c.strict_json,canonical=c.canonical,REGISTERED_UNITS=c.REGISTERED_UNITS))
    monkeypatch.setitem(sys.modules,'pwd',SimpleNamespace(getpwnam=lambda _:SimpleNamespace(pw_gid=1001)))
    def clean():
        if fault=='unclean':raise ValueError('unclean_storage')
    monkeypatch.setattr(m,'storage_clean',clean)
    monkeypatch.setattr(m,'_handoff_jobs',lambda *_a,**_kw:dict(abandoned=key,original=transition))
    monkeypatch.setattr(m,'healthy_original',lambda _:None)
    calls=[]
    monkeypatch.setattr(m,'call',lambda argv:calls.append(argv))
    def native(_a,manager,unit,*_):
        if manager=='user':return {'ActiveState':'inactive'}
        if unit.endswith('.timer'):
            return {'ActiveState':'failed' if fault=='failed_unit' else 'active' if calls else 'inactive'}
        return {'ActiveState':'active'}
    monkeypatch.setattr(m,'_handoff_unit_state',native)
    monkeypatch.setattr(m,'directory',lambda p,**_:Path(p).mkdir(parents=True,exist_ok=True) or Path(p))
    monkeypatch.setattr(m,'write',lambda p,data,**_:Path(p).write_bytes(data))
    if fault in {'none','candidate','quiesced_activation'}:
        m.recover_failed_manual(args)
        outcome=c.strict_json((state/'active-transition.json').read_bytes())
        assert outcome['complete'] and outcome['outcome']=='operator_abandoned_failed_manual_release'
        assert calls==[['/usr/bin/systemctl','start','eimemory-nightly.timer']]
    else:
        with pytest.raises(ValueError):m.recover_failed_manual(args)
        assert (state/'active-transition.json').read_bytes()==transition
    assert job.read_bytes()==job_original and reg.read_bytes()==raw


@pytest.mark.parametrize('fault',['none','automatic','quiesced','control_alive','main_started','mutated','invocation'])
def test_failed_prestart_has_distinct_native_evidence_and_never_a_main_exit(tmp_path,monkeypatch,fault):
    m=module();key='a'*64
    state={name:'' for name in c.NATIVE_EXIT_PROPERTIES}
    state.update(LoadState='loaded',ActiveState='failed',SubState='failed',Result='exit-code',
        InvocationID='b'*32,MainPID='0',ControlGroup='',ExecMainStatus='0',ExecMainExitTimestampMonotonic='0')
    record={'job_key':key,'unit':c.job_unit(key),'manual_release':fault!='automatic','phase':'submitted',
        'quiesced':fault=='quiesced','active_before':None,'original_invocation':'c'*32 if fault=='invocation' else None,
        'registry':{'runtime_gid':1001}}
    if fault=='mutated':record['prestart_failure']={'old':'different'}
    extra={'ControlPID':'7' if fault=='control_alive' else '0',
        'ExecMainStartTimestampMonotonic':'1' if fault=='main_started' else '0'}
    monkeypatch.setattr(m,'call',lambda argv:'\n'.join(k+'='+v for k,v in
        (extra if argv[-1].startswith('--property=ControlPID') else state).items()))
    writes=[];monkeypatch.setattr(m,'write',lambda *_a,**_kw:writes.append(True))
    if fault in {'control_alive','main_started','mutated','invocation'}:
        with pytest.raises(ValueError):m.exited_job_state(c,key,record)
        assert not writes
    else:
        _native,ended=m.exited_job_state(c,key,record)
        assert ended is (fault=='none')
        assert ('prestart_failure' in record) is (fault=='none')
        assert 'terminal_state' not in record
