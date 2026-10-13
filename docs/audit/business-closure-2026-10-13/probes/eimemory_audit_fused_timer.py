from pathlib import Path
import json,os,subprocess,tempfile
root=Path(tempfile.mkdtemp(prefix='eimemory-audit-fused-timer-'))
bin_dir=root/'bin';bin_dir.mkdir()
trace=root/'systemctl.trace'
fake=bin_dir/'systemctl';fake.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$AUDIT_TIMER_TRACE"\nexit 0\n');fake.chmod(0o755)
env=dict(os.environ)
env.update(PATH=str(bin_dir)+':'+env['PATH'],EIMEMORY_TRUSTED_REPOSITORY_ROOT='',EIMEMORY_DEPLOYMENT_REPO_ROOT='',EIMEMORY_L5_FUSED_LOG=str(root/'closure.log'),EIMEMORY_L5_QUERY_STATUS_JSON=str(root/'status.json'),AUDIT_TIMER_TRACE=str(trace))
result=subprocess.run(['bash','/workspace/eimemory/deploy/run_memory_l5_fused_closure.sh'],env=env,capture_output=True,text=True,timeout=5)
print(json.dumps({'fixture_root':str(root),'exit_code':result.returncode,'log':(root/'closure.log').read_text(),'systemctl_calls':trace.read_text().splitlines()},indent=2))
