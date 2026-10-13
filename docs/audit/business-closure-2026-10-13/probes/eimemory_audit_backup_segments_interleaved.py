from pathlib import Path
import importlib.util, json, os, shutil, sqlite3, sys, tempfile
sys.path.insert(0, '/workspace/eimemory')
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef
repo=Path('/workspace/eimemory')
root=Path(tempfile.mkdtemp(prefix='eimemory-audit-backup-segments-'))
config=root/'config'; config.mkdir(); (config/'rpc.env').write_text('FIXTURE=1\n')
data=root/'runtime'
runtime=Runtime.create(root=data)
scope=ScopeRef(tenant_id='audit',agent_id='agent',workspace_id='workspace',user_id='user')
record=RecordEnvelope.create(kind='capability_score',title='Archived audit fixture',summary='Archived audit fixture',content={'samples':['fixture-'+('x'*200000)]},scope=scope,source='audit',source_id='audit')
runtime.store.append(record)
before=runtime.store.get_by_id(record.record_id,scope=scope)
assert before and before.content==record.content
pointer=runtime.store.run_locked(lambda store: store.conn.execute('SELECT payload_pointer_json FROM records WHERE record_id=?',(record.record_id,)).fetchone()[0])
assert pointer
runtime.close()
cli=root/'eimemory-fixture'
cli.write_text('#!'+sys.executable+'\nimport sys,os,json\nsys.path.insert(0,"/workspace/eimemory")\nfrom eimemory.api.runtime import Runtime\nfrom eimemory.compatibility.migration_helpers import backup_create,backup_verify\nfrom pathlib import Path\ncmd,path=sys.argv[2:]\nif cmd=="create":\n runtime=Runtime.create(root=Path(os.environ["EIMEMORY_ROOT"]))\n result=backup_create(runtime,path)\n runtime.close()\nelse:\n result=backup_verify(path)\nprint(json.dumps(result))\nraise SystemExit(0 if result["ok"] else 1)\n')
cli.chmod(0o755)
spec=importlib.util.spec_from_file_location('backup',repo/'deploy/eimemory_backup.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
original_backup_sqlite=mod._backup_sqlite
def scheduled_mutation(source,target):
 report=original_backup_sqlite(source,target)
 # A real authoritative write at the boundary between the SQLite snapshot
 # and later logical export; the backup output/verification is unchanged.
 writer=Runtime.create(root=data)
 changed=RecordEnvelope.from_dict(record.to_dict())
 changed.content={'samples':['newer-fixture-'+('y'*200000)]}
 writer.store.append(changed)
 writer.close()
 return report
mod._backup_sqlite=scheduled_mutation
result=mod.run_backup(root=data,config_dir=config,backup_root=root/'backups',eimemory_bin=str(cli),keep=3)
assert result['ok'], result
backup=Path(result['path'])
manifest=json.loads((backup/'backup-set.json').read_text())
restored=root/'restored';(restored/'state').mkdir(parents=True)
shutil.copy2(backup/'sqlite/eimemory.sqlite',restored/'state/eimemory.sqlite')
error=''
try:
 r=Runtime.create(root=restored)
 try:
  r.store.get_by_id(record.record_id,scope=scope)
 except Exception as exc:
  error=type(exc).__name__+':'+str(exc)
 finally:
  r.close()
except Exception as exc:
 error=type(exc).__name__+':'+str(exc)
print(json.dumps({'fixture_root':str(root),'backup_ok':result['ok'],'sqlite_integrity':result['sqlite'],'archived_pointer_present':bool(pointer),'segment_files_in_manifest':[i['path'] for i in manifest['files'] if 'payload_segments' in i['path']],'logical_backup_rows':manifest['records']['record_count'],'logical_has_snapshot_original':any(json.loads(line).get('content')==record.content for line in (backup/'records/backup.jsonl').read_text().splitlines()),'restored_read_error':error},indent=2))
