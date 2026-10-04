from contextlib import closing
from dataclasses import asdict

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.raw.boundary import RawRecallUnavailable, require_raw_collection_complete
from eimemory.raw.retrieval import _expand_ranked_turn_context, authoritative_raw_payload
from eimemory.storage.runtime_store import RuntimeStore


@pytest.mark.parametrize('blocked', [None, [], {'quality_rejected': True},
    {'quality_rejected': -1}, {'quality_rejected': 1.5}, {1: 0}])
def test_malformed_completion_counts_cannot_certify_empty(blocked):
    with pytest.raises(RawRecallUnavailable) as caught:
        require_raw_collection_complete([], {'blocked_counts': blocked})
    assert caught.value.allow_recovery is False


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'blocked', 'unavailable', 'partial'])
def test_failure_status_is_unavailable(status):
    with pytest.raises(RawRecallUnavailable):
        require_raw_collection_complete([], {'status': status})


def test_turn_neighbors_do_not_inherit_score_across_source_or_scope(tmp_path):
    scope = ScopeRef(tenant_id='fixture', agent_id='fixture', workspace_id='fixture', user_id='owner')
    shared = ScopeRef(**{**asdict(scope), 'user_id': ''})
    with closing(RuntimeStore(tmp_path)) as store:
        def row(source, row_scope, turn):
            return store.append(RecordEnvelope.create(kind='raw_chunk', title='cedar',
                scope=row_scope, source_id=source, content={'text': 'cedar',
                'session_id': 'same-session', 'turn_id': f'turn:{turn}'}))
        anchor = row('alpha', scope, 1)
        sibling = row('alpha', scope, 2)
        cross_source = row('beta', scope, 2)
        cross_scope = row('alpha', shared, 2)
        result = _expand_ranked_turn_context(store, ranked=[{'record': authoritative_raw_payload(anchor),
            'base_score': 1., 'final_score': 1.}], scope=scope, source_ids=None, limit=4)
        ids = {x['record']['record_id'] for x in result}
        assert sibling.record_id in ids
        assert anchor.record_id in ids
        assert cross_source.record_id not in ids and cross_scope.record_id not in ids
