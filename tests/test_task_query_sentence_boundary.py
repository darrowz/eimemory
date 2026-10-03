"""Regression for cross-sentence project/status intent contamination."""
import pytest

from eimemory.recall.task_queries import task_recall_mode

SCHOLAR_CATALYST_QUERY = (
    '是什么让伟大的科学家如此伟大？即便 AI 系统开始在未解问题上取得进展，'
    '科学家在感知一个新问题需要哪一先前想法方面仍远远领先于它们——'
    '这些想法深埋于不断增长的研究档案中。为研究这一能力，我们借助那些'
    '亲身了解哪些早期工作推进了其已完成项目的研究者，而论文则充当指向'
    '其中思想的指针。利用我们使作者标注可扩展的自动化流水线，我'
)


def test_original_scholar_catalyst_smoke_is_not_project_status():
    assert task_recall_mode(SCHOLAR_CATALYST_QUERY) == ''


@pytest.mark.parametrize('query', [
    '模型训练取得进展。我们收集了不同项目的研究资料。',
    'Task progress is discussed in the paper! Projects supply the dataset.',
])
def test_status_and_project_in_separate_sentences_do_not_combine(query):
    assert task_recall_mode(query) == ''


@pytest.mark.parametrize('query', [
    'eimemory项目当前进展和卡点',
    'eimemory项目，当前进展如何？',
    '最近已授权任务、进展、待验收',
    'What is the status of my authorized tasks?',
    '背景说明。eimemory项目目前有什么卡点？',
])
def test_real_status_requests_remain_status(query):
    assert task_recall_mode(query) == 'status'


def test_task_history_remains_history():
    assert task_recall_mode('上次我授权了什么任务？') == 'history'
