"""Real RPC contract: related RAW material is not certified answer support.

These tests make no claim about final model answers. The six Chinese questions
are unchanged q31-q36 from the independently frozen synthetic recall exam;
the paired sources below are unchanged m01-m06, not answers to those questions.
Only a single related source per case is needed to exercise the wire contract.
"""
import pytest

from eimemory.raw.retrieval import search_raw_chunks
from test_runtime_raw_citation_delivery import (
    CANONICAL, add_raw, call, owner, persona, runtime,
)


NO_ANSWER_CASES = (
    ("q31", "港湾生产数据库的管理员密码是什么？",
     "港湾上传器曾将整个视频读入内存，2GB样本导致进程OOM；改为64KB流式传输后峰值稳定。"),
    ("q32", "雪松负责人林岚的手机号码是多少？",
     "雪松支付重试缺少幂等键，超时后重复扣款；账单回放确认两次入账。"),
    ("q33", "燧石从未记录的量子索引参数是多少？",
     "燧石迁移在事务内创建大索引，写锁持续47秒；应在低峰并发建索引。"),
    ("q34", "纸鸢下季度预算具体金额？",
     "纸鸢压缩把字节偏移当字符下标，中文标题被截坏；应按UTF8边界裁切。"),
    ("q35", "芦苇集群今天实际在线节点数量？",
     "芦苇队列确认早于落盘，断电测试丢失已确认消息；刷盘后才允许ACK。"),
    ("q36", "欧泊未评测过的GPU吞吐值？",
     "欧泊连接池未关闭游标，运行六小时耗尽句柄；用上下文管理器后复现消失。"),
)


def _assert_related_not_supported(result, record):
    lane = result["bundle"]["raw_citation"]
    assert lane["decision"]["status"] == "related"
    assert lane["admission"]["status"] == "related"
    assert lane["status"] == "related"
    assert any(row["ref"][0] == record.record_id for row in lane["evidence"])
    assert "related material only; entailment is not established" in result["context"]
    assert "RAW citation: supported" not in result["context"]
    assert "足以回答" not in result["context"]


@pytest.mark.parametrize("case_id,query,text", NO_ANSWER_CASES,
                         ids=[case[0] for case in NO_ANSWER_CASES])
def test_original_unanswerable_questions_preserve_related_search_contract(runtime, case_id, query, text):
    record = add_raw(runtime, text, scope=owner(CANONICAL))
    ordinary = search_raw_chunks(runtime.store, query=query, scope=owner(CANONICAL),
        source_ids=["hermes"], limit=5,
        task_context={"rerank_with_llm": False, "enable_llm_rerank": False})
    assert any(row["record"]["record_id"] == record.record_id for row in ordinary)
    response = call(runtime, base=CANONICAL, query=query,
                    task_context={"source_ids": ["hermes"]})
    _assert_related_not_supported(response, record)
    assert any(row["text"] == text for row in response["bundle"]["raw_citation"]["evidence"])


@pytest.mark.parametrize("text", [
    "The Lumen service timeout is 17 seconds.",
    "The Lumen service timeout is described in operational notes.",
], ids=["answer_present", "related_only"])
def test_independent_answerable_and_unanswerable_pair_do_not_upgrade_support(runtime, text):
    # New subject and question, not copied from the original exam. The first
    # source answers it; the second does not. RAW intentionally reports the
    # same lexical status, so this test cannot establish answer correctness.
    query = "What is the Lumen service timeout?"
    preference = "Prefer concise Lumen service timeout reports with primary evidence."
    persona(runtime, CANONICAL, text=preference)
    record = add_raw(runtime, text, scope=owner(CANONICAL))
    response = call(runtime, base=CANONICAL, query=query,
                    task_context={"source_ids": ["hermes"]})
    _assert_related_not_supported(response, record)
    assert response["bundle"]["persona"] and preference in response["context"]


def test_raw_rejection_cannot_withhold_independently_admitted_l1(runtime):
    preference = "Prefer concise Cobalt deployment manifests reports with primary evidence."
    persona(runtime, CANONICAL, text=preference)
    add_raw(runtime, "Deployment manifests describe release procedures.", scope=owner(CANONICAL))
    response = call(runtime, base=CANONICAL,
        query="Where are Cobalt deployment manifests stored?", task_context={"source_ids": ["hermes"]})
    lane = response["bundle"]["raw_citation"]
    assert lane["decision"]["status"] == "reject" and not lane["evidence"]
    assert response["bundle"]["persona"] and preference in response["context"]
