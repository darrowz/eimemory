from eimemory.recall import analyze_lexical_signal


def test_query_relevant_record_terms_match_full_tokenization():
    import random
    from eimemory.recall import lexical
    randomizer = random.Random(17)
    words = ['任务', '进展', '任务进展已完成', 'UUMit', 'MIPROv2', 'v2',
             'alpha_beta', 'alpha', '12.5', '甲乙丙丁', '完成', '甲乙', '乙丙']
    for _ in range(200):
        query = lexical._clean_text(' '.join(randomizer.choices(words, k=6)))
        record = lexical._clean_text(' '.join(randomizer.choices(words, k=25)))
        requested = set(lexical._extract_terms(query))
        expected = set(lexical._extract_terms(record)) & requested
        assert lexical._matching_record_terms(record, requested) == expected


def test_chinese_anchor_survives_a_single_character_question_prefix():
    from eimemory.storage.sqlite_store import SqliteRecordStore

    query = "问福建供电"
    signal = analyze_lexical_signal(query, "福建 供电", record_kind="memory", record_source="test")
    assert {"福建", "供电"}.issubset(signal.exact_phrase_hits)
    store = object.__new__(SqliteRecordStore)
    assert {"福建", "供电"}.issubset(store._candidate_query_terms(query))


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


def test_analyze_lexical_signal_does_not_match_version_inside_compound_token() -> None:
    signal = analyze_lexical_signal(
        query="UUMit 交付品质 海报 v2",
        record_text='Our approach employs DSPy"s MIPROv2 optimizer for modular prompt tuning.',
        record_kind="claim_card",
        record_source="eimemory.knowledge.claims",
        recall_filters={"intent_name": "project_delivery"},
    )

    assert signal.score == 0.0
    assert signal.version_hits == ()
    assert "v2" not in signal.exact_phrase_hits


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
