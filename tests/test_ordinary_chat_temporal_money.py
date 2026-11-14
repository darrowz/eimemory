"""Grounded financial transitions must survive ordinary answer admission."""
import pytest
from test_ordinary_chat_temporal_semantics import chat,assert_evidence
from eimemory.retrieval.answer_requirements import _supports_money_role


def test_ordinary_current_and_historical_budget_transition(chat):
    old=chat.say('自2022-01-01起，远帆项目预算是7万元。')
    new=chat.say('自2022-03-01起，远帆项目预算改为9万元。')
    assert_evidence(chat.recall('远帆项目现在的预算是多少？'),value='9万元',source_id=new,excluded=('7万元',))
    assert_evidence(chat.recall('在2022-02-01，远帆项目预算是多少？'),value='7万元',source_id=old,excluded=('9万元',))


@pytest.mark.parametrize('role',['预算','预付款','单价','尾款'])
def test_asserted_change_preserves_role_bound_amount(role):
    assert _supports_money_role('远帆项目'+role+'是多少？','自2022-03-01起，远帆项目'+role+'改为9万元。')


@pytest.mark.parametrize('evidence',[
    '如果获批，远帆项目预算改为9万元。',
    '建议远帆项目预算改为9万元。',
    '计划将远帆项目预算改为9万元。',
    '远帆项目预算未改为9万元。',
    '远帆项目预算不是9万元。',
    '另一项目预算改为9万元。',
    '远帆项目预付款改为9万元。',
])
def test_proposal_negation_other_identity_or_other_role_is_not_budget_evidence(evidence):
    assert not _supports_money_role('远帆项目预算是多少？',evidence)
