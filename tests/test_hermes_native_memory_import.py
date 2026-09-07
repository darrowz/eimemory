from hashlib import sha256

import pytest

from eimemory.api.runtime import Runtime
from eimemory.models.records import ScopeRef


def test_native_import_is_preview_first_source_faithful_and_idempotent(tmp_path):
    from eimemory.adapters.hermes.native_memory import import_native_memory_lines

    path = tmp_path / 'MEMORY.md'
    raw = '# Memory\n\n[PROJECT] Existing project uses solar generation plus market power backup.\n'
    path.write_text(raw, encoding='utf-8')
    digest = sha256(path.read_bytes()).hexdigest()
    runtime = Runtime.create(root=tmp_path / 'runtime')
    args = dict(path=path, lines=[3], expected_digest=digest,
                scope=ScopeRef('tenant', 'agent', 'workspace', 'user'))
    preview = import_native_memory_lines(runtime, **args)
    assert preview['applied'] is False
    first = import_native_memory_lines(runtime, **args, apply=True)
    second = import_native_memory_lines(runtime, **args, apply=True)
    assert first['record_ids'] == second['record_ids']
    rec = runtime.store.get_by_id(first['record_ids'][0])
    assert rec.content['text'] == raw.splitlines()[2]
    assert rec.scope.workspace_id == 'workspace::channel::hermes'
    assert rec.source_id == 'hermes'
    assert 'source_faithful' in first
    assert raw.splitlines()[2] not in str(first)
    runtime.close()


def test_native_import_rejects_changed_file_or_header_before_writing(tmp_path):
    from eimemory.adapters.hermes.native_memory import import_native_memory_lines

    path = tmp_path / 'MEMORY.md'
    path.write_text('# header\n', encoding='utf-8')
    runtime = Runtime.create(root=tmp_path / 'runtime')
    with pytest.raises(ValueError, match='digest'):
        import_native_memory_lines(runtime, path=path, lines=[1], expected_digest='a'*64,
                                   scope=ScopeRef(), apply=True)
    with pytest.raises(ValueError, match='line'):
        import_native_memory_lines(runtime, path=path, lines=[1], expected_digest=sha256(path.read_bytes()).hexdigest(),
                                   scope=ScopeRef(), apply=True)
    runtime.close()
