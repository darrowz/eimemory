"""Asserted monetary changes use the existing role and identity contract."""
import pytest

from eimemory.retrieval.answer_requirements import (
    _supports_money_role,
    supports_answer_requirements,
)


CHANGES = ('改为', '调整为', '变更为')
QUERY = '项目 Linden 的预算多少钱？'


@pytest.mark.parametrize('role', [
    pytest.param('合同总金额', id='contract-total'),
    pytest.param('预付款', id='prepayment'),
    pytest.param('预算', id='budget'),
    pytest.param('尾款', id='installment'),
    pytest.param('单价', id='unit-price'),
])
def test_asserted_changes_keep_role_amount_binding(role):
    for change in CHANGES:
        query = f'项目 Linden 的{role}多少钱？'
        evidence = f'项目 Linden 的{role}{change}人民币860元。'
        assert _supports_money_role(query, evidence), (role, change)
        assert supports_answer_requirements(query, evidence), (role, change)


@pytest.mark.parametrize('amount', [
    pytest.param('CNY 1800', id='currency-prefix'),
    pytest.param('￥860', id='currency-symbol'),
    pytest.param('十二万元', id='chinese-numeral'),
    pytest.param('1,800元', id='grouped-number'),
])
def test_completed_changes_keep_existing_currency_grammar(amount):
    for change in CHANGES:
        assert supports_answer_requirements(
            QUERY, f'项目 Linden 已备案，预算已经{change}{amount}。'), change


@pytest.mark.parametrize('template', [
    pytest.param('如果获批，项目 Linden 预算{change}860元。', id='conditional-prefix'),
    pytest.param('项目 Linden 预算{change}860元，前提是获批。', id='conditional-suffix'),
    pytest.param('建议，项目 Linden 预算{change}860元。', id='recommendation-prefix'),
    pytest.param('项目 Linden 预算{change}860元，只是建议。', id='recommendation-suffix'),
    pytest.param('财务要求，项目 Linden 预算{change}860元。', id='requested-change'),
    pytest.param('项目 Linden 预算{change}860元，仅是草案。', id='draft-change'),
    pytest.param('计划将项目 Linden 预算{change}860元。', id='planned-prefix'),
    pytest.param('项目 Linden 预算{change}860元，仍是计划。', id='planned-suffix'),
    pytest.param('项目 Linden 预算未{change}860元。', id='negated-change'),
    pytest.param('项目 Linden 预算{change}860元，但尚未实施。', id='not-implemented'),
    pytest.param('项目 Linden 预算{change}860元，没有生效。', id='not-effective'),
    pytest.param('项目 Linden 预算{change}860元，待审批。', id='pending-approval'),
    pytest.param('项目 Linden 预算{change}860元，仍需审批。', id='requires-approval'),
    pytest.param('可能，项目 Linden 预算{change}860元。', id='uncertain-prefix'),
    pytest.param('项目 Linden 预算{change}860元，是否落实未知。', id='unknown-suffix'),
    pytest.param('项目 Linden 预算{change}860元吗！', id='question-particle'),
    pytest.param('项目 Linden 预算{change}860元，对吗？', id='question-across-comma'),
    pytest.param('If approved, 项目 Linden 预算{change}860元。', id='english-condition'),
])
def test_nonfactual_change_context_survives_comma_split(template):
    for change in CHANGES:
        assert not supports_answer_requirements(QUERY, template.format(change=change)), change


@pytest.mark.parametrize('template', [
    pytest.param('项目 Cedar 预算{change}860元。', id='other-project'),
    pytest.param('项目 Linden 已备案，项目 Cedar 预算{change}860元。', id='other-project-after-comma'),
    pytest.param('项目 Cedar 预算{change}860元，项目 Linden 已备案。', id='other-project-before-comma'),
    pytest.param('项目 Linden 预付款{change}860元。', id='other-role'),
    pytest.param('项目 Linden 预算未知，单价{change}860元。', id='other-role-after-unknown'),
    pytest.param('项目 Linden 预算{change}860。', id='missing-currency'),
    pytest.param('项目 Linden 预算{change}，预付款860元。', id='unbound-amount'),
    pytest.param('项目 Linden 预算方案提到860元。', id='unasserted-number'),
])
def test_change_requires_matching_identity_role_and_currency(template):
    for change in CHANGES:
        assert not supports_answer_requirements(QUERY, template.format(change=change)), change


@pytest.mark.parametrize('evidence', [
    pytest.param('项目 Linden 预算为860元。', id='state-is'),
    pytest.param('项目 Linden 预算约为860元。', id='state-approximate'),
    pytest.param('项目 Linden 预算860元，已确认。', id='state-implicit'),
    pytest.param('项目 Linden 预算为860元，验收后付款。', id='state-payment-condition'),
])
def test_existing_state_assertions_remain_supported(evidence):
    assert supports_answer_requirements(QUERY, evidence)


def test_contract_change_retains_bounded_anaphora():
    assert supports_answer_requirements(
        '项目 Linden 合同总金额多少钱？',
        '项目 Linden 合同已签订。该合同总价调整为USD 1800。')


def test_explicit_record_alias_retains_change_support():
    assert supports_answer_requirements(
        QUERY, '预算变更为860元。', aliases=['Linden'])
