"""Sanitized pre-call structure from the 1.14.27 natural observation.

The record existed before the decision and was not edited afterwards. This
freezes the title/body shape, not private text, IDs, embeddings or a live replay.
"""
import pytest

from eimemory.recall.task_queries import task_recall_mode
from eimemory.retrieval.proactive import ProactiveRecallService


@pytest.mark.parametrize('query,context', [
    ('只读核对最终收据和评估结果',
     'Hermes completed turn User: 图片介绍热门项目 Atlas 的记忆基准，项目正在整理主题摘要。'),
    ('核对测试结果', 'Hermes completed turn Atlas项目已完成发布'),
    ('Atlas项目现在进展如何？', '之前任务历史我们已授权部署'),
])
def test_context_cannot_change_original_task_route(query, context):
    expanded = ProactiveRecallService._recall_query(query, [context])
    assert task_recall_mode(expanded) == task_recall_mode(query)


def test_context_entities_remain_when_task_route_is_unchanged():
    query = '查看相关资料'
    expanded = ProactiveRecallService._recall_query(query, ['Atlas 索引设计'])
    assert expanded.startswith(query)
    assert 'atlas' in expanded
    assert task_recall_mode(expanded) == task_recall_mode(query)
