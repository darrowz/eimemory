from contextlib import closing
from dataclasses import asdict, replace
import pytest
from eimemory.api.runtime import Runtime
from eimemory.identity import needs_hongtu_identity_repair


@pytest.mark.parametrize('flag', [True, False, 'true', None])
def test_exact_scope_survives_identity_repair_only_when_explicit(tmp_path, flag):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        requested = {'agent_id': 'remote-agent', 'workspace_id': 'project-a',
                     'user_id': 'synthetic-owner', 'preserve_scope': flag}
        row = runtime.memory.ingest(text='Remember this synthetic project preference for later.',
            title='scope contract', memory_type='preference', scope=requested,
            source='openclaw.message_received',
            meta={'identity_scope_preserved': True})  # Untrusted metadata is not the provenance marker.
        if flag is True:
            assert row.scope.agent_id == 'remote-agent'
            assert row.scope.workspace_id == 'project-a'
            assert row.provenance['identity_scope_preserved'] is True
            assert needs_hongtu_identity_repair(row) is False
        else:
            assert row.scope.agent_id == 'hongtu'
            assert row.scope.workspace_id == 'embodied'
            assert not row.provenance.get('identity_scope_preserved')
        assert row.scope.user_id == 'synthetic-owner'
        loaded = runtime.store.get_by_id(row.record_id, scope=asdict(row.scope))
        assert loaded is not None and loaded.scope == row.scope
        assert runtime.store.get_by_exact_ref(row.record_id,
            scope=replace(row.scope, user_id='other-owner'), source_id=row.source_id) is None
