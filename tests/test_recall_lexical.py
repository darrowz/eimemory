from eimemory.recall import analyze_lexical_signal


def test_analyze_lexical_signal_recognizes_chinese_phrase_entity_and_version_hits() -> None:
    signal = analyze_lexical_signal(
        query="UUMit 外部订单 交付品质 海报 v2",
        record_text="UUMit 外部订单交付品质 海报 v2 验收清单（含外部订单进度）",
        record_kind="memory",
        record_source="operator.correction",
        recall_filters={"intent_name": "project_delivery"},
    )

    assert signal.score > 0.12
    assert "v2" in signal.version_hits
    assert "交付品质" in signal.exact_phrase_hits
    assert "外部订单" in signal.exact_phrase_hits
    assert "海报" in signal.exact_phrase_hits
    assert "uumit" in signal.exact_phrase_hits
    assert "交付" in signal.token_hits
    assert "品质" in signal.token_hits


def test_analyze_lexical_signal_marks_non_research_knowledge_page_penalty_reason() -> None:
    signal = analyze_lexical_signal(
        query="UUMit 交付品质",
        record_text="该知识页包含交付品质相关内容。",
        record_kind="knowledge_page",
        record_source="eimemory.knowledge.compiler",
        recall_filters={"intent_name": "project_delivery"},
    )

    assert signal.score > 0
    assert signal.suppression_reason
    assert "project_delivery" in signal.suppression_reason
    assert "knowledge_page" in signal.suppression_reason


def test_analyze_lexical_signal_research_query_no_suppression() -> None:
    signal = analyze_lexical_signal(
        query="UUMit 交付品质 海报 v2",
        record_text="UUMit 交付品质 海报 v2",
        record_kind="knowledge_page",
        record_source="eimemory.knowledge.compiler",
        recall_filters={"intent_name": "research"},
    )

    assert signal.score > 0
    assert signal.suppression_reason == ""
