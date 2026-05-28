from eimemory.recall import RecallIntent, classify_recall_intent, recall_filters_for_intent


def test_classify_recall_intent_empty_query_is_generic() -> None:
    intent = classify_recall_intent("")

    assert isinstance(intent, RecallIntent)
    assert intent.name == "generic"
    assert intent.confidence == 0.0
    assert intent.memory_cube == "general"
    assert intent.preferred_kinds == ()
    assert intent.suppressed_kinds == ()
    assert intent.source_weights == {}
    assert intent.query_terms == ()


def test_recall_filters_for_intent() -> None:
    intent = classify_recall_intent("UUMit 交付品质 海报 v2")
    filters = recall_filters_for_intent(intent)

    assert filters["intent"] == "project_delivery"
    assert filters["memory_cube"] == "project"
    assert filters["preferred_kinds"] == ("memory", "rule", "raw_chunk", "reflection")
    assert filters["suppressed_kinds"] == ("knowledge_page",)
    assert filters["query_terms"] and "uumit" in filters["query_terms"]
    assert isinstance(filters["source_weights"], dict)
    assert "allowed_kinds" not in filters
    assert "blocked_kinds" not in filters


def test_classify_recall_intent_project_delivery_query() -> None:
    intent = classify_recall_intent("UUMit 交付品质 海报 v2")

    assert intent.name == "project_delivery"
    assert intent.confidence >= 0.7
    assert intent.memory_cube == "project"
    assert intent.preferred_kinds == ("memory", "rule", "raw_chunk", "reflection")
    assert intent.suppressed_kinds == ("knowledge_page",)
    assert intent.query_terms and "uumit" in intent.query_terms
    assert any("delivery" in reason.lower() or "project" in reason.lower() for reason in intent.reasons)


def test_classify_recall_intent_project_delivery_tends_to_living_posture_with_context() -> None:
    intent = classify_recall_intent(
        "UUMit 外部订单交付品质要求",
        task_context={"intent": "living_posture"},
    )

    assert intent.memory_cube == "project"
    assert intent.name in {"project_delivery", "living_posture"}
    assert intent.confidence >= 0.7
    assert any("living_posture" in reason.lower() or "project_delivery" in reason.lower() for reason in intent.reasons)


def test_classify_recall_intent_operator_preference_query() -> None:
    intent = classify_recall_intent("鸿哥 沟通风格")

    assert intent.name == "operator_preference"
    assert intent.memory_cube == "operator"
    assert intent.preferred_kinds == ("memory", "rule", "reflection")
    assert intent.suppressed_kinds == ("knowledge_page",)
    assert intent.confidence >= 0.75


def test_classify_recall_intent_research_queries() -> None:
    first = classify_recall_intent("Graphiti temporal knowledge graph 论文")
    second = classify_recall_intent("arxiv benchmark")
    mixed = classify_recall_intent("UUMit delivery benchmark paper", task_context={"task_type": "research"})

    assert first.name == "research"
    assert second.name == "research"
    assert mixed.name == "research"
    assert first.memory_cube == "research"
    assert second.memory_cube == "research"
    assert mixed.memory_cube == "research"
    assert "knowledge_page" not in first.suppressed_kinds
    assert "knowledge_page" not in second.suppressed_kinds
    assert "knowledge_page" not in mixed.suppressed_kinds
    assert first.confidence >= 0.7
    assert second.confidence >= 0.7
    assert mixed.confidence >= 0.7


def test_classify_recall_intent_news_queries() -> None:
    today = classify_recall_intent("今天新闻")
    ai = classify_recall_intent("AI 新闻")

    assert today.name == "news"
    assert ai.name == "news"
    assert today.memory_cube == "news"
    assert ai.memory_cube == "news"


def test_classify_recall_intent_report_query() -> None:
    intent = classify_recall_intent("rule_evolution_20260528_2b990a0c report")

    assert intent.name == "report"
    assert intent.memory_cube == "governance"
    assert intent.preferred_kinds == ("reflection",)
    assert intent.suppressed_kinds == ()
