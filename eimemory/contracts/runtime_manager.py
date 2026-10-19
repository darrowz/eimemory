"""Exact manager coordinates and bounded host-manager wire contract.

This stdlib-only module is also installed as a reviewed root-owned copy by the
root bootstrap. Product code never chooses a manager from service health.
"""
from __future__ import annotations
from hashlib import sha256
import json
import re
from typing import Any

REGISTRY_SCHEMA = 'eimemory.runtime_manager_registry.v1'
REQUEST_SCHEMA = 'eimemory.runtime_manager_request.v1'
RESPONSE_SCHEMA = 'eimemory.runtime_manager_response.v1'
JOB_SCHEMA = 'eimemory.code_deployment_job.v1'
REGISTRY_PATH = '/etc/eimemory-code-automation-authority/runtime-manager.v1.json'
SOCKET_PATH = '/run/eimemory-manager/bridge.sock'
JOB_ROOT = '/var/lib/eimemory/state/code-deployment-jobs'
JOB_UNIT_PREFIX = 'eimemory-code-deployment-job@'
CONTROLLER_ENTRY = 'deploy/run_code_deployment_job.py'
VALIDATION_ENTRY = 'deploy/run_code_deployment_validation.py'
EMPTY_PYCACHE = '/var/lib/eimemory-manager/empty-pycache'
VALIDATION_PREFIXES = {'receipt':'eimemory-code-validation-receipt@','post_resume':'eimemory-code-validation-post-resume@'}
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
USER_COMPAT_UNITS = ('hermes-gateway.service','hermes-gateway-hongtai.service','hermes-gateway-hongxin.service',
 'hermes-gateway-xiaomage.service','openclaw-gateway.service','openclaw-loop-watch.service',
 'openclaw-loop-watch.timer','openclaw-loop-compact.service','openclaw-loop-compact.timer')
REGISTERED_UNITS = frozenset((*PRODUCT_SERVICES,*PRODUCT_TIMERS,*PRODUCT_PATHS,*USER_COMPAT_UNITS))
COMPATIBILITY_PATHS = (
 'eimemory/contracts/runtime_manager.py','eimemory/core/runtime_manager.py',
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
 if value['runtime_user']!='darrow' or type(value['runtime_uid']) is not int or type(value['runtime_gid']) is not int or value['runtime_uid']<1 or value['runtime_gid']<1:raise ValueError('manager_registry_runtime_identity_invalid')
 units=value['units']
 if not isinstance(units,dict) or set(units)!=REGISTERED_UNITS:raise ValueError('manager_registry_unit_inventory_invalid')
 for name,item in units.items():
  if not isinstance(item,dict) or set(item)!={'manager','installed'} or type(item['installed']) is not bool or item['manager'] not in {'system','user'}:raise ValueError('manager_registry_unit_invalid')
  if item['manager']!=('user' if name in USER_COMPAT_UNITS else 'system'):raise ValueError('manager_registry_unit_manager_mismatch')
 if not isinstance(value['template_hashes'],dict) or any(name not in REGISTERED_UNITS and name not in {'eimemory-code-deployment-job@.service','eimemory-code-validation-receipt@.service','eimemory-code-validation-post-resume@.service'} for name in value['template_hashes']):raise ValueError('manager_registry_template_invalid')
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


def validate_managed_authority(value: Any) -> dict:
 if value=={'enabled':False}:return value
 fixed={'database_path':'/var/lib/eimemory/state/eimemory.sqlite','manifest_path':'/etc/eimemory-code-automation-authority/managed-code-incident-manifest.v1.json','receipt_directory':'/var/lib/eimemory-manager/managed-incident-receipts','key_path':'/etc/eimemory-code-automation-authority/managed-incident-proof.key'}
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
