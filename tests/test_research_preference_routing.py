"""Research about preferences is not a request for operator preferences."""
import pytest
from eimemory.recall.intent import classify_recall_intent
from eimemory.api.runtime import Runtime


@pytest.mark.parametrize('query', [
    '在这项可复现性研究中，我们考察了通过纳入表示用户偏好的生成式自然语言用户画像而增强的推荐系统的透明性。原始论文探索电影和住宿领域。',
    'Recent papers: embedding metrics, Paint-Anything color control. Themes: cs.LG, cs.AI, cs.IR.',
])
def test_research_markers_inside_chinese_text_and_english_plural(query):
    assert classify_recall_intent(query).name == 'research'


@pytest.mark.parametrize('query', ['研究生入学时间', '研究所地址在哪里', '我偏好简短回答'])
def test_non_research_compounds_and_personal_preferences(query):
    assert classify_recall_intent(query).name != 'research'


def test_research_preference_text_does_not_activate_personal_filter(tmp_path):
    query = '这篇论文研究用户偏好和推荐系统中的自然语言画像。'
    runtime = Runtime.create(root=tmp_path)
    try:
        intent = classify_recall_intent(query, {'intent': 'research'})
        assert not runtime.memory._is_preference_query(query, {}, recall_intent=intent)
        custom = {'preference_query_markers': ['推荐系统']}
        assert runtime.memory._is_preference_query(query, custom, recall_intent=intent)
        # An explicit operator request must keep its narrower behavior.
        context = {'intent': 'operator_preference'}
        personal = classify_recall_intent(query, context)
        assert runtime.memory._is_preference_query(query, context, recall_intent=personal)
    finally:
        runtime.close()
