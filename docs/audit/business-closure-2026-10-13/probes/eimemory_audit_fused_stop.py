from pathlib import Path
import json,os,subprocess,tempfile
root=Path(tempfile.mkdtemp(prefix='eimemory-audit-fused-stop-'))
bin_dir=root/'bin';bin_dir.mkdir();trace=root/'trace'
fake=bin_dir/'systemctl';fake.write_text('#!/bin/bash\nprintf "systemctl %s\\n" "$*" >> "$AUDIT_TRACE"\n[[ "$2" == stop ]] && exit 3\nexit 0\n');fake.chmod(0o755)
current=root/'current';(current/'.venv/bin').mkdir(parents=True)
python=current/'.venv/bin/python';python.write_text('#!/bin/bash\nprintf "python %s\\n" "$*" >> "$AUDIT_TRACE"\nexit 7\n');python.chmod(0o755)
env=dict(os.environ);env.update(PATH=str(bin_dir)+':'+env['PATH'],EIMEMORY_TRUSTED_REPOSITORY_ROOT=str(root/'repo'),EIMEMORY_L5_FUSED_LOG=str(root/'closure.log'),EIMEMORY_L5_QUERY_STATUS_JSON=str(root/'status.json'),EIMEMORY_DEPLOYMENT_CURRENT_LINK=str(current),EIMEMORY_PRIOR_COMMIT='1'*40,AUDIT_TRACE=str(trace))
result=subprocess.run(['bash','/workspace/eimemory/deploy/run_memory_l5_fused_closure.sh'],env=env,capture_output=True,text=True,timeout=5)
print(json.dumps({'fixture_root':str(root),'exit_code':result.returncode,'log':(root/'closure.log').read_text(),'calls':trace.read_text().splitlines()},indent=2))
