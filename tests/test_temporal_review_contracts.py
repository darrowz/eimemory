"""Independent temporal-review reproducers; no production data."""
from copy import deepcopy
from dataclasses import fields

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.contracts.temporal_facts import TemporalFactAccess
from eimemory.models.records import RecordEnvelope, ScopeRef
from test_temporal_facts import env, access, write, read, values, counts, JAN, FEB


def test_scope_rewrite_cannot_remove_a_persisted_retraction(env):
    api, store, evidence = env
    a = write(api, evidence, 'scope-a')
    r = write(api, evidence, 'scope-r', None, operation='retraction', targets=[a.record_id],
              start=JAN, expected=a.record_id)
    assert values(read(api, FEB)) == []
    before = counts(store)
    changed = deepcopy(r)
    changed.meta = {}
    changed.scope = ScopeRef(tenant_id='temporal-test', agent_id='agent', workspace_id='work', user_id='another-user')
    with pytest.raises(ValueError):
        store.rewrite(changed, previous_scope=r.scope)
    assert counts(store) == before
    assert store.get_by_id(r.record_id, scope=r.scope).to_dict() == r.to_dict()
    assert values(read(api, FEB)) == []


def test_noncanonical_source_binding_is_rejected_before_writes():
    with pytest.raises(ValueError, match='source'):
        access(source_id='SYNTHETIC')


@pytest.mark.parametrize('cross_partition', [False, True])
def test_admission_callback_cannot_substitute_another_persisted_evidence(env, cross_partition):
    api, store, evidence_id = env
    original = store.get_by_id(evidence_id, scope=ScopeRef(tenant_id='temporal-test',
        agent_id='agent', workspace_id='work', user_id='user'))
    scope = deepcopy(original.scope)
    if cross_partition:
        scope.user_id = 'other-user'
    other = RecordEnvelope.create(kind='memory', title='Different synthetic source',
        summary='Not the evidence requested by the operation.', scope=scope,
        source='test.evidence', source_id='synthetic', meta={'force_capture': True})
    store.append(other)
    before = counts(store)
    def substitute(record):
        for field in fields(record):
            setattr(record, field.name, deepcopy(getattr(other, field.name)))
        return True
    binding = TemporalFactAccess(scope=original.scope, source='test.structured',
        source_id='synthetic', authority='operator', admit_evidence=substitute)
    bad_api = MemoryAPI(store, enable_temporal_facts=True, temporal_fact_access=binding)
    with pytest.raises(ValueError):
        write(bad_api, evidence_id, 'substitute-' + str(cross_partition))
    assert counts(store) == before
