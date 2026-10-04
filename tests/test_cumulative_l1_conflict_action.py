import json
from types import SimpleNamespace
import pytest
from eimemory.knowledge.l1_conflict import adjudicate_l1_atoms, L1ConflictJudgeUnavailable
from eimemory.knowledge.sediment import L1Atom
from eimemory.models.records import RecordEnvelope, ScopeRef


@pytest.mark.parametrize('action', [None, '', 'invented', True, [], {}])
def test_strict_fake_judge_invalid_action_is_retryable(action):
    scope = ScopeRef(agent_id='synthetic')
    old = RecordEnvelope.create(kind='memory', title='fixture', scope=scope, source_id='synthetic', meta={'memory_type':'fact'}, content={'memory_type':'fact'})
    api = SimpleNamespace(store=SimpleNamespace(search=lambda **kwargs:[old]))
    llm = SimpleNamespace(complete=lambda **kwargs:SimpleNamespace(text=json.dumps([{'record_id':'new-0','action':action}])))
    atom = L1Atom('Synthetic project belongs to Alice.', 'fixture', 'fact', 'fixture', 'fact')
    with pytest.raises(L1ConflictJudgeUnavailable, match='l1_conflict_action'):
        adjudicate_l1_atoms(api,[atom],scope={'agent_id':'synthetic'},source_id='synthetic',llm=llm,strict=True)
    assert adjudicate_l1_atoms(api,[atom],scope={'agent_id':'synthetic'},source_id='synthetic',llm=llm,strict=False)[0].action == 'store'


@pytest.mark.parametrize('action', ['store','skip','update','merge'])
def test_strict_fake_judge_valid_action_and_exact_target(action):
    scope = ScopeRef(agent_id='synthetic')
    old = RecordEnvelope.create(kind='memory', title='fixture', scope=scope, source_id='synthetic', meta={'memory_type':'fact'}, content={'memory_type':'fact'})
    api = SimpleNamespace(store=SimpleNamespace(search=lambda **kwargs:[old]))
    llm = SimpleNamespace(complete=lambda **kwargs:SimpleNamespace(text=json.dumps([{'record_id':'new-0','action':action,'target_ids':[old.record_id]}])))
    atom = L1Atom('Synthetic project belongs to Alice.', 'fixture', 'fact', 'fixture', 'fact')
    result = adjudicate_l1_atoms(api,[atom],scope={'agent_id':'synthetic'},source_id='synthetic',llm=llm,strict=True)
    assert result[0].action == ('update' if action=='merge' else action)
    assert result[0].target_ids == (old.record_id,)
