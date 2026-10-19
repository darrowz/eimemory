"""Authenticated client for the fixed host manager. No state-based fallback."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path
import socket
import stat
import struct
from typing import Any
from eimemory.contracts.runtime_manager import (REGISTRY_PATH,SOCKET_PATH,MAX_REQUEST_BYTES,MAX_RESPONSE_BYTES,
 REQUEST_SCHEMA,RESPONSE_SCHEMA,READ_PROPERTIES,canonical,strict_json,validate_registry,hex_value,job_unit,validation_unit)

class RuntimeManagerError(RuntimeError):pass

def read_registry(path: Path | None=None) -> dict:
 path=Path(REGISTRY_PATH) if path is None else Path(path)
 try:
  for parent in (path.parent,*path.parent.parents):
   metadata=parent.lstat()
   if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=0 or stat.S_IMODE(metadata.st_mode)&0o022:raise RuntimeManagerError('manager_registry_parent_untrusted')
  fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
  try:
   before=os.fstat(fd)
   if not stat.S_ISREG(before.st_mode) or before.st_uid!=0 or before.st_nlink!=1 or stat.S_IMODE(before.st_mode)!=0o640 or before.st_size>65536:raise RuntimeManagerError('manager_registry_file_untrusted')
   raw=os.read(fd,65537);after=os.fstat(fd);named=path.lstat()
   identity=lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_mode,s.st_uid,s.st_nlink)
   if identity(before)!=identity(after) or identity(before)!=identity(named):raise RuntimeManagerError('manager_registry_changed')
  finally:os.close(fd)
  return validate_registry(strict_json(raw))
 except (OSError,ValueError) as exc:raise RuntimeManagerError('manager_registry_unavailable') from exc

def manager_for(unit: str, *, registry: dict | None=None) -> str:
 registry=read_registry() if registry is None else validate_registry(registry)
 if unit not in registry['units']:raise RuntimeManagerError('manager_unit_not_registered')
 return registry['units'][unit]['manager']

def _receive(stream: socket.socket, size: int) -> bytes:
 chunks=[]
 while size:
  block=stream.recv(size)
  if not block:raise RuntimeManagerError('manager_response_truncated')
  chunks.append(block);size-=len(block)
 return b''.join(chunks)

def request(operation: str, **values: Any) -> dict:
 registry=read_registry()
 body={'schema':REQUEST_SCHEMA,'operation':operation,'registry_generation':registry['generation'],**values}
 raw=canonical(body)
 if len(raw)>MAX_REQUEST_BYTES:raise RuntimeManagerError('manager_request_too_large')
 try:
  socket_path=Path(SOCKET_PATH)
  for parent in (socket_path.parent,*socket_path.parent.parents):
   meta=parent.lstat()
   if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=0 or stat.S_IMODE(meta.st_mode)&0o022:raise RuntimeManagerError('manager_socket_parent_untrusted')
  meta=socket_path.lstat()
  if not stat.S_ISSOCK(meta.st_mode) or meta.st_uid!=0 or meta.st_gid!=registry['runtime_gid'] or stat.S_IMODE(meta.st_mode)!=0o660 or meta.st_nlink!=1:raise RuntimeManagerError('manager_socket_untrusted')
  with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as stream:
   stream.settimeout(610 if operation in {'transition','readonly_probe','finalize_job','begin_fused_closure','end_fused_closure','recover_fused_closure'} else
       120 if operation in {'issue_managed_incident_proof','verify_managed_incident_proof','checkpoint_managed_incident_knowledge'} else 30);stream.connect(SOCKET_PATH)
   if not hasattr(socket,'SO_PEERCRED'):raise RuntimeManagerError('manager_peer_credentials_unavailable')
   _pid,uid,_gid=struct.unpack('3i',stream.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,struct.calcsize('3i')))
   if uid!=0:raise RuntimeManagerError('manager_peer_is_not_host_root')
   stream.sendall(struct.pack('>I',len(raw))+raw)
   length=struct.unpack('>I',_receive(stream,4))[0]
   if length>MAX_RESPONSE_BYTES:raise RuntimeManagerError('manager_response_too_large')
   response=strict_json(_receive(stream,length),max_bytes=MAX_RESPONSE_BYTES)
 except OSError as exc:raise RuntimeManagerError('manager_transport_unavailable') from exc
 if not isinstance(response,dict) or response.get('schema')!=RESPONSE_SCHEMA or response.get('operation')!=operation or response.get('registry_generation')!=registry['generation']:raise RuntimeManagerError('manager_response_identity_mismatch')
 if response.get('ok') is not True:raise RuntimeManagerError(str(response.get('reason') or 'manager_request_denied'))
 return response

def unit_state(unit: str, properties: tuple[str,...]=('ActiveState','SubState','Result','InvocationID','MainPID','ExecMainStatus')) -> dict:
 if not properties or any(item not in READ_PROPERTIES for item in properties):raise RuntimeManagerError('manager_property_not_allowlisted')
 return request('unit_state',unit=unit,properties=list(properties))['state']

def unit_journal(unit: str, invocation: str, *, limit: int=5000) -> dict:
 hex_value(invocation,32,'manager_invocation_invalid')
 if type(limit) is not int or not 1<=limit<=5000:raise RuntimeManagerError('manager_journal_limit_invalid')
 return request('unit_journal',unit=unit,invocation=invocation,limit=limit)

def submit_deployment_job(job_key: str) -> dict:
 job_unit(job_key);return request('submit_job',job_key=job_key)

def deployment_job_status(job_key: str) -> dict:
 job_unit(job_key);return request('job_status',job_key=job_key)

def deployment_job_journal(job_key: str, invocation: str) -> dict:
 job_unit(job_key);hex_value(invocation,32,'manager_invocation_invalid')
 return request('job_journal',job_key=job_key,invocation=invocation)

def transition(job_key: str, phase: str) -> dict:
 job_unit(job_key)
 if phase not in {'quiesce','publish','activate','resume_watchers','resume','rollback'}:raise RuntimeManagerError('manager_transition_phase_invalid')
 return request('transition',job_key=job_key,phase=phase)


def runtime_commit(unit: str) -> str:
 return request('runtime_identity',unit=unit)['runtime_commit']


def finalize_deployment_job(job_key: str) -> dict:
 job_unit(job_key);return request('finalize_job',job_key=job_key)


def activation_attestation() -> dict:
 return request('activation_attestation')['reference']


def verify_activation_attestation(receipt_id: str, digest: str) -> dict:
 hex_value(receipt_id,64,'manager_activation_receipt_id_invalid');hex_value(digest,64,'manager_activation_receipt_digest_invalid')
 return request('verify_activation_attestation',receipt_id=receipt_id,digest=digest)['attestation']


def complete_deployment_job(job_key: str) -> dict:
 job_unit(job_key);return request('complete_job',job_key=job_key)


def issue_managed_incident_proof(source_record_id: str, source_digest: str) -> dict:
 if not isinstance(source_record_id,str) or re.fullmatch('[A-Za-z0-9_.:-]{1,256}',source_record_id) is None:
  raise RuntimeManagerError('manager_managed_source_id_invalid')
 hex_value(source_digest,64,'manager_managed_source_digest_invalid')
 return request('issue_managed_incident_proof',source_record_id=source_record_id,source_digest=source_digest)['reference']


def verify_managed_incident_proof(receipt_id: str, digest: str) -> dict:
 hex_value(receipt_id,64,'manager_managed_receipt_id_invalid');hex_value(digest,64,'manager_managed_receipt_digest_invalid')
 return request('verify_managed_incident_proof',receipt_id=receipt_id,digest=digest)['proof']


def checkpoint_managed_incident_knowledge() -> dict:
 return request('checkpoint_managed_incident_knowledge')['checkpoint']


def validate_deployment_job(job_key: str, operation: str) -> dict:
 validation_unit(job_key,operation)
 response=request('validate_deployment_job',job_key=job_key,validation_operation=operation)
 return {**response['validation'],'registry_generation':response['registry_generation']}


def deployment_validation_status(job_key: str, operation: str) -> dict:
 validation_unit(job_key,operation)
 response=request('deployment_validation_status',job_key=job_key,validation_operation=operation)
 return {**response['validation'],'registry_generation':response['registry_generation']}


def seal_deployment_validation_result(job_key: str, operation: str, invocation: str, artifact_digest: str) -> dict:
 validation_unit(job_key,operation)
 hex_value(invocation,32,'manager_validation_invocation_invalid');hex_value(artifact_digest,64,'manager_validation_artifact_digest_invalid')
 response=request('seal_deployment_validation_result',job_key=job_key,validation_operation=operation,
     invocation=invocation,artifact_digest=artifact_digest)
 return {**response['validation'],'registry_generation':response['registry_generation']}


def begin_fused_closure() -> dict:
 return request('begin_fused_closure')


def end_fused_closure(context_key: str) -> dict:
 hex_value(context_key,64,'manager_fused_key_invalid')
 return request('end_fused_closure',context_key=context_key)
