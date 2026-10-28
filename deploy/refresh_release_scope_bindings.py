"""Refresh existing scope pins after a successful immutable deployment."""
import json
import os
from pathlib import Path
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


def already_current_deploy_log(text: str) -> bool:
    """Cheap same-tip installer path prints already_current=1 and skips a new receipt."""
    return any(line.strip() == 'already_current=1' for line in text.splitlines())


def rpc_process_environment(commit: str) -> dict[str, str]:
    from eimemory.ops.runtime_service_observation import service_state
    from eimemory.ops.controlled_stability import _process_environment

    state={'Id':'eimemory-rpc.service',**service_state('eimemory-rpc.service',('MainPID',))}
    _pid,_process,values=_process_environment(state)
    if values.get('EIMEMORY_RUNTIME_COMMIT')!=commit:
        raise ValueError('rpc_process_release_mismatch')
    return values


def resolve_receipt_id(text: str, commit: str, runtime) -> tuple[str, str]:
    """Prefer the unique receipt in this attempt log; on cheap same-tip reuse live identity."""
    try:
        return deployment_receipt_id(text, commit), 'deploy_log'
    except ValueError:
        if not already_current_deploy_log(text):
            raise
        from eimemory.governance.release.evidence_contract import current_release_identity

        identity = current_release_identity(runtime, None)
        if (identity is None or identity.commit != commit
                or not isinstance(identity.receipt_id, str) or not identity.receipt_id):
            raise ValueError('unique_deployment_receipt_required') from None
        return identity.receipt_id, 'already_current_runtime'


def main():
    commit, log_path = sys.argv[1:]
    live = Path('/opt/eimemory/current').resolve()
    if live.name != commit:
        raise ValueError('live_commit_mismatch')
    sys.path.insert(0,str(live))
    values = rpc_process_environment(commit)
    binding = values.get('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
    if not binding:
        print(json.dumps({'binding_refresh': 'not_configured'}))
        return
    for key, value in values.items():
        if key.startswith('EIMEMORY_'):
            os.environ[key] = value
    log_text = Path(log_path).read_text()
    sys.path.insert(0, str(live))
    from eimemory.api.runtime import Runtime
    from eimemory.governance.release_binding_refresh import refresh_bindings
    runtime = Runtime.create(root=Path('/var/lib/eimemory'),bootstrap_schema=False)
    try:
        receipt_id, source = resolve_receipt_id(log_text, commit, runtime)
        count = refresh_bindings(runtime, Path(binding), receipt_id)
        print(json.dumps({
            'binding_refresh': 'verified',
            'scope_count': count,
            'commit': commit,
            'receipt_id': receipt_id,
            'receipt_source': source,
        }))
    finally:
        runtime.close()

if __name__ == '__main__':
    main()
