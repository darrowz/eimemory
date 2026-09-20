import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.query_input_vault import capture_query_input, load_query_input
from test_query_input_vault import seed


@pytest.mark.parametrize('field', ['tenant_id', 'agent_id', 'workspace_id', 'user_id', 'channel', 'source_id'])
def test_explicit_capture_scope_rejects_other_identity(tmp_path, monkeypatch, field):
    runtime = Runtime.create(root=tmp_path)
    try:
        query, exact = seed(runtime)
        grant = dict(exact, channel='codex', source_id='codex')
        monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([{**grant, field: 'other'}]))
        kwargs = dict(decision_id='decision', query=query, effective_query=query, explanation={})
        assert capture_query_input(runtime, **kwargs)['status'] == 'scope_not_enabled'
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', json.dumps([grant]))
        assert capture_query_input(runtime, **kwargs)['status'] == 'captured'
        assert load_query_input(runtime, decision_id='decision', scope=exact,
                                channel='codex', source_id='codex')['query'] == query
    finally:
        runtime.close()


@pytest.mark.parametrize('policy,status', [
    ('[]', 'scope_not_enabled'),
    ('{', 'scope_policy_invalid'),
    ('null', 'scope_policy_invalid'),
    ('{}', 'scope_policy_invalid'),
    ('[{}]', 'scope_policy_invalid'),
    ('[null]', 'scope_policy_invalid'),
])
def test_capture_policy_fails_closed_without_private_write(tmp_path, monkeypatch, policy, status):
    runtime = Runtime.create(root=tmp_path)
    try:
        query, _ = seed(runtime)
        monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
        monkeypatch.setenv('EIMEMORY_CAPTURE_QUERY_SCOPES', policy)
        assert capture_query_input(runtime, decision_id='decision', query=query,
                                   effective_query=query, explanation={})['status'] == status
        assert runtime.store.sqlite.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='proactive_query_input_vault'"
        ).fetchone() is None
    finally:
        runtime.close()
