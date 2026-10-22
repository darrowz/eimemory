"""Exact manager coordinates and bounded host-manager wire contract.

This stdlib-only module is also installed as a reviewed root-owned copy by the
root bootstrap. Product code never chooses a manager from service health.
"""
from __future__ import annotations
from hashlib import sha256
import json
import re
import os
from typing import Any

REGISTRY_SCHEMA = 'eimemory.runtime_manager_registry.v1'
REQUEST_SCHEMA = 'eimemory.runtime_manager_request.v1'
RESPONSE_SCHEMA = 'eimemory.runtime_manager_response.v1'
JOB_SCHEMA = 'eimemory.code_deployment_job.v1'
REGISTRY_PATH = '/etc/eimemory-code-automation-authority/runtime-manager.v1.json'
SOCKET_PATH = '/run/eimemory-manager/bridge.sock'
USER_MODE = os.environ.get('EIMEMORY_CONTROL_PLANE', '') == 'user'
if not USER_MODE and not os.environ.get('EIMEMORY_CONTROL_PLANE') and os.name=='posix' and os.geteuid()>0:
 import pwd
 from pathlib import Path
 _directory=Path(pwd.getpwuid(os.geteuid()).pw_dir)/'.config/eimemory/control-plane'
 USER_MODE=any(os.path.lexists(_directory/name) for name in ('installation.v1.json','runtime-manager.v1.json'))
if USER_MODE:
 if __package__:
  from eimemory.contracts.user_control_plane import coordinates
 else:
  import importlib.util
  from pathlib import Path
  _helper = Path(__file__).with_name('user_control_plane.py')
  _spec = importlib.util.spec_from_file_location('eimemory_user_control_plane', _helper)
  _module = importlib.util.module_from_spec(_spec)
  exec(compile(_helper.read_bytes(), str(_helper), 'exec'), _module.__dict__)
  coordinates = _module.coordinates
 _user_coordinates = coordinates()
 REGISTRY_PATH = str(_user_coordinates['registry'])
 SOCKET_PATH = str(_user_coordinates['socket'])
JOB_ROOT = '/var/lib/eimemory/state/code-deployment-jobs'
JOB_UNIT_PREFIX = 'eimemory-code-deployment-job@'
CONTROLLER_ENTRY = 'deploy/run_code_deployment_job.py'
VALIDATION_ENTRY = 'deploy/run_code_deployment_validation.py'
EMPTY_PYCACHE = '/var/lib/eimemory-manager/empty-pycache'
if USER_MODE:
 EMPTY_PYCACHE = str(_user_coordinates['pycache'])
FUSED_UNIT = 'eimemory-l5-fused-closure.service'
FUSED_L1_UNITS = ('eimemory-l1-extract.timer','eimemory-l1-extract.service')
VALIDATION_PREFIXES = {'receipt':'eimemory-code-validation-receipt@',
 'pre_observation':'eimemory-code-validation-pre-observation@','post_resume':'eimemory-code-validation-post-resume@'}
VALIDATION_RUNTIME_SECONDS = {'receipt':420,'pre_observation':1860,'post_resume':1860}
MANUAL_FIELDS = frozenset({'schema','operation','controller_commit','validation_controller_commit',
 'base_commit','candidate_commit','target_commit','reviewed_attempt_id','scope'})
MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
READ_PROPERTIES = frozenset({'ActiveState','SubState','Result','InvocationID','MainPID','ExecMainStatus',
 'ExecMainStartTimestamp','ExecMainExitTimestamp','ExecMainStartTimestampMonotonic','ExecMainExitTimestampMonotonic',
 'ExecStart','LoadState','UnitFileState','FragmentPath','DropInPaths','Id','Type','NRestarts','ControlGroup',
 'User','Group','PrivateUsers','NoNewPrivileges','CapabilityBoundingSet','AmbientCapabilities','ReadOnlyPaths',
 'TimeoutStartUSec','TimeoutStopUSec','KillMode','SendSIGKILL','Triggers','TriggeredBy','NextElapseUSecRealtime',
 'LastTriggerUSec','LastTriggerUSecMonotonic','ActiveEnterTimestamp','ActiveEnterTimestampMonotonic'})
# Unit definitions are root-reviewed, not supplied over the socket. These names
# are the complete eimemory runtime surface, including retired compatibility jobs.
PRODUCT_SERVICES = ('eimemory-rpc.service','eimemory-console.service','eimemory-real-effect.service',
 'eimemory-l1-extract.service','eimemory-l5-effect-review.service','eimemory-vector-sync.service',
 'eimemory-backup.service','eimemory-code-implementation-refresh.service','eimemory-release-closure.service',
 'eimemory-nightly.service','eimemory-learn-watch.service','eimemory-learn-think.service',
 'eimemory-learn-dashboard.service','eimemory-audit-verify.service','eimemory-timer-monitor.service',
 'eimemory-experience-autopromote.service','eimemory-l5-fused-closure.service')
PRODUCT_TIMERS = tuple(n.replace('.service','.timer') for n in PRODUCT_SERVICES
 if n not in {'eimemory-rpc.service','eimemory-console.service','eimemory-release-closure.service','eimemory-l5-fused-closure.service'})
PRODUCT_PATHS = ('eimemory-release-closure.path',)
USER_COMPAT_UNITS = ('hermes-gateway.service','openclaw-gateway.service','openclaw-loop-watch.service',
 'openclaw-loop-watch.timer','openclaw-loop-compact.service','openclaw-loop-compact.timer')


def compatibility_units(units):
 """Bounded user-only profile names from an authenticated Root registry.

 This does not authorize a system service or executable. Root owns the registry
 bytes; callers must authenticate it before making privileged decisions.
 """
 if not isinstance(units,dict) or len(units)>128:raise ValueError('manager_registry_unit_inventory_invalid')
 extras=set(units)-set(PRODUCT_SERVICES)-set(PRODUCT_TIMERS)-set(PRODUCT_PATHS)-set(USER_COMPAT_UNITS)
 if len(extras)>64 or any(not isinstance(name,str) or re.fullmatch(r'hermes-gateway-[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}\.service',name) is None for name in extras):
  raise ValueError('manager_registry_unit_inventory_invalid')
 return frozenset((*USER_COMPAT_UNITS,*extras))


REGISTERED_UNITS = frozenset((*PRODUCT_SERVICES,*PRODUCT_TIMERS,*PRODUCT_PATHS,*USER_COMPAT_UNITS))
COMPATIBILITY_PATHS = (
 'eimemory/contracts/runtime_manager.py','eimemory/core/runtime_manager.py',
 'eimemory/contracts/user_control_plane.py',
 'eimemory/identity_authority.py',
 'eimemory/ops/timer_monitor.py','eimemory/ops/controlled_stability.py',
 'eimemory/ops/runtime_identity_drift.py','eimemory/ops/code_implementation_owner.py',
 'eimemory/ops/openclaw_loop.py','eimemory/governance/safety/prompt_safety_executor.py',
 'deploy/runtime_manager_control.py','deploy/eimemory-deploy-worker',
 'deploy/install_immutable_release.sh','deploy/refresh_release_scope_bindings.py',
 'deploy/check_user_systemd_owner.sh','deploy/discover_python_runtime_units.sh',
)
JOB_FIELDS = frozenset({'schema','operation','transaction_id','scope','base_commit','candidate_commit','target_commit',
 'controller_commit','installer_digest','policy_digest','authorization_digest','patch_digest','candidate_tree_digest',
 'verification_receipt_digests','observation_deadline'})

def canonical(value: Any) -> bytes:
 return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')

def digest(value: Any) -> str:
 return sha256(canonical(value)).hexdigest()

def strict_json(raw: bytes, *, max_bytes: int=MAX_REQUEST_BYTES) -> Any:
 if len(raw)>max_bytes:raise ValueError('manager_request_too_large')
 def pairs(items):
  out={}
  for key,value in items:
   if key in out:raise ValueError('manager_duplicate_json_key')
   out[key]=value
  return out
 return json.loads(raw.decode('utf-8'),object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('manager_nonfinite_json')))

def hex_value(value: Any, size: int, code: str) -> str:
 if not isinstance(value,str) or re.fullmatch('[0-9a-f]{'+str(size)+'}',value) is None:raise ValueError(code)
 return value

def job_unit(job_key: str) -> str:
 return JOB_UNIT_PREFIX+hex_value(job_key,64,'manager_job_key_invalid')+'.service'

def validate_job(value: Any, job_key: str) -> dict:
 from datetime import datetime
 if not isinstance(value,dict) or set(value)!=JOB_FIELDS or value.get('schema')!=JOB_SCHEMA:raise ValueError('manager_job_schema_invalid')
 if value['operation'] not in {'deploy','rollback'}:raise ValueError('manager_job_operation_invalid')
 if not isinstance(value['transaction_id'],str) or re.fullmatch('[A-Za-z0-9_.:-]{1,160}',value['transaction_id']) is None:raise ValueError('manager_job_transaction_invalid')
 scope=value['scope']
 if not isinstance(scope,dict) or set(scope)!={'tenant_id','agent_id','workspace_id','user_id'} or any(not isinstance(v,str) or not v or v!=v.strip() or len(v)>256 or any(c in v for c in '\r\n\0') for v in scope.values()):raise ValueError('manager_job_scope_invalid')
 for key in ('base_commit','candidate_commit','target_commit','controller_commit'):hex_value(value[key],40,'manager_job_commit_invalid')
 if value['target_commit']!=value['candidate_commit' if value['operation']=='deploy' else 'base_commit']:raise ValueError('manager_job_target_invalid')
 for key in ('installer_digest','policy_digest','authorization_digest','patch_digest','candidate_tree_digest'):hex_value(value[key],64,'manager_job_digest_invalid')
 receipts=value['verification_receipt_digests']
 if not isinstance(receipts,list) or len(receipts)!=3 or any(not isinstance(x,str) for x in receipts) or receipts!=sorted(set(receipts)):raise ValueError('manager_job_verification_receipts_invalid')
 for item in receipts:hex_value(item,64,'manager_job_verification_receipts_invalid')
 try:deadline=datetime.fromisoformat(value['observation_deadline'].replace('Z','+00:00'))
 except (AttributeError,TypeError,ValueError) as exc:raise ValueError('manager_job_deadline_invalid') from exc
 if deadline.tzinfo is None:raise ValueError('manager_job_deadline_invalid')
 if digest(value)!=hex_value(job_key,64,'manager_job_key_invalid'):raise ValueError('manager_job_digest_mismatch')
 return value

def validate_registry(value: Any) -> dict:
 fields={'schema','generation','runtime_user','runtime_uid','runtime_gid','controller_commit','runtime_commit','repository_root','units','template_hashes','compatibility_hashes','unit_scopes','managed_incident_authority','controller_entry_digest','validation_entry_digest'}
 if not isinstance(value,dict) or set(value)!=fields or value.get('schema')!=REGISTRY_SCHEMA:raise ValueError('manager_registry_schema_invalid')
 hex_value(value['generation'],64,'manager_registry_generation_invalid')
 hex_value(value['controller_commit'],40,'manager_registry_controller_invalid')
 hex_value(value['runtime_commit'],40,'manager_registry_runtime_commit_invalid')
 repository=value.get('repository_root')
 if not isinstance(repository,str) or not repository.startswith('/') or repository=='/' or '..' in repository.split('/') or any(x in repository for x in '\r\n\0') or repository.endswith('/'):
  raise ValueError('manager_registry_repository_invalid')
 if not isinstance(value['runtime_user'],str) or re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',value['runtime_user']) is None or type(value['runtime_uid']) is not int or type(value['runtime_gid']) is not int or value['runtime_uid']<1 or value['runtime_gid']<1:raise ValueError('manager_registry_runtime_identity_invalid')
 units=value['units']
 compat=compatibility_units(units)
 registered=frozenset((*PRODUCT_SERVICES,*PRODUCT_TIMERS,*PRODUCT_PATHS,*compat))
 if set(units)!=registered:raise ValueError('manager_registry_unit_inventory_invalid')
 for name,item in units.items():
  if not isinstance(item,dict) or set(item)!={'manager','installed'} or type(item['installed']) is not bool or item['manager'] not in {'system','user'}:raise ValueError('manager_registry_unit_invalid')
  if item['manager']!=('user' if USER_MODE or name in compat else 'system'):raise ValueError('manager_registry_unit_manager_mismatch')
 if not isinstance(value['template_hashes'],dict) or any(name not in registered and name not in {'eimemory-code-deployment-job@.service',*(prefix+'.service' for prefix in VALIDATION_PREFIXES.values())} for name in value['template_hashes']):raise ValueError('manager_registry_template_invalid')
 for item in value['template_hashes'].values():hex_value(item,64,'manager_registry_template_digest_invalid')
 if not isinstance(value['compatibility_hashes'],dict) or set(value['compatibility_hashes'])!=set(COMPATIBILITY_PATHS):raise ValueError('manager_registry_compatibility_invalid')
 for item in value['compatibility_hashes'].values():hex_value(item,64,'manager_registry_compatibility_digest_invalid')
 scopes=value['unit_scopes']
 if not isinstance(scopes,dict) or not set(scopes)<=set(PRODUCT_SERVICES):raise ValueError('manager_registry_unit_scopes_invalid')
 for scope in scopes.values():validate_scope(scope)
 validate_managed_authority(value['managed_incident_authority'])
 if value['controller_entry_digest'] is not None:hex_value(value['controller_entry_digest'],64,'manager_registry_controller_entry_digest_invalid')
 if value['validation_entry_digest'] is not None:hex_value(value['validation_entry_digest'],64,'manager_registry_validation_entry_digest_invalid')
 if value['generation']!=digest({k:v for k,v in value.items() if k!='generation'}):raise ValueError('manager_registry_digest_mismatch')
 return value

JOURNAL_ERROR_PATTERNS = (
 ('crash', r'segfault|core-dump|dumped core'),
 ('oom', r'oom-kill|out of memory|killed process'),
 ('database_read_write_failure', r'database is locked|database is read.only|attempt to write a readonly|disk i/o error|database or disk is full|sqlite.*(?:permission denied|operationalerror|databaseerror)'),
)


def validate_scope(value: Any) -> dict:
 if not isinstance(value,dict) or set(value)!={'tenant_id','agent_id','workspace_id','user_id'} or any(not isinstance(v,str) or not v or v!=v.strip() or len(v)>256 or any(c in v for c in '\r\n\0') for v in value.values()):raise ValueError('manager_scope_invalid')
 return value


def validate_manual_permit(value: Any, job_key: str) -> dict:
 if not isinstance(value,dict) or set(value)!=MANUAL_FIELDS or value.get('schema')!='eimemory.manual_release_permit.v2':
  raise ValueError('manager_manual_permit_schema_invalid')
 if value['operation']!='deploy':raise ValueError('manager_manual_permit_operation_invalid')
 for name in ('controller_commit','validation_controller_commit','base_commit','candidate_commit','target_commit'):
  hex_value(value[name],40,'manager_manual_permit_commit_invalid')
 if value['target_commit']!=value['candidate_commit'] or value['validation_controller_commit']!=value['candidate_commit']:
  raise ValueError('manager_manual_permit_target_invalid')
 hex_value(value['reviewed_attempt_id'],32,'manager_manual_permit_attempt_invalid')
 validate_scope(value['scope'])
 if digest(value)!=hex_value(job_key,64,'manager_job_key_invalid'):raise ValueError('manager_manual_permit_digest_mismatch')
 return value


def validate_managed_authority(value: Any) -> dict:
 if value=={'enabled':False}:return value
 fixed={'database_path':'/var/lib/eimemory/state/eimemory.sqlite','manifest_path':'/etc/eimemory-code-automation-authority/managed-code-incident-manifest.v1.json','receipt_directory':'/var/lib/eimemory-manager/managed-incident-receipts','key_path':'/etc/eimemory-code-automation-authority/managed-incident-proof.key'}
 if USER_MODE:
  fixed.update(manifest_path=str(_user_coordinates['authority']/'managed-code-incident-manifest.v1.json'),
   receipt_directory=str(_user_coordinates['state']/'managed-incident-receipts'),
   key_path=str(_user_coordinates['authority']/'managed-incident-proof.key'))
 if not isinstance(value,dict) or set(value)!={'enabled','manifest_sha256',*fixed} or value.get('enabled') is not True or any(value[k]!=v for k,v in fixed.items()):raise ValueError('manager_managed_authority_registry_invalid')
 hex_value(value['manifest_sha256'],64,'manager_managed_authority_manifest_digest_invalid')
 return value


def validation_unit(job_key: str, operation: str) -> str:
 if operation not in VALIDATION_PREFIXES:raise ValueError('manager_validation_operation_invalid')
 return VALIDATION_PREFIXES[operation]+hex_value(job_key,64,'manager_job_key_invalid')+'.service'


def is_dynamic_unit(unit: str) -> bool:
 if not isinstance(unit,str):return False
 if unit.startswith(JOB_UNIT_PREFIX):
  return unit==job_unit(unit[len(JOB_UNIT_PREFIX):-8])
 for operation,prefix in VALIDATION_PREFIXES.items():
  if unit.startswith(prefix):return unit==validation_unit(unit[len(prefix):-8],operation)
 return False


NATIVE_EXIT_PROPERTIES = ('LoadState','ActiveState','SubState','Result','InvocationID',
 'ExecMainStatus','MainPID','ControlGroup','ExecMainExitTimestampMonotonic')


def fixed_system_unit_cgroup(value: Any, unit: str) -> bool:
 """Only the reviewed flat/static or exact PID1 template slice is admissible."""
 if not isinstance(unit,str) or not isinstance(value,str):return False
 if USER_MODE:
  if unit not in REGISTERED_UNITS and not is_dynamic_unit(unit):return False
  prefix='/user.slice/user-'+str(_user_coordinates['uid'])+'.slice/user@'+str(_user_coordinates['uid'])+'.service/'
  suffix=value.removeprefix(prefix)
  return (value.startswith(prefix) and suffix.endswith('/'+unit)
   and all(re.fullmatch(r'[A-Za-z0-9_.@\\-]+\.slice', part) for part in suffix.split('/')[:-1]))
 flat='/system.slice/'+unit
 if unit in REGISTERED_UNITS:
  return unit not in USER_COMPAT_UNITS and value==flat
 try:
  dynamic=is_dynamic_unit(unit)
 except (TypeError,ValueError):
  return False
 if not dynamic:return False
 template=unit.partition('@')[0].replace('-',r'\x2d')
 return value in {flat,'/system.slice/system-'+template+'.slice/'+unit}


def native_terminal_exit(state: Any, invocation: Any, unit: str) -> bool:
 """A native process exit; empty cgroup is valid only after positive exit proof."""
 if not isinstance(state,dict) or not set(NATIVE_EXIT_PROPERTIES)<=set(state):return False
 actual=state['InvocationID'];mono=state['ExecMainExitTimestampMonotonic']
 if (not isinstance(actual,str) or re.fullmatch('[0-9a-f]{32}',actual) is None
     or (invocation is not None and invocation!=actual) or state['LoadState']!='loaded' or state['MainPID']!='0'
     or not isinstance(mono,str) or not mono.isdigit() or int(mono)<=0):return False
 cg=state['ControlGroup']
 if cg!='' and not fixed_system_unit_cgroup(cg,unit):return False
 success=(state['ActiveState']=='active' and state['SubState']=='exited'
     and state['Result']=='success' and state['ExecMainStatus']=='0')
 failed=(state['ActiveState'] in {'inactive','failed'}
     and state['Result'] in {'exit-code','signal','timeout','core-dump','watchdog'})
 return success or failed


def native_terminal_readback(pinned: Any, state: Any, invocation: Any, unit: str) -> bool:
 """Keep an authentic pinned exit across cgroup release/stop/GC, never a new run."""
 if not isinstance(invocation,str) or re.fullmatch('[0-9a-f]{32}',invocation) is None:return False
 if not native_terminal_exit(pinned,invocation,unit) or not isinstance(state,dict):return False
 if not set(NATIVE_EXIT_PROPERTIES)<=set(state):return False
 actual=state['InvocationID']
 if not isinstance(actual,str) or actual not in {'',invocation} or state['MainPID'] not in {'0',''}:return False
 if state['LoadState']=='not-found':
  return state['ActiveState'] in {'inactive','failed'} and state['ControlGroup']==''
 if state['LoadState']!='loaded':return False
 if state['ControlGroup']!='' and not fixed_system_unit_cgroup(state['ControlGroup'],unit):return False
 if not actual:
  return state['ActiveState'] in {'inactive','failed'}
 stable=('InvocationID','Result','ExecMainStatus','ExecMainExitTimestampMonotonic')
 if state['MainPID']!='0' or any(state[name]!=pinned[name] for name in stable):return False
 if state['ActiveState'] in {'inactive','failed'}:return True
 return native_terminal_exit(state,invocation,unit)
