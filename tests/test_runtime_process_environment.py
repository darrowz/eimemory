from stdlib_isolation import isolate_stdlib
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from hashlib import sha256
import pytest

from test_runtime_manager import standalone
from eimemory.contracts import runtime_manager as c


@pytest.mark.parametrize('fault',['none','unit','owner','cgroup','duplicate','restart','inactive','unapplied',
    'namespace_nested','namespace_invalid','namespace_zero','namespace_foreign','namespace_changed'])
def test_host_observes_only_the_original_runtime_owned_process(tmp_path,monkeypatch,fault):
    m=standalone('runtime_manager_bridge');bridge=m.Bridge(c);bridge.proc=tmp_path/'proc'
    process=bridge.proc/'123';process.mkdir(parents=True)
    owner_status='Uid:\t1001\t1001\t1001\t1001\n'
    namespace={'namespace_nested':'123 1','namespace_invalid':'123 bad','namespace_zero':'123 0',
        'namespace_foreign':'456 1','namespace_changed':'123 1'}.get(fault)
    (process/'status').write_text(owner_status+('NSpid:\t'+namespace+'\n' if namespace else ''))
    (process/'stat').write_text('123 (runtime with spaces) '+' '.join(['S']+['0']*18+['4321']))
    group='/system.slice/eimemory-rpc.service'
    (process/'cgroup').write_text('0::'+('/other.service' if fault=='cgroup' else group)+'\n')
    token=sha256(b'reviewed settings'+b'reviewed launcher').hexdigest()
    monkeypatch.setattr(m,'read_file',lambda p,**_:b'reviewed settings' if p.name=='runtime-settings.v1.json' else b'reviewed launcher')
    marker='0'*64 if fault=='unapplied' else token
    (process/'environ').write_bytes(b'CAPTURE=1\0PATH=/usr/bin\0EIMEMORY_RUNTIME_SETTINGS_APPLIED='+marker.encode()+b'\0'+(b'CAPTURE=2\0' if fault=='duplicate' else b''))
    original=Path.stat
    bridge.proc=m.Path(bridge.proc)
    isolate_stdlib(m, monkeypatch)
    monkeypatch.setattr(m.Path,'stat',lambda p,**kw:SimpleNamespace(st_uid=9 if fault=='owner' else 1001) if p==process else original(p,**kw))
    state=dict(Id='eimemory-rpc.service',LoadState='loaded',ActiveState='inactive' if fault=='inactive' else 'active',
        MainPID='123',InvocationID='a'*32,ControlGroup=group)
    reads=[]
    def native(*_):
        reads.append(True);result=deepcopy(state)
        if fault=='restart' and len(reads)>1:result['MainPID']='456'
        if fault=='namespace_changed' and len(reads)>1:(process/'status').write_text(owner_status+'NSpid:\t123 2\n')
        return result
    monkeypatch.setattr(bridge,'unit_state',native)
    if fault in {'none','namespace_nested'}:
        value=bridge.runtime_process_environment({'runtime_uid':1001,'units':{name:{'manager':'user' if name in c.USER_COMPAT_UNITS else 'system'} for name in c.REGISTERED_UNITS}},state['Id'])
        assert value['pid']==123 and value['start_ticks']==4321
        assert value['environment']=={'CAPTURE':'1','PATH':'/usr/bin','EIMEMORY_RUNTIME_SETTINGS_APPLIED':token}
        assert value['namespace_pids']==([123,1] if fault=='namespace_nested' else [])
    else:
        with pytest.raises(ValueError):bridge.runtime_process_environment({'runtime_uid':1001,'units':{name:{'manager':'user' if name in c.USER_COMPAT_UNITS else 'system'} for name in c.REGISTERED_UNITS}},'sshd.service' if fault=='unit' else state['Id'])


@pytest.mark.parametrize('fault',['first','bound','changed'])
def test_runtime_settings_are_exec_applied_once_before_product_entry(tmp_path,monkeypatch,fault):
    m=standalone('runtime_manager_launch');source=tmp_path/'runtime_manager_launch.py';source.write_bytes(b'reviewed launcher')
    settings=tmp_path/'runtime-settings.v1.json';settings.write_bytes(b'reviewed settings')
    token=sha256(settings.read_bytes()+source.read_bytes()).hexdigest()
    monkeypatch.setattr(m,'__file__',str(source));monkeypatch.setattr(m,'read_root',lambda p,**_:Path(p).read_bytes())
    marker='EIMEMORY_RUNTIME_SETTINGS_APPLIED';monkeypatch.delenv(marker,raising=False)
    if fault!='first':monkeypatch.setenv(marker,token if fault=='bound' else '0'*64)
    def apply(module,args):
        monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY','1');return ['transformed']
    monkeypatch.setattr(m,'apply_runtime_settings',apply)
    monkeypatch.setattr(m,'protected_cache',lambda:['-I','-B'])
    observed=[]
    def execute(binary,argv,environment):observed.append((binary,argv,environment));raise RuntimeError('fixture_exec')
    monkeypatch.setattr(m.os,'execve',execute)
    if fault=='first':
        with pytest.raises(RuntimeError,match='fixture_exec'):m.exec_runtime_settings('eimemory.cli.main',['original'])
        assert observed[0][2][marker]==token and observed[0][2]['EIMEMORY_CAPTURE_ORIGINAL_QUERY']=='1'
        assert observed[0][1][-1]=='transformed'
    elif fault=='bound':assert m.exec_runtime_settings('eimemory.cli.main',['transformed'])==['transformed'] and not observed
    else:
        with pytest.raises(ValueError):m.exec_runtime_settings('eimemory.cli.main',[])
        assert not observed


@pytest.mark.parametrize('fault',['none','pid','unit','invocation','ticks','nul','request_denied'])
def test_registered_stability_uses_native_host_observation_without_proc_fallback(monkeypatch,fault):
    from eimemory.ops import controlled_stability as s
    from eimemory.ops import runtime_service_observation as owner
    from eimemory.core import runtime_manager as client
    value=dict(schema='eimemory.runtime_process_environment.v1',unit='eimemory-rpc.service',pid=123,start_ticks=4321,
        invocation_id='a'*32,environment={'CAPTURE':'1'})
    if fault=='pid':value['pid']=456
    if fault=='unit':value['unit']='another.service'
    if fault=='invocation':value['invocation_id']=''
    if fault=='ticks':value['start_ticks']=0
    if fault=='nul':value['environment']['CAPTURE']='bad\0value'
    monkeypatch.setattr(owner,'registered_runtime_context',lambda:True)
    def request(*a,**kw):
        if fault=='request_denied':raise RuntimeError('actual_host_denial')
        return {'observation':value}
    monkeypatch.setattr(client,'request',request)
    state={'Id':'eimemory-rpc.service','MainPID':'123'}
    if fault=='none':assert s._process_environment(state)==(123,Path('/proc/123'),{'CAPTURE':'1'})
    else:
        with pytest.raises((ValueError,RuntimeError)):s._process_environment(state)

@pytest.mark.parametrize('fault',['none','too_large','nan','missing','wrong_pid','denied'])
def test_managed_hermes_timeout_uses_authenticated_host_observation(monkeypatch,capsys,fault):
    module=standalone('runtime_manager_control')
    from eimemory.core import runtime_manager as client
    monkeypatch.setattr(module.os,'geteuid',lambda:1001,raising=False)
    monkeypatch.setattr(module,'unit_state',lambda unit,properties:{'Id':unit,'MainPID':'123'})
    environment={'EIMEMORY_ADAPTER_TIMEOUT_SECONDS':'8','PRIVATE_TOKEN':'never-print-this-secret'}
    if fault=='too_large':environment['EIMEMORY_ADAPTER_TIMEOUT_SECONDS']='31'
    if fault=='nan':environment['EIMEMORY_ADAPTER_TIMEOUT_SECONDS']='nan'
    if fault=='missing':environment.pop('EIMEMORY_ADAPTER_TIMEOUT_SECONDS')
    def request(operation,**kw):
        assert operation=='runtime_process_environment' and kw=={'unit':'hermes-gateway.service'}
        if fault=='denied':raise RuntimeError('host denied')
        return {'observation':dict(schema='eimemory.runtime_process_environment.v1',unit='hermes-gateway.service',
            pid=124 if fault=='wrong_pid' else 123,start_ticks=10,invocation_id='a'*32,environment=environment)}
    monkeypatch.setattr(client,'request',request)
    if fault=='none':
        assert module.main(['hermes-adapter-timeout'])==0
        assert capsys.readouterr().out=='8\n'
    else:
        with pytest.raises((ValueError,RuntimeError)):module.main(['hermes-adapter-timeout'])
        assert capsys.readouterr().out==''


@pytest.mark.parametrize('denied',[False,True])
def test_managed_bootstrap_uses_host_binding_observation_without_proc_fallback(monkeypatch,denied):
    module=standalone('bootstrap_production_recall')
    from eimemory.ops import runtime_service_observation as owner,controlled_stability as stability
    monkeypatch.delenv('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE',raising=False)
    monkeypatch.setattr(owner,'registered_runtime_context',lambda:True)
    monkeypatch.setattr(owner,'service_state',lambda unit,properties:{'Id':unit,'MainPID':'123'})
    def observe(state):
        if denied:raise RuntimeError('host observation denied')
        return {'environment':{'EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE':'/var/lib/eimemory/state/real-bindings.json'}}
    monkeypatch.setattr(stability,'_host_process_observation',observe)
    import subprocess
    monkeypatch.setattr(subprocess,'check_output',lambda *a,**k:pytest.fail('direct host discovery is forbidden'))
    if denied:
        with pytest.raises(RuntimeError,match='host observation denied'):module._adopt_release_scope_bindings_env()
        assert 'EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE' not in module.os.environ
    else:
        module._adopt_release_scope_bindings_env()
        assert module.os.environ['EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE']=='/var/lib/eimemory/state/real-bindings.json'
        monkeypatch.delenv('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
