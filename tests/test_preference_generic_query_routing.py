"""Ordinary technical modifiers must not select personal-memory filtering."""
from dataclasses import asdict
import re

import pytest

from eimemory.api import memory as memory_module
from eimemory.api.runtime import Runtime
from eimemory.evaluation.controlled_memory_benchmark import (
    build_controlled_memory_benchmark, run_controlled_memory_benchmark,
)
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall import RecallIntent, classify_recall_intent
from eimemory.recall.preference import preference_recall_request, supports_preference_request


SCOPE = ScopeRef("synthetic", "preference-routing", "isolated", "synthetic-member")
CONTEXT = {"source_ids": ["hermes"], "target_source_id": "hermes", "exact_scope_only": True}


@pytest.fixture(autouse=True)
def no_external_models(monkeypatch):
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    monkeypatch.setenv("EIMEMORY_RELEVANCE_ADMISSION_ENABLED", "0")
    monkeypatch.setenv("EIMEMORY_RERANKER_ENABLED", "0")
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete",
                        lambda *a, **kw: pytest.fail("Remote model prohibited"))


def memory(runtime, title, text, *, memory_type="fact", source="operator.memory"):
    return runtime.store.append(RecordEnvelope.create(
        kind="memory", title=title, summary=text, content={"text": text, "memory_type": memory_type},
        meta={"memory_type": memory_type, "memory_layer": "l1", "force_capture": True},
        scope=SCOPE, source=source, source_id="hermes"))


@pytest.mark.parametrize("title,text", [
    ("直接索引寻址", "直接索引寻址计算数组中的元素位置。"),
    ("简洁的数据结构", "采用紧凑数组布局保存Orchid映射表。"),
    ("极简状态机", "状态机只有等待和运行两个状态。"),
    ("处理废话字符串的函数", "函数删除输入字符串中的填充词。"),
])
def test_technical_fact_survives_real_default_controlled_recall(tmp_path, title, text):
    with Runtime.create(root=tmp_path) as runtime:
        target = memory(runtime, title, text)
        intent = classify_recall_intent(title, CONTEXT)
        assert intent.name == "generic"
        assert preference_recall_request(title, CONTEXT) is None
        assert not runtime.memory._is_preference_query(title, CONTEXT, recall_intent=intent)
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        assert dataset["cases"][0]["query"] == title
        report = run_controlled_memory_benchmark(runtime, dataset, scope=SCOPE, source_ids=["hermes"])

        assert report["pass_count"] == report["retrieval_case_count"] == 1
        assert report["fail_count"] == 0
        sample = report["samples"][0]
        assert sample["returned_refs"][0]["record_id"] == target.record_id
        stages = {row["stage"]: row for row in sample["retrieval_diagnostics"]["target_trace"]["stages"]}
        for stage in ("hydrated", "filtered", "final_items"):
            assert stages[stage]["target_presence"] == "present"
        assert stages["filtered"]["drop_reasons"] == []
        assert report["memory_benchmark_accepted"] is False


@pytest.mark.parametrize("query", [
    "我喜欢什么", "我的偏好", "我的回复长度是什么", "我的回复风格是什么",
    "What are my preferences", "What is my response length", "How should you reply to me?",
    "鸿哥 沟通风格", "operator preference", "communication style", "reply style",
])
def test_personal_queries_keep_existing_preference_route(tmp_path, query):
    with Runtime.create(root=tmp_path) as runtime:
        assert isinstance(memory_module._DEFAULT_PREFERENCE_QUERY_MARKER_RE, re.Pattern)
        assert runtime.memory._is_preference_query(query, {}, recall_intent=classify_recall_intent(query))


@pytest.mark.parametrize("query,title,text", [
    ("我的偏好", "用户回复偏好", "用户希望回复简短。"),
    ("我的回复长度是什么", "用户回复长度", "用户希望回复简短。"),
    ("What are my preferences", "Personal preferences", "I prefer concise replies."),
    ("What is my response length", "Response length", "I prefer concise replies."),
])
def test_typed_preference_queries_retrieve_asserted_preference_and_exclude_unrelated_records(tmp_path, query, title, text):
    with Runtime.create(root=tmp_path) as runtime:
        preference = memory(runtime, title, text, memory_type="preference", source="operator.preference")
        unrelated = memory(runtime, title, "The database uses a compact array layout.")
        diagnostic = memory(runtime, "Recall diagnostic", f"recall '{query}' 返回结果不对，需要诊断。",
                            source="operator.diagnostic")
        request = preference_recall_request(query)
        assert request is not None and supports_preference_request(request, preference)
        assert not supports_preference_request(request, unrelated)
        assert not supports_preference_request(request, diagnostic)

        bundle = runtime.memory.recall(query=query, scope=asdict(SCOPE), task_context=dict(CONTEXT), limit=5)

        refs = [item.record_id for item in bundle.items]
        assert preference.record_id in refs
        assert unrelated.record_id not in refs
        assert diagnostic.record_id not in refs


@pytest.mark.parametrize("query", ["我的回复长度是什么", "What are my preferences"])
def test_typed_preference_query_does_not_fall_back_to_fact_when_no_preference_exists(tmp_path, query):
    with Runtime.create(root=tmp_path) as runtime:
        memory(runtime, query, "The database uses a compact array layout.")
        bundle = runtime.memory.recall(query=query, scope=asdict(SCOPE), task_context=dict(CONTEXT), limit=5)
        assert bundle.items == []


@pytest.mark.parametrize("query,language_query,title,text", [
    ("我的回复长度是什么", "我的回复语言是什么", "用户回复语言", "用户希望用英文回复。"),
    ("What is my response length", "What is my response language", "Response language", "I prefer English replies."),
])
def test_length_request_rejects_real_preference_about_language(tmp_path, query, language_query, title, text):
    with Runtime.create(root=tmp_path) as runtime:
        language = memory(runtime, title, text, memory_type="preference", source="operator.preference")
        assert supports_preference_request(preference_recall_request(language_query), language)
        assert not supports_preference_request(preference_recall_request(query), language)

        bundle = runtime.memory.recall(query=query, scope=asdict(SCOPE), task_context=dict(CONTEXT), limit=5)

        assert bundle.items == []


@pytest.mark.parametrize("name,confidence,terms,expected", [
    ("operator_preference", 0.65, ("preference",), True),
    ("living_posture", 0.65, ("reply style",), True),
    ("operator_preference", 0.64, ("preference",), False),
    ("operator_preference", 0.9, ("array", "layout"), False),
    ("generic", 0.9, ("preference",), False),
])
def test_existing_intent_fallback_keeps_its_confidence_and_topic_requirements(tmp_path, name, confidence, terms, expected):
    query = "Atlas array layout"
    assert preference_recall_request(query) is None
    assert memory_module._DEFAULT_PREFERENCE_QUERY_MARKER_RE.search(query) is None
    intent = RecallIntent(name=name, confidence=confidence, reasons=("synthetic intent contract",),
                          preferred_kinds=(), suppressed_kinds=(), source_weights={},
                          memory_cube="general", query_terms=terms)
    with Runtime.create(root=tmp_path) as runtime:
        assert runtime.memory._is_preference_query(query, {}, recall_intent=intent) is expected


def test_explicit_custom_markers_and_research_exemption_keep_their_priority(tmp_path):
    with Runtime.create(root=tmp_path) as runtime:
        query = "直接索引寻址"
        intent = classify_recall_intent(query)
        assert runtime.memory._is_preference_query(
            query, {"preference_query_markers": ["直接"]}, recall_intent=intent)
        research = "这篇论文研究用户偏好和推荐系统中的自然语言画像。"
        research_intent = classify_recall_intent(research, {"intent": "research"})
        assert not runtime.memory._is_preference_query(research, {}, recall_intent=research_intent)
        assert runtime.memory._is_preference_query(
            research, {"preference_query_markers": ["推荐系统"]}, recall_intent=research_intent)
