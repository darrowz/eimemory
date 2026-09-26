from eimemory.retrieval.caller_assistance import prioritize_verification_candidates


def test_short_evidence_is_not_starved_by_long_history():
    history = [(str(i), '历史文本' * 1000) for i in range(21)]
    fact = ('fact', '职责甲由人员乙负责。')
    rows = prioritize_verification_candidates('职责如何分工？', history + [fact])[:8]
    assert fact in rows
    assert len(rows) == 8
    assert sum(len(text) > 1536 for _, text in rows) == 7


def test_short_candidates_do_not_replace_all_fused_leaders():
    history = [(str(i), '历史文本' * 1000) for i in range(8)]
    short = [('s'+str(i), '已检索短证据') for i in range(10)]
    rows = prioritize_verification_candidates('工作规则？', history + short)[:8]
    assert rows[:2] == short[:2]
    assert rows[2:] == history[:6]
