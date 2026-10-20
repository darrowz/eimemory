from pathlib import Path
from types import SimpleNamespace
from hashlib import sha256
import sys
import stat
import struct
import pytest

from eimemory.contracts import runtime_manager as c
from test_runtime_manager import standalone
from test_runtime_manager_handoff import module


@pytest.mark.parametrize('fault',['none','worker','worker_restore','receipt','resume','business','complete'])
def test_manual_parent_stays_alive_through_independent_admission(tmp_path,monkeypatch,fault):
    m=standalone('runtime_manager_launch');key='a'*64;calls=[]
    monkeypatch.setattr(m,'root_contract',lambda:SimpleNamespace(JOB_ROOT=str(tmp_path/'jobs')))
    monkeypatch.setattr(m.os,'geteuid',lambda:1001,raising=False)
    # This path test substitutes fixture ownership only, never native proof.
    original=m.Path.lstat
    def lstat(path):
        st=original(path)
        return SimpleNamespace(st_mode=(st.st_mode&~0o777)|0o700,st_uid=1001)
    monkeypatch.setattr(m.Path,'lstat',lstat)
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=1 if fault.startswith('worker') else 0))
    monkeypatch.setattr(m,'read_root',lambda _:dict(resume_requested=fault=='worker_restore'))
    base=tmp_path/('b'*40);base.mkdir()
    monkeypatch.setattr(m,'CURRENT',base);monkeypatch.setattr(m,'RELEASES',tmp_path)
    request_body=dict(target_commit='c'*40,base_commit='b'*40)
    def validation(_key,operation):
        calls.append(operation)
        if fault==operation or fault=='business' and operation=='post_resume':raise ValueError('actual_child_failure')
    def request(operation,*_args,**_kwargs):
        calls.append(operation)
        if operation=='finalize_job':return dict(resumed=fault!='resume',phase='resumed_awaiting_business')
        return dict(complete=fault!='complete',operational_complete=True,phase='completed',resumed=True)
    monkeypatch.setattr(m,'manual_validation',validation);monkeypatch.setattr(m,'manual_request',request)
    if fault=='none':
        assert m.manual_worker(tmp_path,request_body,key)==0
        assert calls==['receipt','finalize_job','post_resume','complete_job']
    elif fault.startswith('worker'):
        assert m.manual_worker(tmp_path,request_body,key)==1
        assert calls==(['finalize_job'] if fault=='worker_restore' else [])
    else:
        with pytest.raises(ValueError):m.manual_worker(tmp_path,request_body,key)
        if fault in {'receipt','resume','business'}:assert 'complete_job' not in calls


@pytest.mark.parametrize('fault',['none','failed_exit','unsealed','timeout'])
def test_manual_child_requires_native_success_and_root_seal(monkeypatch,fault):
    m=standalone('runtime_manager_launch')
    monkeypatch.setattr(m,'root_contract',lambda:SimpleNamespace(VALIDATION_RUNTIME_SECONDS={'receipt':0}))
    native={'terminal_state':dict(Result='exit-code' if fault=='failed_exit' else 'success',ExecMainStatus='0',MainPID='0'),
        'sealed_artifact':None if fault=='unsealed' else {'root':'original'}}
    if fault=='timeout':native['terminal_state']=None
    monkeypatch.setattr(m,'manual_request',lambda *_a,**_kw:{'validation':native})
    times=iter([0,100]);monkeypatch.setattr(m.time,'monotonic',lambda:next(times))
    if fault=='none':assert m.manual_validation('a'*64,'receipt')==native
    else:
        with pytest.raises(ValueError):m.manual_validation('a'*64,'receipt')


@pytest.mark.parametrize('fault',['none','peer','generation','operation','oversize','denied'])
@pytest.mark.skipif(sys.platform=='win32',reason='Unix credentials transport')
def test_manual_transport_requires_host_root_and_exact_response(monkeypatch,fault):
    m=standalone('runtime_manager_launch');key='a'*64
    registry={'runtime_uid':1001,'runtime_gid':1001,'generation':'b'*64}
    contract=SimpleNamespace(validate_registry=lambda x:x,job_unit=c.job_unit,SOCKET_PATH=c.SOCKET_PATH,
        REQUEST_SCHEMA=c.REQUEST_SCHEMA,RESPONSE_SCHEMA=c.RESPONSE_SCHEMA,canonical=c.canonical,
        strict_json=c.strict_json,MAX_RESPONSE_BYTES=c.MAX_RESPONSE_BYTES)
    monkeypatch.setattr(m,'root_contract',lambda:contract);monkeypatch.setattr(m,'read_root',lambda _:registry)
    monkeypatch.setattr(m.os,'getuid',lambda:1001,raising=False)
    monkeypatch.setattr(m.os,'geteuid',lambda:1001,raising=False)
    def metadata(path):
        socket=path==Path(c.SOCKET_PATH)
        return SimpleNamespace(st_uid=0,st_gid=1001,st_nlink=1,
            st_mode=(stat.S_IFSOCK|0o660) if socket else (stat.S_IFDIR|0o755))
    monkeypatch.setattr(m.Path,'lstat',metadata)
    response={'schema':c.RESPONSE_SCHEMA,'operation':'wrong' if fault=='operation' else 'complete_job',
        'registry_generation':'c'*64 if fault=='generation' else registry['generation'],'ok':fault!='denied'}
    payload=c.canonical(response)
    data=bytearray(struct.pack('>I',c.MAX_RESPONSE_BYTES+1 if fault=='oversize' else len(payload))+payload)
    class Socket:
        def __init__(self,*_):pass
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def settimeout(self,_):pass
        def connect(self,_):pass
        def getsockopt(self,*_):return struct.pack('3i',123,1001 if fault=='peer' else 0,0)
        def sendall(self,value):assert c.strict_json(value[4:])['job_key']==key
        def recv(self,size):value=bytes(data[:size]);del data[:size];return value
    monkeypatch.setattr(m.socket,'socket',Socket)
    if fault=='none':assert m.manual_request('complete_job',key)==response
    else:
        with pytest.raises(ValueError):m.manual_request('complete_job',key)


@pytest.mark.parametrize('fault',['none','registry','current','automatic','unclean','no_resume','failed_unit'])
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
    if fault=='none':
        m.recover_failed_manual(args)
        outcome=c.strict_json((state/'active-transition.json').read_bytes())
        assert outcome['complete'] and outcome['outcome']=='operator_abandoned_failed_manual_release'
        assert calls==[['/usr/bin/systemctl','start','eimemory-nightly.timer']]
    else:
        with pytest.raises(ValueError):m.recover_failed_manual(args)
        assert (state/'active-transition.json').read_bytes()==transition
    assert job.read_bytes()==job_original and reg.read_bytes()==raw
