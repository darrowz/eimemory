"""Deployment-process-only atomic refresh of existing operator scope pins."""
import json
import os
from pathlib import Path
from hashlib import sha256
import tempfile

from eimemory.adapters.runtime.host_auth import _read_private_file
from eimemory.governance.evidence_contract import (
    _verified_receipt_identity, _runtime_commit, deployment_receipt_for_scope,
)
from eimemory.models.records import ScopeRef


def refresh_bindings(runtime, path: Path, receipt_id: str) -> int:
    original = _read_private_file(path, max_bytes=65536)
    entries = json.loads(original)
    record = runtime.store.get_by_id(receipt_id)
    identity = _verified_receipt_identity(record)
    if identity is None or identity.commit != _runtime_commit(runtime):
        raise ValueError('new_receipt_not_current')
    keys = {'tenant_id', 'agent_id', 'workspace_id', 'user_id'}
    if not isinstance(entries, list) or not entries:
        raise ValueError('existing_bindings_required')
    for entry in entries:
        if (not isinstance(entry, dict) or not isinstance(entry.get('scope'), dict)
                or set(entry['scope']) != keys
                or not all(isinstance(v, str) and v for v in entry['scope'].values())):
            raise ValueError('invalid_existing_scope')
    digest = sha256(json.dumps(record.to_dict(), sort_keys=True, ensure_ascii=False,
                              separators=(',', ':')).encode()).hexdigest()
    updated = [{**entry, 'receipt_id': receipt_id, 'receipt_sha256': digest} for entry in entries]
    previous = os.environ.get('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE')
    temp = None
    try:
        os.environ['EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE'] = str(path)
        for entry in entries:
            if deployment_receipt_for_scope(runtime, entry.get('receipt_id', ''),
                                            ScopeRef.from_dict(entry['scope'])) is None:
                raise ValueError('existing_binding_unverified')
        fd, name = tempfile.mkstemp(prefix='.release-binding-', dir=path.parent)
        temp = Path(name)
        with os.fdopen(fd, 'w') as stream:
            json.dump(updated, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.environ['EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE'] = str(temp)
        for entry in updated:
            if deployment_receipt_for_scope(runtime, receipt_id, ScopeRef.from_dict(entry['scope'])) is None:
                raise ValueError('new_binding_unverified')
        if _read_private_file(path, max_bytes=65536) != original:
            raise ValueError('bindings_changed_concurrently')
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return len(updated)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
        if previous is None:
            os.environ.pop('EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE', None)
        else:
            os.environ['EIMEMORY_RELEASE_SCOPE_BINDINGS_FILE'] = previous
