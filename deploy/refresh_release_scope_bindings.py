"""Refresh existing scope pins after a successful immutable deployment."""
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    commit, log_path = sys.argv[1:]
    live = Path('/opt/eimemory/current').resolve()
    if live.name != commit:
        raise ValueError('live_commit_mismatch')
    pid = subprocess.check_output(['systemctl', '--user', 'show', 'eimemory-rpc.service',
                                   '-p', 'MainPID', '--value'], text=True).strip()
    values = dict(part.split(b'=', 1) for part in Path('/proc', pid, 'environ').read_bytes().split(b'\0') if b'=' in part)
    binding = values.get(b'EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
    if not binding:
        print(json.dumps({'binding_refresh': 'not_configured'}))
        return
    for key, value in values.items():
        if key.startswith(b'EIMEMORY_'):
            os.environ[key.decode()] = value.decode()
    receipt_ids = set()
    for line in Path(log_path).read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('report_type') == 'l5_release_closure' and row.get('commit') == commit and row.get('receipt_id'):
            receipt_ids.add(row['receipt_id'])
    if len(receipt_ids) != 1:
        raise ValueError('unique_deployment_receipt_required')
    sys.path.insert(0, str(live))
    from eimemory.api.runtime import Runtime
    from eimemory.governance.release_binding_refresh import refresh_bindings
    runtime = Runtime.create(root=Path('/var/lib/eimemory'))
    try:
        count = refresh_bindings(runtime, Path(binding.decode()), next(iter(receipt_ids)))
        print(json.dumps({'binding_refresh': 'verified', 'scope_count': count, 'commit': commit}))
    finally:
        runtime.close()

if __name__ == '__main__':
    main()
