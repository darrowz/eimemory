#!/usr/bin/python3
"""Root-owned fixed-action host bridge. Install only through reviewed bootstrap.

There are no product imports. The adjacent contract is a reviewed root-owned
copy installed with this file; an editable checkout is never a root entrypoint.
"""
from __future__ import annotations
from hashlib import sha256
import importlib.util
import hmac
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import struct
import subprocess
import threading
from contextlib import contextmanager
import time

ROOT = Path('/usr/libexec/eimemory-manager')
STATE = Path('/var/lib/eimemory-manager')
REGISTRY = Path('/etc/eimemory-code-automation-authority/runtime-manager.v1.json')
SOCKET = Path('/run/eimemory-manager/bridge.sock')
CURRENT = Path('/opt/eimemory/current')
RELEASES = Path('/opt/eimemory/releases')


def trusted_directory(path, *, mode=None):
    path = Path(path)
    for item in (path, *path.parents):
        st = item.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) & 0o022:
            raise ValueError('manager_root_directory_untrusted')
    if mode is not None and stat.S_IMODE(path.stat().st_mode) != mode:
        raise ValueError('manager_root_directory_mode_invalid')


def read_file(path, *, uid=0, mode=0o640, maximum=65536):
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        a = os.fstat(fd)
        if not stat.S_ISREG(a.st_mode) or a.st_uid != uid or a.st_nlink != 1 or stat.S_IMODE(a.st_mode) != mode or a.st_size > maximum:
            raise ValueError('manager_file_untrusted')
        raw = os.read(fd, maximum + 1)
        b, named = os.fstat(fd), path.lstat()
        identity = lambda s: (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_gid,s.st_nlink)
        if identity(a) != identity(b) or identity(a) != identity(named):
            raise ValueError('manager_file_changed')
        return raw
    finally:
        os.close(fd)


def load_contract():
    trusted_directory(ROOT, mode=0o750)
    path = ROOT / 'runtime_manager_contract.py'
    raw=read_file(path)
    spec = importlib.util.spec_from_file_location('runtime_manager_contract', path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(raw,str(path),'exec'),module.__dict__)
    return module


def atomic_json(path, value, *, gid, exclusive=False):
    path = Path(path)
    raw = json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
    temporary = path.with_name(path.name+'.tmp.'+str(os.getpid())+'.'+str(threading.get_ident()))
    fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o640)
    try:
        os.fchown(fd,0,gid);os.fchmod(fd,0o640)
        with os.fdopen(fd,'wb',closefd=False) as stream:
            stream.write(raw);stream.flush();os.fsync(fd)
        if exclusive:
            os.link(temporary,path,follow_symlinks=False)
        else:
            if path.exists():read_file(path)
            os.replace(temporary,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        os.close(fd)
        if temporary.exists():temporary.unlink()


class Bridge:
    def __init__(self, contract, *, registry=REGISTRY, state=STATE, command=None, proc=Path('/proc')):
        self.c=contract;self.registry_path=Path(registry);self.state=Path(state)
        self.proc=Path(proc);self.command=command or self._command
        self.mutex=threading.Lock()

    @contextmanager
    def lifecycle_lock(self):
        # Shared with the root bootstrap/restore operation. A process restart
        # releases only the kernel lock, never the durable transition record.
        import fcntl
        trusted_directory(self.state,mode=0o750)
        path=self.state/'lifecycle.lock'
        fd=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600)
        try:
            meta=os.fstat(fd);named=path.lstat()
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0 or meta.st_nlink!=1
                    or stat.S_IMODE(meta.st_mode)!=0o600 or (meta.st_dev,meta.st_ino)!=(named.st_dev,named.st_ino)):
                raise ValueError('manager_lifecycle_lock_untrusted')
            deadline=time.monotonic()+5
            while True:
                try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                except BlockingIOError:
                    if time.monotonic()>=deadline:raise ValueError('manager_lifecycle_lock_timeout')
                    time.sleep(0.025)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _command(args):
        result=subprocess.run(args,check=False,capture_output=True,text=True,timeout=600,
            env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C','TZ':'UTC'})
        if result.returncode!=0:raise ValueError('manager_command_failed')
        if len(result.stdout.encode())>8*1024*1024:raise ValueError('manager_output_limit')
        return result.stdout

    def registry(self):
        trusted_directory(self.registry_path.parent)
        return self.c.validate_registry(self.c.strict_json(read_file(self.registry_path)))

    def systemctl(self,registry,unit,*args):
        if self.c.is_dynamic_unit(unit):
            manager='system'
        else:
            if unit not in registry['units']:raise ValueError('manager_unit_not_registered')
            manager=registry['units'][unit]['manager']
        command=['/usr/bin/systemctl']
        if manager=='user':
            uid=registry['runtime_uid']
            command=['/usr/sbin/runuser','-u',registry['runtime_user'],'--','/usr/bin/env',
                'XDG_RUNTIME_DIR=/run/user/'+str(uid),'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(uid)+'/bus',
                '/usr/bin/systemctl','--user']
        return self.command(command+list(args)+[unit])

    def unit_state(self,registry,unit,properties):
        if not isinstance(properties,list) or not properties or any(p not in self.c.READ_PROPERTIES for p in properties):
            raise ValueError('manager_property_not_allowlisted')
        optional={'openclaw-loop-watch.timer','openclaw-loop-compact.timer',
            'openclaw-loop-watch.service','openclaw-loop-compact.service'}
        raw=self.systemctl(registry,unit,'show','--no-pager','--property='+','.join(properties))
        state=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
        if unit in optional and state.get('LoadState')=='not-found':
            return {p:state.get(p,'') for p in properties}
        if any(p not in state for p in properties):raise ValueError('manager_state_incomplete')
        return {p:state[p] for p in properties}

    def journal(self,registry,unit,invocation,limit):
        self.c.hex_value(invocation,32,'manager_invocation_invalid')
        if type(limit) is not int or not 1<=limit<=5000:raise ValueError('manager_journal_limit_invalid')
        actual=self.unit_state(registry,unit,['InvocationID'])['InvocationID']
        if actual!=invocation:raise ValueError('manager_journal_invocation_mismatch')
        manager='system' if self.c.is_dynamic_unit(unit) else registry['units'][unit]['manager']
        args=['/usr/bin/journalctl','--quiet','--no-pager','--output=json','--lines='+str(limit+1),
            '_SYSTEMD_INVOCATION_ID='+invocation]
        if manager=='system':args+=['_SYSTEMD_UNIT='+unit]
        else:args+=['_SYSTEMD_USER_UNIT='+unit,'_UID='+str(registry['runtime_uid'])]
        raw=self.command(args)
        if len(raw.splitlines())>limit:raise ValueError('manager_journal_truncated')
        return {'journal':raw,'journal_sha256':sha256(raw.encode()).hexdigest(),'invocation':invocation}

    def journal_output(self,args):
        # One shared original 12-second budget covers readability and filtering.
        # Empty grep is valid only after an actual successful native preflight.
        deadline=time.monotonic()+12
        def run(command):
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('readonly_command_timeout')
            result=subprocess.run(command,capture_output=True,text=True,timeout=remaining,check=False,
                env={'PATH':'/usr/bin:/bin','LC_ALL':'C','TZ':'UTC'})
            if time.monotonic()>deadline:raise TimeoutError('readonly_command_timeout')
            if result.stderr:raise RuntimeError('journal_command_unavailable')
            if len(result.stdout.encode())>2*1024*1024:raise ValueError('readonly_command_output_limit')
            return result
        preflight=['--lines=1' if item.startswith('--lines=') else item for item in args
            if not item.startswith(('--grep=','--case-sensitive='))]
        result=run(preflight)
        if result.returncode!=0:raise RuntimeError('journal_preflight_unavailable')
        lines=result.stdout.splitlines()
        if len(lines)>1 or any(not isinstance(self.c.strict_json(line.encode(),max_bytes=262144),dict) for line in lines):
            raise ValueError('journal_preflight_contract_invalid')
        result=run(args)
        if result.returncode==0 or (result.returncode==1 and result.stdout==''):return result.stdout
        raise RuntimeError('journal_command_unavailable')

    def error_window(self,registry):
        # Counts only: credential-bearing messages never leave the root bridge.
        # The native OR query covers ALL invocations, including a failed prefix.
        end=time.time();counts={name:0 for name,_ in self.c.JOURNAL_ERROR_PATTERNS}
        pattern='|'.join('(?:'+pattern+')' for _,pattern in self.c.JOURNAL_ERROR_PATTERNS)
        args=['/usr/bin/journalctl','--quiet','--no-pager','--all','--output=json','--lines=2001',
            '--since=@'+str(end-600),'--until=@'+str(end),'--grep='+pattern,'--case-sensitive=no']
        groups=[]
        for unit,item in sorted(registry['units'].items()):
            if not item['installed'] or not unit.endswith('.service'):continue
            if item['manager']=='system':groups.append(['_SYSTEMD_UNIT='+unit])
            else:groups.append(['_SYSTEMD_USER_UNIT='+unit,'_UID='+str(registry['runtime_uid'])])
        if not groups:raise ValueError('manager_journal_inventory_empty')
        for index,group in enumerate(groups):
            if index:args.append('+')
            args+=group
        lines=self.journal_output(args).splitlines()
        if len(lines)>2000:raise ValueError('journal_window_truncated')
        for line in lines:
            entry=self.c.strict_json(line.encode(),max_bytes=262144)
            if not isinstance(entry,dict) or not isinstance(entry.get('MESSAGE'),str):raise ValueError('journal_message_contract_invalid')
            matched={name:bool(re.search(pattern,entry['MESSAGE'],re.I)) for name,pattern in self.c.JOURNAL_ERROR_PATTERNS}
            if not any(matched.values()):raise ValueError('journal_filter_contract_invalid')
            for name,hit in matched.items():counts[name]+=int(hit)
        return {'ok':not any(counts.values()),'window_seconds':600,
            'observation_scope':'registered_manager_all_invocations_journal',
            'entries_observed':len(lines),'entry_selection':'native_error_pattern_matches',
            'native_database_counters_available':False,'observed_error_counts':counts}

    def bus_property(self,path,interface,name):
        if not isinstance(path,str) or not re.fullmatch(r'/org/freedesktop/systemd1/(?:unit/[A-Za-z0-9_]+|job/[0-9]+)',path):
            raise ValueError('manager_bus_object_invalid')
        raw=self.command(['/usr/bin/busctl','--json=short','--system','get-property',
            'org.freedesktop.systemd1',path,interface,name])
        value=self.c.strict_json(raw.encode())
        if not isinstance(value,dict) or set(value)!={'type','data'}:raise ValueError('manager_bus_property_shape_invalid')
        return value

    def unit_bus_path(self,unit):
        raw=self.command(['/usr/bin/busctl','--json=short','--system','call','org.freedesktop.systemd1',
            '/org/freedesktop/systemd1','org.freedesktop.systemd1.Manager','GetUnit','s',unit])
        value=self.c.strict_json(raw.encode())
        if not isinstance(value,dict) or value.get('type')!='o' or not isinstance(value.get('data'),list) or len(value['data'])!=1:
            raise ValueError('manager_bus_unit_shape_invalid')
        return value['data'][0]

    def activation_cause(self,registry,unit,state):
        # Unit.ActivationDetails can describe a previous invocation. Only the
        # immutable CURRENT running Job object's details may establish a cause.
        unknown={'trigger_kind':'unknown','trigger_unit':'','job_id':0,'job_state':'',
            'activation_details_digest':self.c.digest({'cause':'unknown'})}
        try:
            unitpath=self.unit_bus_path(unit)
            current=self.bus_property(unitpath,'org.freedesktop.systemd1.Unit','Job')
            pair=current.get('data')
            if current.get('type')!='(uo)' or not isinstance(pair,list) or len(pair)!=2 or type(pair[0]) is not int or pair[0]<=0:
                return unknown
            jobid,jobpath=pair
            if jobpath!='/org/freedesktop/systemd1/job/'+str(jobid):return unknown
            fields={name:self.bus_property(jobpath,'org.freedesktop.systemd1.Job',name) for name in ('Id','Unit','JobType','State','ActivationDetails')}
            if fields['Id']!={'type':'u','data':jobid} or fields['Unit']!={'type':'(so)','data':[unit,unitpath]} or fields['JobType']!={'type':'s','data':'start'} or fields['State']!={'type':'s','data':'running'}:
                return unknown
            details=fields['ActivationDetails']
            if details.get('type')!='a(ss)' or not isinstance(details.get('data'),list):return unknown
            pairs=details['data']
            if any(not isinstance(item,list) or len(item)!=2 or any(not isinstance(v,str) for v in item) for item in pairs):return unknown
            values=dict(pairs)
            if len(values)!=len(pairs):return unknown
            timer=values.get('trigger_unit','')
            if not timer.endswith('.timer') or timer not in registry['units'] or registry['units'][timer]['manager']!='system' or not registry['units'][timer]['installed']:
                return unknown
            stamp=values.get('trigger_timer_monotonic_usec','')
            if not stamp.isdigit() or int(stamp)<=0:return unknown
            start=int(state['ExecMainStartTimestampMonotonic'])
            if int(stamp)>start or start-int(stamp)>60000000:return unknown
            timerpath=self.unit_bus_path(timer)
            last=self.bus_property(timerpath,'org.freedesktop.systemd1.Timer','LastTriggerUSecMonotonic')
            target=self.bus_property(timerpath,'org.freedesktop.systemd1.Timer','Unit')
            if last!={'type':'t','data':int(stamp)} or target!={'type':'s','data':unit}:return unknown
            # Reread the job reference and service identity around all property
            # reads. Configuration or a near timestamp alone is never proof.
            if self.bus_property(unitpath,'org.freedesktop.systemd1.Unit','Job')!=current:return unknown
            again=self.unit_state(registry,unit,['InvocationID','ControlGroup','ExecMainStartTimestampMonotonic'])
            if any(again[k]!=state[k] for k in again):return unknown
            return {'trigger_kind':'scheduled_timer','trigger_unit':timer,'job_id':jobid,'job_state':'running',
                'activation_details_digest':self.c.digest(fields)}
        except (OSError,ValueError,RuntimeError,KeyError,TypeError):
            return unknown

    def activation_attestation(self,registry,pid,uid):
        if uid!=registry['runtime_uid']:raise ValueError('manager_peer_uid_denied')
        process=self.proc/str(pid)
        status=dict(line.split(':',1) for line in (process/'status').read_text().splitlines() if ':' in line)
        if status.get('NoNewPrivs','').strip()!='1' or status.get('Uid','').split()!=[str(uid)]*4:
            raise ValueError('manager_activation_peer_identity_invalid')
        start_ticks=int((process/'stat').read_text().rsplit(')',1)[1].split()[19])
        groups=[line[3:] for line in (process/'cgroup').read_text().splitlines() if line.startswith('0::')]
        if len(groups)!=1:raise ValueError('manager_activation_cgroup_invalid')
        cgroup=groups[0];unit=cgroup.rsplit('/',1)[-1]
        if unit.startswith(self.c.JOB_UNIT_PREFIX):
            key=unit[len(self.c.JOB_UNIT_PREFIX):-8];record=self.record(key)
            self.verify_job_peer(registry,record,pid,uid);scope=record['request'].get('scope')
            if scope is None:raise ValueError('manager_activation_manual_release_has_no_incident_owner')
        else:
            if unit not in registry['unit_scopes'] or registry['units'][unit]['manager']!='system' or not registry['units'][unit]['installed']:
                raise ValueError('manager_activation_unit_scope_not_reviewed')
            scope=registry['unit_scopes'][unit]
        state=self.unit_state(registry,unit,['InvocationID','ControlGroup','ExecMainStartTimestampMonotonic'])
        self.c.hex_value(state['InvocationID'],32,'manager_activation_invocation_invalid')
        if not cgroup.startswith('/system.slice/') or state['ControlGroup']!=cgroup:
            raise ValueError('manager_activation_native_cgroup_mismatch')
        if CURRENT.resolve(strict=True)!=RELEASES/registry['runtime_commit']:
            raise ValueError('manager_activation_current_registry_mismatch')
        boot=(self.proc/'sys/kernel/random/boot_id').read_text().strip()
        if re.fullmatch('[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',boot) is None:raise ValueError('manager_activation_boot_invalid')
        cause=self.activation_cause(registry,unit,state)
        if int((process/'stat').read_text().rsplit(')',1)[1].split()[19])!=start_ticks:
            raise ValueError('manager_activation_peer_changed')
        payload={'schema':'eimemory.runtime_activation_attestation.v1','registry_generation':registry['generation'],
            'scope':scope,'manager':'system','unit':unit,'runtime_commit':registry['runtime_commit'],'controller_commit':registry['controller_commit'],'invocation_id':state['InvocationID'],'boot_id':boot,
            'observed_at':datetime.now(timezone.utc).isoformat(),'observed_monotonic_ns':time.monotonic_ns(),
            'service_start_monotonic_usec':int(state['ExecMainStartTimestampMonotonic']),
            'peer_pid':pid,'peer_uid':uid,'peer_start_ticks':start_ticks,'actual_unit_cgroup':cgroup,**cause}
        key=read_file(self.state/'activation-signing.key',mode=0o600,maximum=32)
        if len(key)!=32:raise ValueError('manager_activation_key_invalid')
        receipt={'payload':payload,'signature':hmac.new(key,self.c.canonical(payload),'sha256').hexdigest()}
        receipt_id=self.c.digest(payload);path=self.state/'activation-receipts'/(receipt_id+'.json')
        trusted_directory(path.parent,mode=0o750)
        atomic_json(path,receipt,gid=registry['runtime_gid'],exclusive=True)
        return {'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':receipt_id,'digest':self.c.digest(receipt)}

    def verify_activation(self,receipt_id,digest):
        self.c.hex_value(receipt_id,64,'manager_activation_receipt_id_invalid');self.c.hex_value(digest,64,'manager_activation_receipt_digest_invalid')
        path=self.state/'activation-receipts'/(receipt_id+'.json')
        trusted_directory(path.parent,mode=0o750)
        raw=read_file(path);value=self.c.strict_json(raw)
        if self.c.canonical(value)!=raw or sha256(raw).hexdigest()!=digest or set(value)!={'payload','signature'} or self.c.digest(value['payload'])!=receipt_id:
            raise ValueError('manager_activation_receipt_digest_mismatch')
        key=read_file(self.state/'activation-signing.key',mode=0o600,maximum=32)
        expected=hmac.new(key,self.c.canonical(value['payload']),'sha256').hexdigest()
        if not hmac.compare_digest(expected,value['signature']):raise ValueError('manager_activation_receipt_signature_invalid')
        return {**value['payload'],'signature_verified':True,
            'reference':{'schema':'eimemory.runtime_activation_receipt_ref.v1','receipt_id':receipt_id,'digest':digest}}

    def managed_authority(self,registry):
        if registry['managed_incident_authority'].get('enabled') is not True:
            raise ValueError('manager_managed_authority_not_enabled')
        # Only the bootstrap-reviewed root copy is imported. No product path,
        # source module, key location or manifest is chosen by the request.
        trusted_directory(ROOT,mode=0o750)
        path=ROOT/'managed_incident_proof_authority.py';raw=read_file(path,maximum=1048576)
        spec=importlib.util.spec_from_file_location('reviewed_managed_incident_authority',path)
        authority=importlib.util.module_from_spec(spec);exec(compile(raw,str(path),'exec'),authority.__dict__)
        return authority

    def record_path(self,key):
        self.c.hex_value(key,64,'manager_job_key_invalid')
        return self.state / 'jobs' / (key+'.json')

    def record(self,key):
        path=self.record_path(key)
        trusted_directory(path.parent,mode=0o750)
        value=self.c.strict_json(read_file(path))
        if not isinstance(value,dict) or value.get('job_key')!=key:raise ValueError('manager_job_record_invalid')
        if value.get('manual_release') is True:self.c.validate_manual_permit(value.get('request'),key)
        else:self.c.validate_job(value.get('request'),key)
        self.c.validate_registry(value.get('registry'))
        if value.get('unit')!=self.c.job_unit(key):raise ValueError('manager_job_record_unit_invalid')
        return value

    def save(self,registry,record,*,exclusive=False):
        atomic_json(self.record_path(record['job_key']),record,gid=registry['runtime_gid'],exclusive=exclusive)

    def load_request(self,registry,key):
        directory=Path(self.c.JOB_ROOT)/key
        self.c.hex_value(key,64,'manager_job_key_invalid')
        # A request is data, never a root Python entrypoint. Each component below
        # the runtime state root must be a directory, not a symlink.
        for parent in (directory,directory.parent,directory.parent.parent):
            st=parent.lstat()
            if not stat.S_ISDIR(st.st_mode) or st.st_uid!=registry['runtime_uid'] or stat.S_IMODE(st.st_mode)&0o022:
                raise ValueError('manager_request_parent_untrusted')
        raw=read_file(directory/'request.json',uid=registry['runtime_uid'],mode=0o600)
        value=self.c.validate_job(self.c.strict_json(raw),key)
        if raw!=self.c.canonical(value):raise ValueError('manager_request_not_canonical')
        if value['controller_commit']!=registry['controller_commit']:raise ValueError('manager_controller_not_registered')
        self.verify_controller_entry(registry,value['controller_commit'])
        return value

    def verify_controller_entry(self,registry,commit):
        pin=registry.get('controller_entry_digest')
        if pin is None:raise ValueError('manager_controller_entry_not_reviewed')
        path=RELEASES/commit/self.c.CONTROLLER_ENTRY
        if path.is_symlink() or not path.is_file() or path.resolve(strict=True)!=path:raise ValueError('manager_controller_entry_missing')
        raw=read_file(path,uid=registry['runtime_uid'],mode=stat.S_IMODE(path.stat().st_mode),maximum=65536)
        if sha256(raw).hexdigest()!=pin:raise ValueError('manager_controller_entry_digest_mismatch')

    def submit(self,registry,key):
        path=self.record_path(key)
        if path.exists():
            # Durable submission is never replayed as a second start after an
            # ambiguous systemctl result or daemon restart.
            return self.job_status(registry,key)
        request=self.load_request(registry,key)
        record={'job_key':key,'request':request,'registry':registry,'phase':'submitted',
            'unit':self.c.job_unit(key),'active_before':None,'quiesced':False,'resumed':False}
        self.save(registry,record,exclusive=True)
        self.systemctl(registry,record['unit'],'start','--no-block')
        return self.job_status(registry,key)

    def job_status(self,registry,key):
        record=self.record(key)
        state=self.unit_state(record['registry'],record['unit'],['LoadState','ActiveState','SubState','Result','InvocationID','ExecMainStatus','MainPID','ControlGroup','ExecMainExitTimestampMonotonic'])
        invocation=state.get('InvocationID')
        if invocation:
            self.c.hex_value(invocation,32,'manager_job_invocation_invalid')
            if record.get('original_invocation') not in {None,invocation}:raise ValueError('manager_job_invocation_changed')
            if record.get('original_invocation') is None:
                record['original_invocation']=invocation;self.save(registry,record)
        if self.pin_terminal(record,state,'job'):self.save(registry,record)
        return {'manager':'system','unit':record['unit'],'state':state,'phase':record['phase'],'job_key':key,
            'original_invocation':record.get('original_invocation'),'resumed':record.get('resumed') is True,
            'terminal_state':record.get('terminal_state'),'request':record['request'],
            'manual_release':record.get('manual_release') is True,
            'launch_controller_commit':record['request']['controller_commit'],
            'validation_controller_commit':record['request'].get('validation_controller_commit',record['request']['controller_commit']),
            'complete':record.get('finalized') is True,'held_background':record.get('held_background',[])}

    def pin_terminal(self,record,state,label):
        """An actual native exit is independent of a receipt or sealed bytes."""
        old=record.get('terminal_state');actual=state.get('InvocationID')
        if actual and actual!=record.get('original_invocation'):
            raise ValueError('manager_'+label+'_invocation_changed')
        if old is not None:
            if state.get('MainPID') not in {'0',''}:raise ValueError('manager_'+label+'_terminal_process_changed')
            if not self.c.native_terminal_readback(old,state,record.get('original_invocation'),record['unit']):
                raise ValueError('manager_'+label+'_terminal_state_changed')
            return False
        if self.c.native_terminal_exit(state,record.get('original_invocation'),record['unit']):
            record['terminal_state']=dict(state);return True
        return False

    def release_job(self,registry,key):
        result=self.job_status(registry,key);record=self.record(key)
        terminal=result.get('terminal_state')
        if (record.get('finalized') is not True or terminal is None or terminal.get('Result')!='success'
                or terminal.get('ExecMainStatus')!='0' or record.get('phase')!='completed'):
            raise ValueError('manager_job_release_without_verified_completion')
        state=result['state']
        if state.get('ActiveState')=='active' and state.get('SubState')=='exited':
            record['release_requested']=True;self.save(registry,record)
            self.systemctl(record['registry'],record['unit'],'stop','--no-block')
        return {**result,'release_requested':True}

    def validation_record_path(self,key,operation):
        self.c.validation_unit(key,operation)
        return self.state/'validations'/(key+'-'+operation+'.json')

    def validation_artifact(self,registry,key,operation):
        self.c.validation_unit(key,operation)
        parent=Path(self.c.JOB_ROOT)/key
        for directory in (parent,parent.parent):
            meta=directory.lstat()
            if (not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=registry['runtime_uid']
                    or stat.S_IMODE(meta.st_mode)!=0o700):raise ValueError('manager_validation_artifact_parent_untrusted')
        path=parent/('validation-'+operation+'-result.json')
        if not path.exists() and not path.is_symlink():return None
        return read_file(path,uid=registry['runtime_uid'],mode=0o600,maximum=4*1024*1024)

    def validation_status(self,registry,key,operation):
        path=self.validation_record_path(key,operation)
        trusted_directory(path.parent,mode=0o750)
        record=self.c.strict_json(read_file(path))
        parent=self.record(key)
        if (record.get('job_key')!=key or record.get('operation')!=operation
                or record.get('unit')!=self.c.validation_unit(key,operation)
                or record.get('parent_original_invocation')!=parent.get('original_invocation')):
            raise ValueError('manager_validation_record_binding_invalid')
        state=self.unit_state(record['registry'],record['unit'],['LoadState','ActiveState','SubState','Result','InvocationID','ExecMainStatus','MainPID','ControlGroup','ExecMainExitTimestampMonotonic'])
        actual=state.get('InvocationID')
        if actual:
            self.c.hex_value(actual,32,'manager_validation_invocation_invalid')
            if record.get('original_invocation') not in {None,actual}:raise ValueError('manager_validation_invocation_changed')
            if record.get('original_invocation') is None:
                record['original_invocation']=actual;atomic_json(path,record,gid=registry['runtime_gid'])
        seal=self.validation_seal(registry,key,operation,record)
        if self.pin_terminal(record,state,'validation'):
            if seal is None and state['Result']=='success' and state.get('ExecMainStatus')=='0':
                raise ValueError('manager_validation_success_artifact_not_sealed')
            atomic_json(path,record,gid=registry['runtime_gid'])
        return {'job_key':key,'operation':operation,'manager':'system','unit':record['unit'],'state':state,
            'artifact_sha256':seal['artifact_sha256'] if seal else None,'sealed_artifact':seal['reference'] if seal else None,'terminal_state':record.get('terminal_state'),
            'original_invocation':record.get('original_invocation'),'parent_original_invocation':record['parent_original_invocation'],
            'target_commit':record['target_commit'],'controller_commit':record['controller_commit'],
            'launch_controller_commit':record['launch_controller_commit'],'manual_release':record['manual_release'],
            'phase':parent['phase'],'artifact_basename':'validation-'+operation+'-result.json'}

    def validation_seal(self,registry,key,operation,record):
        directory=self.state/'validation-results';trusted_directory(directory,mode=0o750)
        path=directory/(key+'-'+operation+'.json')
        if not path.exists():return None
        raw=read_file(path,maximum=9*1024*1024);value=self.c.strict_json(raw,max_bytes=9*1024*1024)
        fields={'schema','job_key','operation','invocation','parent_invocation','artifact_sha256','raw_hex'}
        if (not isinstance(value,dict) or set(value)!=fields or value['schema']!='eimemory.deployment_validation_seal.v1'
                or value['job_key']!=key or value['operation']!=operation or value['invocation']!=record.get('original_invocation')
                or value['parent_invocation']!=record['parent_original_invocation'] or self.c.canonical(value)!=raw):
            raise ValueError('manager_validation_root_seal_invalid')
        artifact=bytes.fromhex(value['raw_hex'])
        if len(artifact)>4*1024*1024 or sha256(artifact).hexdigest()!=value['artifact_sha256']:
            raise ValueError('manager_validation_root_seal_digest_invalid')
        return {**value,'reference':{'schema':'eimemory.deployment_validation_artifact.v1',
            'job_key':key,'operation':operation,'invocation':value['invocation'],
            'parent_invocation':value['parent_invocation'],'artifact_sha256':value['artifact_sha256'],
            'envelope_sha256':sha256(raw).hexdigest()}}

    def seal_validation(self,registry,key,operation,invocation,digest,pid,uid):
        self.c.hex_value(invocation,32,'manager_validation_invocation_invalid')
        self.c.hex_value(digest,64,'manager_validation_artifact_digest_invalid')
        path=self.validation_record_path(key,operation)
        record=self.c.strict_json(read_file(path))
        if record.get('job_key')!=key or record.get('operation')!=operation or record.get('unit')!=self.c.validation_unit(key,operation):
            raise ValueError('manager_validation_record_binding_invalid')
        process=self.proc/str(pid)
        values=dict(line.split(':',1) for line in (process/'status').read_text().splitlines() if ':' in line)
        if (uid!=registry['runtime_uid'] or values.get('Uid','').split()!=[str(uid)]*4
                or values.get('NoNewPrivs','').strip()!='1'
                or any(re.fullmatch('0+',values.get(name,'').strip()) is None for name in ('CapEff','CapPrm','CapAmb'))):
            raise ValueError('manager_validation_seal_peer_invalid')
        state=self.unit_state(record['registry'],record['unit'],['ActiveState','InvocationID','ControlGroup'])
        cg=state['ControlGroup']
        groups=(process/'cgroup').read_text().splitlines()
        if (not cg.startswith('/system.slice/') or cg.rsplit('/',1)[-1]!=record['unit']
                or not any(line.startswith('0::') and (line[3:]==cg or line[3:].startswith(cg+'/')) for line in groups)
                or state['ActiveState'] not in {'active','activating'} or state['InvocationID']!=invocation
                or record.get('original_invocation') not in {None,invocation}):
            raise ValueError('manager_validation_seal_cgroup_or_invocation_invalid')
        if record.get('original_invocation') is None:
            record['original_invocation']=invocation;atomic_json(path,record,gid=registry['runtime_gid'])
        previous=self.validation_seal(registry,key,operation,record)
        if previous is not None:
            if previous['artifact_sha256']!=digest:raise ValueError('manager_validation_seal_immutable')
            return {'job_key':key,'operation':operation,'artifact_sha256':digest,'sealed_artifact':previous['reference'],'idempotent':True}
        raw=self.validation_artifact(registry,key,operation)
        if raw is None or sha256(raw).hexdigest()!=digest:raise ValueError('manager_validation_seal_artifact_mismatch')
        value={'schema':'eimemory.deployment_validation_seal.v1','job_key':key,'operation':operation,
            'invocation':invocation,'parent_invocation':record['parent_original_invocation'],
            'artifact_sha256':digest,'raw_hex':raw.hex()}
        destination=self.state/'validation-results'/(key+'-'+operation+'.json')
        self.write_root_bytes(destination,self.c.canonical(value),registry['runtime_gid'],exclusive=True)
        sealed=self.validation_seal(registry,key,operation,record)
        return {'job_key':key,'operation':operation,'artifact_sha256':digest,'sealed_artifact':sealed['reference'],'idempotent':False}

    def validate_job(self,registry,key,operation,pid,uid):
        path=self.validation_record_path(key,operation)
        if path.exists():return self.validation_status(registry,key,operation)
        parent=self.record(key);self.verify_job_peer(registry,parent,pid,uid)
        if parent.get('superseded_by') or parent.get('finalized') is True:raise ValueError('manager_validation_parent_not_current')
        manual=parent.get('manual_release') is True
        target=CURRENT.resolve(strict=True).name if manual else parent['request']['target_commit']
        if target not in {parent['request']['base_commit'],parent['request']['candidate_commit']}:
            raise ValueError('manager_validation_target_unbound')
        if CURRENT.resolve(strict=True)!=RELEASES/target or registry['runtime_commit']!=target:
            raise ValueError('manager_validation_target_not_current')
        if operation=='post_resume':
            if parent['request']['operation']!='deploy' or parent.get('resumed') is not True or parent['phase']!='resumed_awaiting_business':
                raise ValueError('manager_validation_business_before_resume')
        else:
            if parent.get('quiesced') is not True or parent.get('resumed') is True:
                raise ValueError('manager_validation_receipt_before_quiesce')
            self.storage_guard(registry)
        pin=registry['validation_entry_digest']
        if pin is None:raise ValueError('manager_validation_entry_not_reviewed')
        controller=parent['request'].get('validation_controller_commit',parent['request']['controller_commit'])
        source=RELEASES/controller/self.c.VALIDATION_ENTRY
        if source.is_symlink() or source.resolve(strict=True)!=source:raise ValueError('manager_validation_entry_untrusted')
        if not self.runtime_compatible(registry,RELEASES/controller):raise ValueError('manager_validation_controller_incompatible')
        raw=read_file(source,uid=registry['runtime_uid'],mode=stat.S_IMODE(source.stat().st_mode),maximum=1048576)
        if sha256(raw).hexdigest()!=pin:raise ValueError('manager_validation_entry_digest_mismatch')
        record={'job_key':key,'operation':operation,'unit':self.c.validation_unit(key,operation),'registry':registry,
            'parent_original_invocation':parent['original_invocation'],'target_commit':target,
            'controller_commit':controller,'launch_controller_commit':parent['request']['controller_commit'],
            'manual_release':manual,'entry_digest':pin}
        trusted_directory(path.parent,mode=0o750)
        atomic_json(path,record,gid=registry['runtime_gid'],exclusive=True)
        self.systemctl(registry,record['unit'],'start','--no-block')
        return self.validation_status(registry,key,operation)

    def storage_guard(self,registry):
        # The original native marker reader checks current release plus the two
        # held kernel flocks. It runs as UID1001 so its own marker lock never
        # becomes a root-owned runtime DB/state file.
        path=ROOT/'storage_release_transaction.py';read_file(path,maximum=1048576)
        result=subprocess.run(['/usr/sbin/runuser','-u',registry['runtime_user'],'--','/usr/bin/python3',
            '-I','-B','-X','pycache_prefix='+self.c.EMPTY_PYCACHE,str(path),'guard',
            '--marker','/var/lib/eimemory/state/storage-release-transaction.json'],
            capture_output=True,timeout=10,env={'PATH':'/usr/bin:/bin','LC_ALL':'C','TZ':'UTC'})
        if result.returncode!=0:raise ValueError('manager_validation_storage_guard_denied')

    def verify_job_peer(self,registry,record,pid,uid):
        if uid!=registry['runtime_uid']:raise ValueError('manager_peer_uid_denied')
        status=(self.proc/str(pid)/'status').read_text()
        values=dict(line.split(':',1) for line in status.splitlines() if ':' in line)
        if values.get('NoNewPrivs','').strip()!='1':raise ValueError('manager_peer_nnp_required')
        if any(re.fullmatch('0+',values.get(name,'').strip()) is None for name in ('CapEff','CapPrm','CapAmb')):
            raise ValueError('manager_peer_capabilities_forbidden')
        realuids=values.get('Uid','').split()
        if len(realuids)!=4 or any(int(x)!=uid for x in realuids):raise ValueError('manager_peer_identity_changed')
        groups=(self.proc/str(pid)/'cgroup').read_text().splitlines()
        state=self.unit_state(record['registry'],record['unit'],['ActiveState','InvocationID','ControlGroup'])
        expected=state.get('ControlGroup','')
        if not expected.startswith('/system.slice/') or expected.rsplit('/',1)[-1]!=record['unit'] or not any(line.startswith('0::') and (line[3:]==expected or line[3:].startswith(expected+'/')) for line in groups):
            raise ValueError('manager_peer_job_cgroup_required')
        if record.get('original_invocation') not in {None,state['InvocationID']}:raise ValueError('manager_job_invocation_changed')
        self.c.hex_value(state['InvocationID'],32,'manager_job_invocation_invalid')
        if state['ActiveState'] not in {'active','activating'}:raise ValueError('manager_peer_job_not_running')
        if record.get('original_invocation') is None:
            record['original_invocation']=state['InvocationID'];self.save(registry,record)

    def fused_peer(self,registry,pid,uid):
        unit=self.c.FUSED_UNIT
        if registry['units'][unit]!={'manager':'system','installed':True}:
            raise ValueError('manager_fused_unit_not_registered')
        process=self.proc/str(pid)
        values=dict(line.split(':',1) for line in (process/'status').read_text().splitlines() if ':' in line)
        if (uid!=registry['runtime_uid'] or values.get('Uid','').split()!=[str(uid)]*4
                or values.get('NoNewPrivs','').strip()!='1'
                or any(re.fullmatch('0+',values.get(name,'').strip()) is None for name in ('CapEff','CapPrm','CapAmb'))):
            raise ValueError('manager_fused_peer_identity_invalid')
        state=self.unit_state(registry,unit,['LoadState','ActiveState','InvocationID','MainPID','ControlGroup','KillMode','SendSIGKILL'])
        cg=state['ControlGroup']
        if (state['LoadState']!='loaded' or not cg.startswith('/system.slice/') or cg.rsplit('/',1)[-1]!=unit
                or not any(line.startswith('0::') and (line[3:]==cg or line[3:].startswith(cg+'/'))
                    for line in (process/'cgroup').read_text().splitlines())
                or state['KillMode']!='control-group' or state['SendSIGKILL']!='yes'):
            raise ValueError('manager_fused_peer_cgroup_invalid')
        self.c.hex_value(state['InvocationID'],32,'manager_fused_invocation_invalid')
        return state

    def fused_record(self,key):
        self.c.hex_value(key,64,'manager_fused_key_invalid')
        path=self.state/'fused-closures'/(key+'.json');trusted_directory(path.parent,mode=0o750)
        record=self.c.strict_json(read_file(path))
        if not isinstance(record,dict):raise ValueError('manager_fused_record_invalid')
        fields={'schema','context_key','unit','invocation','runtime_commit','unit_states','active_before','phase'}
        identity={name:record.get(name) for name in ('unit','invocation','runtime_commit')}
        if (not isinstance(record,dict) or not fields<=set(record)<=fields|{'pause_failed','restore_units'}
                or record.get('context_key')!=key or self.c.digest(identity)!=key
                or record.get('schema')!='eimemory.fused_closure_context.v1'
                or record.get('phase') not in {'pause_intent','paused','restore_intent','completed'}
                or set(record.get('unit_states',{}))!=set(self.c.FUSED_L1_UNITS)
                or any(v not in {'active','activating','reloading','inactive','failed'} for v in record['unit_states'].values())
                or record.get('unit')!=self.c.FUSED_UNIT or record.get('active_before')!=[
                    unit for unit in self.c.FUSED_L1_UNITS if record.get('unit_states',{}).get(unit) in {'active','activating','reloading'}]):
            raise ValueError('manager_fused_record_invalid')
        self.c.hex_value(record['runtime_commit'],40,'manager_fused_runtime_commit_invalid')
        self.c.hex_value(record['invocation'],32,'manager_fused_invocation_invalid')
        restored=record.get('restore_units',{})
        if not isinstance(restored,dict) or not set(restored)<=set(self.c.FUSED_L1_UNITS):raise ValueError('manager_fused_restore_progress_invalid')
        for progress in restored.values():
            if (not isinstance(progress,dict) or set(progress)!={'phase','previous_invocation','invocation'}
                    or progress['phase'] not in {'dispatch_intent','restored'}):raise ValueError('manager_fused_restore_progress_invalid')
        return record

    def save_fused(self,registry,record,*,exclusive=False):
        directory=self.state/'fused-closures'
        if not directory.exists():directory.mkdir(mode=0o750);os.chown(directory,0,registry['runtime_gid'])
        trusted_directory(directory,mode=0o750)
        atomic_json(directory/(record['context_key']+'.json'),record,gid=registry['runtime_gid'],exclusive=exclusive)

    def restore_fused(self,registry,record):
        # A completed oneshot must not be started twice after a daemon crash.
        # Persist each submission before calling PID1 and retain ambiguity.
        if CURRENT.resolve(strict=True)!=RELEASES/record['runtime_commit'] or registry['runtime_commit']!=record['runtime_commit']:
            raise ValueError('manager_fused_restore_release_changed')
        active=self.state/'active-transition.json'
        pointer=self.c.strict_json(read_file(active))
        if pointer.get('context')!='fused_closure' or pointer.get('job_key')!=record['context_key']:
            raise ValueError('manager_fused_restore_context_changed')
        record['phase']='restore_intent';self.save_fused(registry,record)
        progress=record.setdefault('restore_units',{})
        for unit in reversed(self.c.FUSED_L1_UNITS):
            if unit not in record['active_before']:continue
            state=self.unit_state(registry,unit,['LoadState','ActiveState','Result','InvocationID','MainPID','ExecMainExitTimestampMonotonic'])
            saved=progress.get(unit)
            if saved is not None and saved['phase']=='restored':continue
            if saved is not None:
                fresh=(state['InvocationID'] and state['InvocationID']!=saved['previous_invocation'])
                ended=(str(state['ExecMainExitTimestampMonotonic']).isdigit() and int(state['ExecMainExitTimestampMonotonic'])>0
                    and state['MainPID']=='0' and state['Result']=='success')
                if state['LoadState']=='loaded' and fresh and (state['ActiveState'] in {'active','activating','reloading'} or ended):
                    saved.update(phase='restored',invocation=state['InvocationID']);self.save_fused(registry,record);continue
                raise ValueError('manager_fused_restore_submission_unknown')
            if state['LoadState']!='loaded':raise ValueError('manager_fused_restore_unit_unknown')
            if state['ActiveState'] in {'active','activating','reloading'}:
                progress[unit]={'phase':'restored','previous_invocation':state['InvocationID'],'invocation':state['InvocationID']}
                self.save_fused(registry,record);continue
            progress[unit]={'phase':'dispatch_intent','previous_invocation':state['InvocationID'],'invocation':None}
            self.save_fused(registry,record)
            self.systemctl(registry,unit,'start',*(['--no-block'] if unit.endswith('.service') else []))
            state=self.unit_state(registry,unit,['LoadState','ActiveState','Result','InvocationID','MainPID','ExecMainExitTimestampMonotonic'])
            completed=(unit.endswith('.service') and state['ActiveState']=='inactive' and state['Result']=='success'
                and state['InvocationID'] and state['InvocationID']!=progress[unit]['previous_invocation']
                and state['MainPID']=='0' and str(state['ExecMainExitTimestampMonotonic']).isdigit()
                and int(state['ExecMainExitTimestampMonotonic'])>0)
            if (state['LoadState']!='loaded' or state['ActiveState'] not in {'active','activating','reloading'}
                    and not completed):
                raise ValueError('manager_fused_restore_not_observed')
            progress[unit].update(phase='restored',invocation=state['InvocationID']);self.save_fused(registry,record)
        record['phase']='completed';self.save_fused(registry,record)
        atomic_json(active,{'context':'fused_closure','job_key':record['context_key'],'complete':True},gid=registry['runtime_gid'])
        return {'context_key':record['context_key'],'restored':True,'phase':'completed','active_before':record['active_before']}

    def fused_closure(self,registry,action,pid,uid,key=None):
        state=self.fused_peer(registry,pid,uid);active=self.state/'active-transition.json'
        pointer=self.c.strict_json(read_file(active)) if active.exists() else None
        existing=None
        if pointer and pointer.get('context')=='fused_closure':existing=self.fused_record(pointer.get('job_key'))
        if action=='recover':
            if existing is None or existing['phase']=='completed':return {'restored':True,'phase':'no_pending_context'}
            # Only the fixed service's pre-start/post-stop hooks run with no
            # MainPID. A different CLI, or its still-running main, cannot end a
            # context on behalf of the service. New invocation recovery covers
            # a prior boot without rewriting that invocation's original record.
            if state['ActiveState'] not in {'activating','deactivating','inactive','failed'} or state['MainPID']!='0':
                raise ValueError('manager_fused_recovery_requires_native_hook')
            return self.restore_fused(registry,existing)
        if state['ActiveState'] not in {'active','activating'}:raise ValueError('manager_fused_service_not_running')
        if action=='end':
            record=self.fused_record(key)
            if record['invocation']!=state['InvocationID']:raise ValueError('manager_fused_original_invocation_mismatch')
            if record['phase']=='completed':return {'context_key':key,'restored':True,'phase':'completed','idempotent':True}
            return self.restore_fused(registry,record)
        if pointer and pointer.get('complete') is not True:
            if existing is None:raise ValueError('manager_another_transition_active')
            if existing['invocation']!=state['InvocationID']:raise ValueError('manager_fused_prior_context_requires_recovery')
            record=existing
        else:
            if pointer and pointer.get('held') is True:raise ValueError('manager_held_runtime_requires_manual_upgrade')
            if CURRENT.resolve(strict=True)!=RELEASES/registry['runtime_commit']:raise ValueError('manager_fused_current_release_changed')
            states={}
            for unit in self.c.FUSED_L1_UNITS:
                if registry['units'][unit]!={'manager':'system','installed':True}:raise ValueError('manager_fused_l1_unit_not_registered')
                value=self.unit_state(registry,unit,['LoadState','ActiveState'])
                if value['LoadState']!='loaded' or value['ActiveState'] not in {'active','activating','reloading','inactive','failed'}:
                    raise ValueError('manager_fused_l1_state_unknown')
                states[unit]=value['ActiveState']
            identity={'unit':self.c.FUSED_UNIT,'invocation':state['InvocationID'],'runtime_commit':registry['runtime_commit']}
            key=self.c.digest(identity)
            record={'schema':'eimemory.fused_closure_context.v1','context_key':key,**identity,
                'unit_states':states,'active_before':[u for u in self.c.FUSED_L1_UNITS if states[u] in {'active','activating','reloading'}],'phase':'pause_intent'}
            self.save_fused(registry,record,exclusive=True)
            atomic_json(active,{'context':'fused_closure','job_key':key,'complete':False},gid=registry['runtime_gid'])
        if record['phase']=='paused':return {'context_key':record['context_key'],'phase':'paused','active_before':record['active_before'],'idempotent':True}
        try:
            for unit in self.c.FUSED_L1_UNITS:
                if unit in record['active_before']:self.systemctl(registry,unit,'stop')
                value=self.unit_state(registry,unit,['ActiveState'])
                if value['ActiveState'] not in {'inactive','failed'}:raise ValueError('manager_fused_l1_not_quiesced')
        except Exception:
            record['pause_failed']=True;self.save_fused(registry,record)
            self.restore_fused(registry,record)
            raise
        record['phase']='paused';self.save_fused(registry,record)
        return {'context_key':record['context_key'],'phase':'paused','active_before':record['active_before']}

    def writers(self,registry):
        excluded={'eimemory-console.service'}
        return [u for u,v in registry['units'].items() if v['installed'] and u not in excluded]

    def transition(self,registry,key,phase,pid,uid):
        record=self.record(key);self.verify_job_peer(registry,record,pid,uid)
        if phase not in {'quiesce','publish','activate','resume_watchers','resume','rollback'}:raise ValueError('manager_transition_phase_invalid')
        if record.get('resumed') is True:
            if phase not in {'resume','resume_watchers'}:raise ValueError('manager_transition_already_resumed')
            return {'job_key':key,'phase':record['phase'],'resumed':True,'idempotent':True}
        if record.get('finalized') is True:raise ValueError('manager_transition_already_complete')
        if phase=='quiesce':
            current=CURRENT.resolve(strict=True)
            if current.parent!=RELEASES or current.name not in {record['request']['base_commit'],record['request']['candidate_commit']}:
                raise ValueError('manager_quiesce_current_unbound')
            active_path=self.state/'active-transition.json'
            if active_path.exists():
                existing=self.c.strict_json(read_file(active_path))
                if existing.get('job_key')!=key and existing.get('complete') is not True:
                    if existing.get('context')=='fused_closure':raise ValueError('manager_another_transition_active')
                    prior=self.record(existing.get('job_key',''))
                    before=prior['request'];after=record['request']
                    same_rollback=(before.get('operation')=='deploy' and after.get('operation')=='rollback'
                        and set(before)==set(after)==self.c.JOB_FIELDS
                        and all(before[k]==after[k] for k in self.c.JOB_FIELDS-{'operation','target_commit'}))
                    reviewed_retry=(record.get('manual_release') is True and prior.get('manual_release') is True
                        and record.get('approved_retry_of')==prior['job_key']
                        and all(before.get(k)==after.get(k) for k in ('base_commit','candidate_commit','target_commit','controller_commit')))
                    inherited=((same_rollback or reviewed_retry) and prior.get('finalized') is not True
                        and (prior.get('active_before') is not None or (reviewed_retry and prior.get('phase')=='submitted')))
                    if not inherited:raise ValueError('manager_another_transition_active')
                    # A previous validation process is a dynamic SQLite writer,
                    # even when its parent failed or timed out. Fence its exact
                    # invocation before a new rollback can restore a snapshot.
                    self.fence_job_processes(registry,prior)
                    if prior.get('active_before') is not None:
                        record['active_before']=list(prior['active_before']);record['inherited_capture_from']=prior['job_key']
                    prior['superseded_by']=key;self.save(registry,prior)
                elif existing.get('job_key')!=key and existing.get('held') is True:
                    # A legacy rollback retained stopped writers. Only an
                    # explicit root-reviewed manual upgrade may recover them;
                    # a new automatic repair must not inherit stale authority.
                    if record.get('manual_release') is not True:
                        raise ValueError('manager_held_runtime_requires_manual_upgrade')
                    prior=self.record(existing.get('job_key',''))
                    if prior.get('phase')!='completed_held' or prior.get('finalized') is not True or not isinstance(prior.get('active_before'),list):
                        raise ValueError('manager_held_restore_capture_invalid')
                    for unit in prior['active_before']:
                        if unit not in record['registry']['units']:raise ValueError('manager_held_restore_unit_unknown')
                    record['active_before']=list(prior['active_before'])
                    record['inherited_held_capture_from']=prior['job_key']
            atomic_json(active_path,{'job_key':key,'complete':False},gid=registry['runtime_gid'])
            if record['resumed']:raise ValueError('manager_transition_already_resumed')
            self.fence_job_processes(registry,record,include_parent=False)
            if record['active_before'] is None:
                active=[]
                for unit in self.writers(record['registry']):
                    state=self.unit_state(record['registry'],unit,['LoadState','ActiveState'])
                    if state['LoadState'] not in {'loaded','not-found'}:raise ValueError('manager_writer_load_unknown')
                    if state['ActiveState'] in {'active','activating','reloading','deactivating'}:active.append(unit)
                    elif state['ActiveState'] not in {'inactive','failed'}:raise ValueError('manager_writer_state_unknown')
                migration=self.state/'migration-prior-active.json'
                if record.get('manual_release') is True and migration.exists():
                    original=self.c.strict_json(read_file(migration))
                    if original.get('restored') is not True:
                        for unit in original['active_before']:
                            if unit not in record['registry']['units']:raise ValueError('manager_migration_unit_unknown')
                            if unit not in active:active.append(unit)
                record['active_before']=active;record['phase']='quiesce_intent';self.save(registry,record)
            record['phase']='quiesce_intent';self.save(registry,record)
            # Timers first; RPC/gateways last. A partial failure preserves the
            # original set and never resets it from the now-stopped inventory.
            ordered=sorted(record['active_before'],key=lambda x:(x.endswith('.service'),x in {'eimemory-rpc.service','hermes-gateway.service','openclaw-gateway.service'},x))
            for unit in ordered:self.systemctl(record['registry'],unit,'stop')
            for unit in self.writers(record['registry']):
                state=self.unit_state(record['registry'],unit,['ActiveState'])
                if state['ActiveState'] not in {'inactive','failed'}:raise ValueError('manager_writer_still_running')
            record['quiesced']=True
        elif phase in {'publish','activate','rollback'}:
            if not record['quiesced'] or record['resumed']:raise ValueError('manager_transition_not_quiesced')
            target=record['request']['target_commit'] if phase!='rollback' else record['request']['base_commit']
            self.publish_runtime(registry,record,target,activate=phase in {'activate','rollback'})
            if phase in {'activate','rollback'}:
                for unit in ('eimemory-rpc.service','hermes-gateway.service','openclaw-gateway.service'):
                    if record['registry']['units'][unit]['installed'] and (unit=='eimemory-rpc.service' or unit in record['active_before']):
                        self.systemctl(record['registry'],unit,'restart')
        elif phase=='resume_watchers':
            if not record['quiesced']:raise ValueError('manager_transition_not_quiesced')
            if record['registry']['units']['eimemory-release-closure.path']['installed']:
                self.systemctl(record['registry'],'eimemory-release-closure.path','start')
        else:
            if record['active_before'] is None:raise ValueError('manager_restore_set_missing')
            record['resume_requested']=True;record['phase']='resume_deferred_until_terminal';self.save(registry,record)
            return {'job_key':key,'phase':record['phase'],'resumed':False,'deferred':True}
        record['phase']=phase;self.save(registry,record)
        return {'job_key':key,'phase':phase,'active_before':record['active_before'],'resumed':record['resumed']}

    def fence_job_processes(self,registry,prior,*,include_parent=True):
        key=prior['job_key'];fences=prior.setdefault('writer_fences',{})
        units=[(operation,self.c.validation_unit(key,operation)) for operation in self.c.VALIDATION_PREFIXES
            if self.validation_record_path(key,operation).exists()]
        if include_parent:units.append(('parent',prior['unit']))
        for operation,unit in units:
            path=self.record_path(key) if operation=='parent' else self.validation_record_path(key,operation)
            bound=prior if operation=='parent' else self.c.strict_json(read_file(path))
            state=self.unit_state(bound['registry'],unit,list(self.c.NATIVE_EXIT_PROPERTIES))
            actual=state.get('InvocationID');original=bound.get('original_invocation')
            if actual and original not in {None,actual}:raise ValueError('manager_predecessor_invocation_changed')
            if original is None and self.c.native_terminal_exit(state,None,unit):
                bound['original_invocation']=actual;original=actual
            if bound.get('terminal_state') is not None or self.c.native_terminal_exit(state,original,unit):
                if self.pin_terminal(bound,state,'predecessor'):
                    if operation=='parent':self.save(registry,bound)
                    else:atomic_json(path,bound,gid=registry['runtime_gid'])
            if state['ActiveState'] in {'active','activating','reloading','deactivating'}:
                retained=self.c.native_terminal_exit(state,original,unit)
                cg=state['ControlGroup'];self.c.hex_value(actual,32,'manager_predecessor_invocation_unknown')
                if not retained and not self.c.fixed_system_unit_cgroup(cg,unit):
                    raise ValueError('manager_predecessor_cgroup_invalid')
                if original is None:
                    bound['original_invocation']=actual
                    if operation=='parent':self.save(registry,bound)
                    else:atomic_json(path,bound,gid=registry['runtime_gid'])
                fences[operation]={'unit':unit,'invocation':actual,'before':state,'phase':'stop_intent'}
                self.save(registry,prior)
                self.systemctl(bound['registry'],unit,'stop')
                after=self.unit_state(bound['registry'],unit,list(self.c.NATIVE_EXIT_PROPERTIES))
                if after.get('InvocationID') not in {'',actual} or after.get('MainPID') not in {'','0'}:
                    raise ValueError('manager_predecessor_stop_identity_changed')
                state=after
            if state['LoadState'] not in {'loaded','not-found'} or state['ActiveState'] not in {'inactive','failed'} or state['MainPID'] not in {'0',''}:
                raise ValueError('manager_predecessor_writer_not_fenced')
            fences[operation]={**fences.get(operation,{}),'unit':unit,'invocation':actual or original,'after':state,'phase':'inactive_verified'}
            self.save(registry,prior)

    def runtime_compatible(self,registry,target):
        for path,pin in registry['compatibility_hashes'].items():
            source=target/path
            if source.is_symlink() or not source.is_file() or source.resolve(strict=True)!=source:return False
            if sha256(read_file(source,uid=registry['runtime_uid'],mode=stat.S_IMODE(source.stat().st_mode),maximum=4*1024*1024)).hexdigest()!=pin:
                return False
        return True

    def recover_release_observers(self,registry,record):
        if record.get('manual_release') is not True:return
        # A failed prior nightly cannot be required to have already published
        # the new release's first lineage. Start real observations after the
        # storage commit/resume, retaining the failed invocations as evidence.
        fixed=(('eimemory-code-implementation-refresh.service',True),
            ('eimemory-l5-effect-review.service',False),('eimemory-nightly.service',False),
            ('eimemory-timer-monitor.service',True))
        for unit,refresh in fixed:
            timer=unit.replace('.service','.timer')
            if timer not in record['active_before'] or not registry['units'][unit]['installed']:continue
            before=self.unit_state(registry,unit,list(self.c.NATIVE_EXIT_PROPERTIES))
            if before['ActiveState'] not in {'inactive','failed'}:continue
            failed=before['Result'] in {'exit-code','signal','timeout','core-dump','watchdog'}
            if not failed and not refresh:continue
            if (before['MainPID']!='0' or before['ControlGroup']!=''
                    or failed and not self.c.native_terminal_exit(before,before['InvocationID'],unit)):
                raise ValueError('manager_observer_recovery_not_terminal')
            attempts=record.setdefault('observer_recovery',{})
            if unit in attempts:continue
            attempts[unit]={'before':before,'action':'start_submitted','execution_verified':False}
            self.save(registry,record)
            self.systemctl(registry,unit,'start','--no-block')

    def finalize(self,registry,key,pid,uid):
        record=self.record(key);self.verify_job_peer(registry,record,pid,uid)
        if record.get('resumed') is True:
            return {'job_key':key,'resumed':True,'phase':record['phase'],'idempotent':True}
        if record.get('superseded_by'):raise ValueError('manager_job_superseded')
        if not record.get('quiesced') or not record.get('resume_requested'):
            raise ValueError('manager_finalize_without_restore_intent')
        target=CURRENT.resolve(strict=True)
        if target.parent!=RELEASES or target.name not in {record['request']['base_commit'],record['request']['candidate_commit']}:
            raise ValueError('manager_finalize_current_unbound')
        compatible=self.runtime_compatible(registry,target)
        if not compatible:
            record['held_background']=[unit for unit in record['active_before'] if unit not in {'eimemory-rpc.service','hermes-gateway.service','openclaw-gateway.service'}];record['phase']='held_incompatible_runtime'
            self.save(registry,record)
            return {'job_key':key,'resumed':False,'held_background':record['held_background'],
                'serving_commit':target.name,'controller_commit':registry['controller_commit'],'reason':'manager_legacy_runtime_requires_reviewed_controller_upgrade'}
        self.resume(registry,record);record['resumed']=True;record['phase']='resumed_awaiting_business'
        self.save(registry,record)
        self.recover_release_observers(registry,record)
        return {'job_key':key,'resumed':True,'phase':record['phase'],'serving_commit':target.name,'controller_commit':registry['controller_commit']}

    def cleanup_validation_units(self,registry,key):
        for operation in self.c.VALIDATION_PREFIXES:
            path=self.validation_record_path(key,operation)
            if not path.exists() and not path.is_symlink():continue
            value=self.validation_status(registry,key,operation)
            terminal=value.get('terminal_state')
            if terminal is None:raise ValueError('manager_validation_completion_unproven')
            state=value['state']
            # Stop only a retained, already proven successful exit. A failed or
            # unknown invocation is preserved, never reset to appear healthy.
            if state.get('ActiveState')=='active' and state.get('SubState')=='exited':
                if terminal.get('Result')!='success' or terminal.get('MainPID')!='0':
                    raise ValueError('manager_validation_cleanup_not_terminal')
                self.systemctl(registry,value['unit'],'stop','--no-block')

    def verify_completed_validation(self,registry,record,operation,target_commit):
        key=record['job_key']
        proof=self.validation_status(registry,key,operation);terminal=proof.get('terminal_state')
        if (terminal is None or terminal.get('LoadState')!='loaded'
                or terminal.get('InvocationID')!=proof.get('original_invocation')
                or terminal.get('Result')!='success' or terminal.get('ExecMainStatus')!='0'
                or not proof.get('sealed_artifact') or proof['target_commit']!=target_commit
                or proof['parent_original_invocation']!=record.get('original_invocation')):
            raise ValueError('manager_complete_validation_unproven')
        validation_record=self.c.strict_json(read_file(self.validation_record_path(key,operation)))
        seal=self.validation_seal(registry,key,operation,validation_record)
        raw=bytes.fromhex(seal['raw_hex']);artifact=self.c.strict_json(raw,max_bytes=4*1024*1024)
        expected={'schema':'managed_code_deployment_validation.v2','job_key':key,'operation':operation,
            'parent_invocation':record['original_invocation'],'invocation':proof['original_invocation'],
            'target_commit':target_commit,'controller_commit':record['request'].get('validation_controller_commit',record['request']['controller_commit']),
            'scope':record['request']['scope'],'source_readonly':True,
            'manual_release':record.get('manual_release') is True,'launch_controller_commit':record['request']['controller_commit']}
        if (not isinstance(artifact,dict) or set(artifact)!=set(expected)|{'report','target_transport'}
                or any(artifact.get(name)!=value for name,value in expected.items())
                or not isinstance(artifact.get('report'),dict) or artifact['report'].get('ok') is not True
                or self.c.canonical(artifact)!=raw):
            raise ValueError('manager_complete_sealed_context_invalid')
        transport=artifact['target_transport'];report=artifact['report']
        fields={'exit_status','timed_out','stdout_digest','stderr_digest','executed','physical_receipt_readback','closure_capture'}
        if (not isinstance(transport,dict) or set(transport)!=fields or transport.get('executed') is not True
                or type(transport.get('exit_status')) is not int or transport['exit_status']!=0 or transport.get('timed_out') is not False):
            raise ValueError('manager_complete_target_execution_unproven')
        for name in ('stdout_digest','stderr_digest'):self.c.hex_value(transport[name],64,'manager_complete_target_digest_invalid')
        if operation=='receipt':
            receipt=report.get('promotion_request_id')
            if (report.get('report_type')!='deployment_receipt' or report.get('commit')!=target_commit
                    or not isinstance(receipt,str) or not receipt or len(receipt)>256
                    or transport['physical_receipt_readback']!={'current_deployment_receipt_verified':True,'commit':target_commit,'receipt_id':receipt}):
                raise ValueError('manager_complete_receipt_readback_invalid')
        else:
            capture=transport['closure_capture']
            if (not isinstance(capture,dict) or capture.get('capture_saved') is not True or capture.get('recording_ok') is not True
                    or type(capture.get('exit_code')) is not int or capture['exit_code']!=0
                    or not isinstance(capture.get('capture'),dict) or capture['capture'].get('scope')!=record['request']['scope']
                    or capture['capture'].get('expected_commit')!=target_commit
                    or report.get('observation_started') is not False):
                raise ValueError('manager_complete_closure_capture_invalid')
            if operation=='pre_observation':
                if (report.get('report_type')!='code_evolution_pre_observation' or report.get('schema_version')!='1'
                        or report.get('status')!='ready_for_observation' or report.get('closure_complete') is not False):
                    raise ValueError('manager_complete_pre_observation_invalid')
            else:
                if (report.get('report_type')!='controlled_production_release_closure' or report.get('business_qualified') is not True
                        or report.get('closure_complete') is not True or report.get('l5_certification')!='not_certified'):
                    raise ValueError('manager_complete_business_unqualified')
                receipt_path=self.validation_record_path(key,'receipt')
                receipt_record=self.c.strict_json(read_file(receipt_path))
                receipt_seal=self.validation_seal(registry,key,'receipt',receipt_record)
                receipt_artifact=self.c.strict_json(bytes.fromhex(receipt_seal['raw_hex']),max_bytes=4*1024*1024)
                if (not isinstance(report.get('record_ids'),dict)
                        or report['record_ids'].get('deployment_receipt')!=receipt_artifact['report'].get('promotion_request_id')):
                    raise ValueError('manager_complete_business_receipt_mismatch')

    def complete(self,registry,key,pid,uid):
        record=self.record(key);self.verify_job_peer(registry,record,pid,uid)
        if record.get('superseded_by'):raise ValueError('manager_job_superseded')
        if record.get('finalized') is True:
            self.cleanup_validation_units(registry,key)
            return {'job_key':key,'complete':True,'resumed':record['resumed'],'phase':record['phase'],'operational_complete':record['phase']=='completed','held_background':record.get('held_background',[]),'idempotent':True}
        held=record.get('phase')=='held_incompatible_runtime'
        if not record.get('resumed') and not held:raise ValueError('manager_complete_before_resume')
        target=CURRENT.resolve(strict=True)
        if target.parent!=RELEASES or registry['runtime_commit']!=target.name:
            raise ValueError('manager_complete_current_unbound')
        operations=['receipt']
        if record['request']['operation']=='deploy':
            operations.append('post_resume')
            if record.get('manual_release') is not True:operations.append('pre_observation')
        for operation in operations:self.verify_completed_validation(registry,record,operation,target.name)
        self.cleanup_validation_units(registry,key)
        record['finalized']=True;record['phase']='completed_held' if held else 'completed'
        self.save(registry,record)
        migration=self.state/'migration-prior-active.json'
        if not held and record.get('manual_release') is True and migration.exists():
            original=self.c.strict_json(read_file(migration));original['restored']=True
            atomic_json(migration,original,gid=registry['runtime_gid'])
        atomic_json(self.state/'active-transition.json',{'job_key':key,'complete':True,'held':held},gid=registry['runtime_gid'])
        return {'job_key':key,'complete':True,'resumed':record['resumed'],'phase':record['phase'],
            'held_background':record.get('held_background',[]),'operational_complete':not held}

    def publish_runtime(self,registry,record,target,*,activate):
        # Only a reviewed immutable root template is rendered. Candidate unit
        # source is never read/imported by root. Root reviews template upgrades
        # through bootstrap independently of autonomous code changes.
        self.c.hex_value(target,40,'manager_target_invalid')
        if target not in {record['request']['base_commit'],record['request']['candidate_commit']}:
            raise ValueError('manager_target_not_authorized')
        release=RELEASES/target
        if release.is_symlink() or not release.is_dir() or release.resolve(strict=True)!=release:raise ValueError('manager_target_release_missing')
        if activate and CURRENT.resolve(strict=True)!=release:
            raise ValueError('manager_current_target_mismatch')
        for unit,pin in registry['template_hashes'].items():
            source=ROOT/'templates'/unit
            raw=read_file(source)
            if sha256(raw).hexdigest()!=pin:raise ValueError('manager_template_digest_mismatch')
            if unit.endswith('.service'):
                destination=Path('/etc/systemd/system')/(unit+'.d')
                trusted_directory(destination,mode=0o750)
                # Root-reviewed policy is separate from the static Exec* unit.
                if unit=='eimemory-code-deployment-job@.service':
                    controller=target if record.get('manual_release') is True and target==record['request']['candidate_commit'] else registry['controller_commit']
                    policy=read_file(ROOT/'job-runtime-policy.conf').replace(b'@COMMIT@',controller.encode())
                elif unit.startswith('eimemory-code-validation-'):
                    policy=read_file(ROOT/'validation-runtime-policy.conf')
                else:
                    policy=read_file(ROOT/'runtime-policy.conf').replace(b'@COMMIT@',target.encode())
                if unit in {'eimemory-rpc.service','eimemory-console.service'}:
                    policy=policy.replace(('/opt/eimemory/releases/'+target+' ').encode(),b'/opt/eimemory/releases ')
                self.write_root_bytes(destination/'90-eimemory-runtime.conf',policy,registry['runtime_gid'])
        self.command(['/usr/sbin/runuser','-u',registry['runtime_user'],'--','/usr/bin/env',
            'XDG_RUNTIME_DIR=/run/user/'+str(registry['runtime_uid']),
            'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(registry['runtime_uid'])+'/bus',
            '/usr/bin/python3','-I','-B','-X','pycache_prefix='+self.c.EMPTY_PYCACHE,str(ROOT/'runtime_manager_user_policy.py'),target])
        self.command(['/usr/bin/systemctl','daemon-reload'])
        if activate:
            fresh=dict(registry);fresh['runtime_commit']=target
            if record.get('manual_release') is True and target==record['request']['candidate_commit']:
                self.verify_controller_entry(registry,target)
                # Do not resume a legacy controller lacking the worker contract.
                for path,pin in registry['compatibility_hashes'].items():
                    source=release/path
                    if source.is_symlink() or not source.is_file() or source.resolve(strict=True)!=source or sha256(read_file(source,uid=registry['runtime_uid'],mode=stat.S_IMODE(source.stat().st_mode),maximum=4*1024*1024)).hexdigest()!=pin:
                        raise ValueError('manager_manual_controller_compatibility_mismatch')
                fresh['controller_commit']=target
            fresh['generation']=self.c.digest({k:v for k,v in fresh.items() if k!='generation'})
            atomic_json(self.registry_path,fresh,gid=registry['runtime_gid'])

    @staticmethod
    def write_root_bytes(path,raw,gid,*,exclusive=False):
        temporary=path.with_name(path.name+'.tmp.'+str(os.getpid()))
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o640)
        try:
            os.fchown(fd,0,gid)
            with os.fdopen(fd,'wb',closefd=False) as stream:
                stream.write(raw);stream.flush();os.fsync(fd)
            if path.exists():read_file(path)
            if exclusive:os.link(temporary,path,follow_symlinks=False)
            else:os.replace(temporary,path)
            directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:os.fsync(directory)
            finally:os.close(directory)
        finally:
            os.close(fd)
            if temporary.exists():temporary.unlink()

    def resume(self,registry,record):
        active=record['active_before']
        if active is None:raise ValueError('manager_restore_set_missing')
        core={'eimemory-rpc.service','hermes-gateway.service','openclaw-gateway.service'}
        for unit in sorted(active,key=lambda x:(x not in core,x.endswith('.timer'),x)):
            args=['start']
            if unit.endswith('.service') and unit not in core:args+=['--no-block']
            self.systemctl(record['registry'],unit,*args)

    def runtime_process_environment(self,registry,unit):
        # Only the runtime UID may observe its own registered runtime processes.
        # Never accept an arbitrary PID, root unit, path or environment override.
        allowed={'eimemory-rpc.service','hermes-gateway.service','hermes-gateway-hongtai.service',
                 'hermes-gateway-hongxin.service','hermes-gateway-xiaomage.service'}
        if unit not in allowed:raise ValueError('manager_process_unit_not_allowlisted')
        properties=['Id','LoadState','ActiveState','MainPID','InvocationID','ControlGroup']
        before=self.unit_state(registry,unit,properties)
        if (before['Id']!=unit or before['LoadState']!='loaded' or before['ActiveState']!='active'
                or not before['MainPID'].isdigit() or int(before['MainPID'])<=0 or not before['ControlGroup']):
            raise ValueError('manager_process_unit_not_running')
        self.c.hex_value(before['InvocationID'],32,'manager_process_invocation_invalid')
        process=self.proc/before['MainPID']
        status=dict(line.split(':',1) for line in (process/'status').read_text().splitlines() if ':' in line)
        if (process.stat().st_uid!=registry['runtime_uid']
                or status.get('Uid','').split()!=[str(registry['runtime_uid'])]*4):
            raise ValueError('manager_process_owner_mismatch')
        groups=(process/'cgroup').read_text().splitlines();group=before['ControlGroup']
        if not any(row.startswith('0::') and (row[3:]==group or row[3:].startswith(group+'/')) for row in groups):
            raise ValueError('manager_process_cgroup_mismatch')
        def ticks():return int((process/'stat').read_text().rsplit(')',1)[1].split()[19])
        start=ticks();raw=(process/'environ').read_bytes()
        if len(raw)>1048576:raise ValueError('manager_process_environment_limit')
        entries=[item.split('=',1) for item in raw.decode().split('\0') if '=' in item]
        if len(entries)!=len({key for key,_ in entries}):raise ValueError('manager_process_environment_duplicate')
        environment=dict(entries)
        if unit=='eimemory-rpc.service':
            token=sha256(read_file(ROOT/'runtime-settings.v1.json')+read_file(ROOT/'runtime_manager_launch.py')).hexdigest()
            if environment.get('EIMEMORY_RUNTIME_SETTINGS_APPLIED')!=token:
                raise ValueError('manager_process_settings_exec_unverified')
        after=self.unit_state(registry,unit,properties)
        if after!=before or ticks()!=start:raise ValueError('manager_process_identity_changed')
        return {'schema':'eimemory.runtime_process_environment.v1','unit':unit,'pid':int(before['MainPID']),
            'start_ticks':start,'invocation_id':before['InvocationID'],'environment':environment}

    def dispatch(self,value,*,pid,uid):
        registry=self.registry()
        if uid!=registry['runtime_uid']:raise ValueError('manager_peer_uid_denied')
        if not isinstance(value,dict) or value.get('schema')!=self.c.REQUEST_SCHEMA or value.get('registry_generation')!=registry['generation']:
            raise ValueError('manager_request_registry_mismatch')
        op=value.get('operation');base={'schema','operation','registry_generation'}
        fields={'unit_state':{'unit','properties'},'unit_journal':{'unit','invocation','limit'},
            'runtime_process_environment':{'unit'},
            'begin_fused_closure':set(),'end_fused_closure':{'context_key'},'recover_fused_closure':set(),
            'issue_managed_incident_proof':{'source_record_id','source_digest'},'verify_managed_incident_proof':{'receipt_id','digest'},'checkpoint_managed_incident_knowledge':set(),
            'runtime_identity':{'unit'},'activation_attestation':set(),'verify_activation_attestation':{'receipt_id','digest'},'error_window':set(),'single_rpc_owner':set(),'readonly_probe':{'job_key'},'submit_job':{'job_key'},'job_status':{'job_key'},'release_job':{'job_key'},
            'job_journal':{'job_key','invocation'},'validate_deployment_job':{'job_key','validation_operation'},'seal_deployment_validation_result':{'job_key','validation_operation','invocation','artifact_digest'},'deployment_validation_status':{'job_key','validation_operation'},'finalize_job':{'job_key'},'complete_job':{'job_key'},'transition':{'job_key','phase'}}
        if op not in fields or set(value)!=base|fields[op]:raise ValueError('manager_request_shape_invalid')
        if op=='issue_managed_incident_proof':
            return {'reference':self.managed_authority(registry).issue(value['source_record_id'],value['source_digest'],
                verified_activation_receipt=self.verify_activation,registry=registry,peer={'pid':pid,'uid':uid})}
        if op=='verify_managed_incident_proof':
            return {'proof':self.managed_authority(registry).verify(value['receipt_id'],value['digest'],registry=registry)}
        if op=='checkpoint_managed_incident_knowledge':
            return {'checkpoint':self.managed_authority(registry).checkpoint(registry=registry,peer={'pid':pid,'uid':uid})}
        if op=='activation_attestation':return {'reference':self.activation_attestation(registry,pid,uid)}
        if op=='verify_activation_attestation':return {'attestation':self.verify_activation(value['receipt_id'],value['digest'])}
        if op=='error_window':return {'report':self.error_window(registry)}
        if op=='single_rpc_owner':
            state=self.unit_state(registry,'eimemory-rpc.service',['ActiveState','UnitFileState'])
            raw=self.command(['/usr/sbin/runuser','-u',registry['runtime_user'],'--','/usr/bin/env',
                'XDG_RUNTIME_DIR=/run/user/'+str(registry['runtime_uid']),
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(registry['runtime_uid'])+'/bus',
                '/usr/bin/systemctl','--user','show','eimemory-rpc.service','--property=ActiveState,UnitFileState'])
            old=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
            if state['ActiveState']!='active' or old.get('ActiveState') not in {'inactive','failed'} or old.get('UnitFileState') not in {'disabled','masked','not-found',''}:
                raise ValueError('manager_rpc_owner_not_unique')
            return {'manager':'system','unit':'eimemory-rpc.service','single_owner':True}
        if op=='unit_state':return {'state':self.unit_state(registry,value['unit'],value['properties'])}
        if op=='unit_journal':return self.journal(registry,value['unit'],value['invocation'],value['limit'])
        if op=='runtime_identity':
            unit=value['unit']
            # Do not return Environment: it contains credentials. The one public
            # identity assignment is parsed in root and validated independently.
            raw=self.systemctl(registry,unit,'show','--property=Environment','--property=LoadState')
            state=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
            if state.get('LoadState')!='loaded':raise ValueError('manager_identity_unit_not_loaded')
            values=[x.split('=',1)[1] for x in shlex.split(state.get('Environment','')) if x.startswith('EIMEMORY_RUNTIME_COMMIT=')]
            if len(values)!=1:raise ValueError('manager_identity_not_unique')
            self.c.hex_value(values[0],40,'manager_identity_commit_invalid')
            return {'runtime_commit':values[0]}
        # Serialize durable transitions. Reads never cause a lifecycle action.
        if op=='runtime_process_environment':
            return {'observation':self.runtime_process_environment(registry,value['unit'])}
        with self.mutex,self.lifecycle_lock():
            # A different thread may have activated a release while this caller
            # waited for the lifecycle lock. Never mutate using stale authority.
            if self.registry()['generation']!=registry['generation']:
                raise ValueError('manager_request_registry_mismatch')
            if op=='begin_fused_closure':return self.fused_closure(registry,'begin',pid,uid)
            if op=='end_fused_closure':return self.fused_closure(registry,'end',pid,uid,value['context_key'])
            if op=='recover_fused_closure':return self.fused_closure(registry,'recover',pid,uid)
            if op=='seal_deployment_validation_result':return {'validation':self.seal_validation(registry,value['job_key'],value['validation_operation'],value['invocation'],value['artifact_digest'],pid,uid)}
            if op=='validate_deployment_job':return {'validation':self.validate_job(registry,value['job_key'],value['validation_operation'],pid,uid)}
            if op=='deployment_validation_status':return {'validation':self.validation_status(registry,value['job_key'],value['validation_operation'])}
            if op=='complete_job':return self.complete(registry,value['job_key'],pid,uid)
            if op=='finalize_job':return self.finalize(registry,value['job_key'],pid,uid)
            if op=='readonly_probe':
                record=self.record(value['job_key']);self.verify_job_peer(registry,record,pid,uid)
                target=record['request']['target_commit'];self.c.hex_value(target,40,'manager_probe_target_invalid')
                root=RELEASES/target
                if root.is_symlink() or not root.is_dir():raise ValueError('manager_probe_release_missing')
                self.command(['/usr/bin/systemd-run','--quiet','--wait','--collect','--pipe',
                    '--unit=eimemory-readonly-probe-'+value['job_key'],
                    '--property=User=darrow','--property=Group=darrow','--property=PrivateUsers=self',
                    '--property=NoNewPrivileges=yes','--property=CapabilityBoundingSet=',
                    '--property=ReadOnlyPaths=/opt/eimemory/releases',
                    str(root/'.venv/bin/python'),'-I','-B','-X','pycache_prefix='+self.c.EMPTY_PYCACHE,'-c',
                    'import os,sys;sys.exit(0 if os.statvfs(sys.argv[1]).f_flag & os.ST_RDONLY else 1)',str(root)])
                return {'readonly':True,'target_commit':target}
            if op=='submit_job':return self.submit(registry,value['job_key'])
            if op=='job_status':return self.job_status(registry,value['job_key'])
            if op=='release_job':return self.release_job(registry,value['job_key'])
            if op=='job_journal':
                record=self.record(value['job_key'])
                return self.journal(record['registry'],record['unit'],value['invocation'],5000)
            return self.transition(registry,value['job_key'],value['phase'],pid,uid)


def receive(stream,size):
    value=b''
    while len(value)<size:
        chunk=stream.recv(size-len(value))
        if not chunk:raise ValueError('manager_request_truncated')
        value+=chunk
    return value


def serve(bridge):
    trusted_directory(SOCKET.parent,mode=0o750)
    os.chown(SOCKET.parent,0,bridge.registry()['runtime_gid'])
    if SOCKET.exists():
        st=SOCKET.lstat()
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid!=0:raise ValueError('manager_socket_untrusted')
        SOCKET.unlink()
    with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as listener:
        listener.bind(str(SOCKET));os.chown(SOCKET,0,bridge.registry()['runtime_gid']);os.chmod(SOCKET,0o660)
        listener.listen(16)
        while True:
            stream,_=listener.accept()
            def handle(stream):
                with stream:
                    stream.settimeout(610)
                    value={}
                    try:
                        pid,uid,_gid=struct.unpack('3i',stream.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
                        size=struct.unpack('>I',receive(stream,4))[0]
                        if size>bridge.c.MAX_REQUEST_BYTES:raise ValueError('manager_request_too_large')
                        value=bridge.c.strict_json(receive(stream,size))
                        result=bridge.dispatch(value,pid=pid,uid=uid)
                        response={'ok':True,**result}
                    except Exception as exc:
                        # Only fixed machine codes, never exception text or env.
                        code=str(exc) if isinstance(exc,ValueError) and re.fullmatch('(?:manager|managed_authority)_[a-z0-9_]+',str(exc)) else 'manager_operation_failed'
                        response={'ok':False,'reason':code}
                    response.update(schema=bridge.c.RESPONSE_SCHEMA,operation=value.get('operation'),registry_generation=value.get('registry_generation'))
                    raw=bridge.c.canonical(response)
                    stream.sendall(struct.pack('>I',len(raw))+raw)
            threading.Thread(target=handle,args=(stream,),daemon=True).start()


if __name__=='__main__':
    if os.geteuid()!=0:raise SystemExit('host_root_required')
    trusted_directory(STATE,mode=0o750)
    import fcntl
    lock=os.open(STATE/'bridge.lock',os.O_WRONLY|os.O_CREAT|os.O_NOFOLLOW,0o600)
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    serve(Bridge(load_contract()))
