"""Refresh existing scope pins after a successful immutable deployment."""
import json
import os
from pathlib import Path
import subprocess
import sys


def deployment_receipt_id(text: str, commit: str) -> str:
    """Read the technical receipt, independent of later L5 qualification."""
    decoder = json.JSONDecoder()
    receipt_ids = set()
    offset = 0
    while (start := text.find('{', offset)) >= 0:
        try:
            row, length = decoder.raw_decode(text[start:])
        except ValueError:
            offset = start + 1
            continue
        offset = start + length
        if (isinstance(row, dict) and row.get('report_type') == 'deployment_receipt'
                and row.get('ok') is True and row.get('commit') == commit
                and row.get('promotion_request_id')):
            receipt_ids.add(str(row['promotion_request_id']))
    if len(receipt_ids) != 1:
        raise ValueError('unique_deployment_receipt_required')
    return next(iter(receipt_ids))


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
    receipt_id = deployment_receipt_id(Path(log_path).read_text(), commit)
    sys.path.insert(0, str(live))
    from eimemory.api.runtime import Runtime
    from eimemory.governance.release_binding_refresh import refresh_bindings
    runtime = Runtime.create(root=Path('/var/lib/eimemory'))
    try:
        count = refresh_bindings(runtime, Path(binding.decode()), receipt_id)
        print(json.dumps({'binding_refresh': 'verified', 'scope_count': count, 'commit': commit}))
    finally:
        runtime.close()

if __name__ == '__main__':
    main()
