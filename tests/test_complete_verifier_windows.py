"""Predefined window IDs never certify an incomplete clipped assertion."""
from eimemory.retrieval.verifier_projection import (
    MAX_PARENT_CHARS, MAX_WINDOW_CHARS, complete_evidence_windows,
)


def test_bounded_complete_turn_stays_verbatim_with_its_late_qualification():
    parent = 'User: What is the approved amount?\nAssistant: The estimate is 12.7. Funding is not committed.'
    windows = complete_evidence_windows('approved amount', parent)
    assert len(windows) == 1 and windows[0].text == parent
    assert 'not committed' in windows[0].text


def test_long_windows_align_inward_and_preserve_absolute_parent_offsets():
    parent = ('An earlier unrelated complete sentence.\n' * 60
              + 'The approved estimate is 12.7 billion; funding is not committed.\n')
    windows = complete_evidence_windows('approved estimate funding', parent)
    assert windows and any('funding is not committed.' in window.text for window in windows)
    for window in windows:
        assert parent[window.start:window.end] == window.text
        assert len(window.text) <= MAX_WINDOW_CHARS
        assert window.text.endswith(('.', ';'))
        assert window.start == 0 or parent[window.start - 1] in '\n '


def test_single_overlong_unbroken_assertion_has_no_id_selection():
    assert complete_evidence_windows('qualifier', 'x' * 900 + ' qualifier unknown') == ()


def test_a_truncated_parent_tail_is_not_a_complete_boundary():
    parent = 'x' * (MAX_PARENT_CHARS + 100)
    assert complete_evidence_windows('x', parent) == ()


def test_chinese_qualification_and_decimal_are_not_split_at_decimal_point():
    parent = 'User: 投资是多少？\nAssistant: 建议书估算12.7亿元；资金尚未落实，不承诺收益。'
    result = complete_evidence_windows('投资', parent)
    assert len(result) == 1 and result[0].text == parent
