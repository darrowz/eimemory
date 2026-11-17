"""Independent typed-native review; property availability is real, values synthetic."""
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
AVAILABILITY = Path(__file__).parent/'fixtures/native_unit_property_availability_honrui_11587.json'


def source(name):
    spec=spec_from_file_location('native_review_'+name,ROOT/'deploy'/(name+'.py'))
    result=module_from_spec(spec);spec.loader.exec_module(result)
    return result


def fixture_row(module,row):
    # Never describes these synthetic values as observed PID/command evidence.
    item={key:'' for key in module.NATIVE_PROPERTIES if key not in row['missing_properties']}
    item.update(Id=row['unit'],LoadState=row['load_state'],ActiveState=row['active_state'],
        SubState='dead',UnitFileState='disabled' if row['load_state']=='loaded' else 'not-found')
    if row['kind']=='service':
        item.update(MainPID='0',ControlPID='0',ControlGroup='')
        if row['active_state']=='active':
            item.update(MainPID='123',InvocationID='a'*32,ControlGroup=
                '/user.slice/user-1001.slice/user@1001.service/app.slice/'+row['unit'])
        if row['load_state']=='loaded':item['ExecStart']='synthetic fixture command'
    return item


def parser(op,monkeypatch,states):
    requested=[]
    def run(argv,**kwargs):
        unit=argv[3]
        properties=argv[4].removeprefix('--property=').split(',')
        requested.append((unit,properties))
        fields=states[unit]
        return SimpleNamespace(returncode=0,stdout='\n'.join(key+'='+fields[key]
            for key in properties if key in fields))
    monkeypatch.setattr(op.subprocess,'run',run)
    return requested


def test_actual_37_unit_availability_uses_real_strict_parser_without_fill(monkeypatch):
    module=source('runtime_manager_current_user');op=source('install_user_runtime_manager')
    rows=json.loads(AVAILABILITY.read_text())['rows']
    assert len(rows)==37 and sum(bool(row['missing_properties']) for row in rows)==22
    states={row['unit']:fixture_row(module,row) for row in rows}
    parser(op,monkeypatch,states)
    old_rejections=0
    valid=0
    for row in rows:
        unit=row['unit']
        try:op.native_state(unit,module.NATIVE_PROPERTIES,{})
        except ValueError as exc:
            assert str(exc)=='user_operator_native_state_incomplete';old_rejections+=1
        requested=module.native_properties(unit,loaded=row['load_state']=='loaded')
        assert not set(requested)&set(row['missing_properties'])
        value=op.native_state(unit,requested,{})
        assert set(value)==set(requested) and value==states[unit]
        if row['active_state']=='activating':
            with pytest.raises(ValueError):module.validate_native_snapshot_unit(unit,value,1001)
        else:
            assert module.validate_native_snapshot_unit(unit,value,1001)==value;valid+=1
    assert old_rejections==22 and valid==36


@pytest.mark.parametrize('field',['MainPID','ControlPID','ControlGroup','ExecStart'])
def test_loaded_service_cannot_omit_its_required_native_field(field):
    module=source('runtime_manager_current_user')
    row=dict(unit='eimemory-rpc.service',kind='service',load_state='loaded',
        active_state='active',missing_properties=[])
    item=fixture_row(module,row);item.pop(field)
    with pytest.raises(ValueError):module.validate_native_snapshot_unit(row['unit'],item,1001)


@pytest.mark.parametrize('unit',['eimemory-backup.timer','eimemory-release-closure.path'])
def test_trigger_snapshot_refuses_manufactured_service_process_properties(unit):
    module=source('runtime_manager_current_user')
    item=fixture_row(module,dict(unit=unit,kind=unit.rsplit('.',1)[1],load_state='loaded',
        active_state='active',missing_properties=list(module.SERVICE_PROPERTIES)))
    assert module.validate_native_snapshot_unit(unit,item,1001)==item
    for key,value in [('MainPID','0'),('ControlPID','0'),('ControlGroup',''),('ExecStart','')]:
        altered={**item,key:value}
        with pytest.raises(ValueError):module.validate_native_snapshot_unit(unit,altered,1001)


def test_snapshot_rejects_loadstate_changed_between_reads(tmp_path):
    module=source('runtime_manager_current_user')
    name='eimemory-backup.service';calls=[]
    def native(unit,properties,env):
        calls.append(properties)
        return {'LoadState':'not-found'} if len(calls)==1 else {'LoadState':'loaded'}
    op=SimpleNamespace(native_state=native)
    contract=SimpleNamespace(REGISTERED_UNITS={name})
    topology=dict(runtime_uid=1001,retired_gateways=[])
    with pytest.raises(ValueError,match='native_load_changed'):
        module.native_snapshot(op,{},contract,topology,tmp_path)
    assert calls[0]==['LoadState'] and 'ExecStart' not in calls[1]


def test_quiesce_uses_typed_real_parser_and_stops_triggers_first(monkeypatch):
    module=source('runtime_manager_current_user');op=source('install_user_runtime_manager')
    rows=[row for row in json.loads(AVAILABILITY.read_text())['rows'] if row['load_state']=='loaded']
    states={row['unit']:fixture_row(module,row) for row in rows}
    before=deepcopy(states)
    requested=parser(op,monkeypatch,states);stops=[]
    def systemctl(env,verb,*units):
        assert verb=='stop';stops.append(list(units))
        for name in units:
            states[name]['ActiveState']='inactive'
            if name.endswith('.service'):states[name].update(MainPID='0',ControlPID='0',ControlGroup='')
    monkeypatch.setattr(module,'systemctl',systemctl)
    module.quiesce(op,{},dict(units=before),list(before))
    assert len(stops)==2
    assert all(name.endswith(('.timer','.path')) for name in stops[0])
    assert all(name.endswith('.service') for name in stops[1])
    for name,properties in requested:
        if not name.endswith('.service'):
            assert set(properties)=={'ActiveState','UnitFileState'}
        else:
            assert {'MainPID','ControlPID','ControlGroup'}<=set(properties)


@pytest.mark.parametrize('fault',['trigger_active','trigger_enable_changed','service_pid_alive',
    'missing_service_pid','missing_trigger_enablement'])
def test_quiesce_rejects_real_parser_missing_or_unquiet_native_fields(monkeypatch,fault):
    module=source('runtime_manager_current_user');op=source('install_user_runtime_manager')
    rows=[row for row in json.loads(AVAILABILITY.read_text())['rows'] if row['load_state']=='loaded']
    states={row['unit']:fixture_row(module,row) for row in rows};before=deepcopy(states)
    trigger=next(name for name in states if name.endswith('.timer'))
    service=next(name for name in states if name.endswith('.service'))
    requested=parser(op,monkeypatch,states)
    def stop(environment,verb,*names):
        assert verb=='stop'
        for name in names:
            states[name]['ActiveState']='inactive'
            if name.endswith('.service'):states[name].update(MainPID='0',ControlPID='0',ControlGroup='')
        if trigger in names:
            if fault=='trigger_active':states[trigger]['ActiveState']='active'
            if fault=='trigger_enable_changed':states[trigger]['UnitFileState']='enabled'
            if fault=='missing_trigger_enablement':states[trigger].pop('UnitFileState')
        if service in names:
            if fault=='service_pid_alive':states[service]['MainPID']='123'
            if fault=='missing_service_pid':states[service].pop('MainPID')
    monkeypatch.setattr(module,'systemctl',stop)
    with pytest.raises(ValueError):module.quiesce(op,{},dict(units=before),list(before))
    for name,properties in requested:
        if name.endswith(('.timer','.path')):assert not set(properties)&module.SERVICE_PROPERTIES


@pytest.mark.parametrize('first,second',[('loaded','not-found'),('not-found','loaded')])
def test_load_state_drift_through_real_native_parser_is_rejected(tmp_path,monkeypatch,first,second):
    module=source('runtime_manager_current_user');op=source('install_user_runtime_manager')
    unit='eimemory-backup.service';calls=[]
    def run(argv,**kwargs):
        properties=argv[4].removeprefix('--property=').split(',');calls.append(properties)
        state=first if len(calls)==1 else second
        item=fixture_row(module,dict(unit=unit,kind='service',load_state=state,active_state='inactive',
            missing_properties=[] if state=='loaded' else ['ExecStart']))
        return SimpleNamespace(returncode=0,stdout='\n'.join(name+'='+item[name] for name in properties if name in item))
    monkeypatch.setattr(op.subprocess,'run',run)
    with pytest.raises(ValueError,match='native_load_changed|native_state_incomplete'):
        module.native_snapshot(op,{},SimpleNamespace(REGISTERED_UNITS={unit}),dict(runtime_uid=1001,retired_gateways=[]),tmp_path)
    assert calls[0]==['LoadState']
