"""Bounded maintenance of the existing PostgreSQL candidate index."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

from eimemory.api.runtime import Runtime
from .postgres_cli import handle_vector_index_command


def maintain_index(runtime: Runtime) -> dict[str, object]:
    status = handle_vector_index_command(SimpleNamespace(vector_index_command='status'), runtime)
    if not status.get('ok'):
        return status
    state = status.get('vector_index', {})
    if not state.get('enabled') and not state.get('maintenance_enabled'):
        return {'ok': True, 'skipped': 'disabled'}
    if state.get('available'):
        return {'ok': True, 'skipped': 'already_current', 'watermark': state.get('watermark')}
    return handle_vector_index_command(
        SimpleNamespace(vector_index_command='sync', batch_size=4, max_pages=25), runtime)


def main() -> int:
    # Maintenance must never instantiate a serving engine or a reranker.
    os.environ['EIMEMORY_POSTGRES_VECTOR_ENABLED'] = '0'
    os.environ['EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED'] = '0'
    os.environ['EIMEMORY_RERANKER_ENABLED'] = '0'
    runtime = Runtime.create(root=os.environ.get('EIMEMORY_ROOT', '/var/lib/eimemory'))
    try:
        result = maintain_index(runtime)
        print(json.dumps(result, ensure_ascii=True), flush=True)
        return 0 if result.get('ok') else 1
    finally:
        runtime.close()


if __name__ == '__main__':
    raise SystemExit(main())
