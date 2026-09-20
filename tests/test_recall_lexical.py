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

def test_natural_howto_paraphrases_keep_content_anchors_above_grounding_floor():
    """Issue #1: interrogative wrappers must not dilute procedure paraphrases.

    Dense cosine alone must not admit (see dense-admission contract). Lexical
    grounding has to recover content anchors like 镜头/抖音+链接 without
    treating hybrid provider rank as keyword evidence.
    """
    from eimemory.retrieval.engine import GovernedRecallEngine

    threshold = float(GovernedRecallEngine._relevance_selector_thresholds["non_exact_min_grounding"])
    cases = [
        ("更换镜头之前应该先做什么？", "相机更换镜头前，先关闭电源，再卸下镜头，避免灰尘进入机身。"),
        ("抖音链接应该怎么处理", "抖音短链不要停在 ies 分享壳或 curl：先解析 aweme_id，再用浏览器打开作品页面，等待作品标题再抽文案。鸿哥发抖音默认要评估。"),
        ("重启之后应该先做什么", "网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。"),
        ("收到抖音分享地址后该如何提取文案？", "抖音短链不要停在 ies 分享壳或 curl：先解析 aweme_id，再用浏览器打开作品页面，等待作品标题再抽文案。鸿哥发抖音默认要评估。"),
        ("会话恢复时如何检查之前授权的工作？", "网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。"),
    ]
    for query, text in cases:
        signal = analyze_lexical_signal(query, text, record_kind="memory", record_source="test")
        assert signal.score >= threshold, (query, signal.score, signal.token_hits)


def test_secret_attribute_questions_stay_below_grounding_without_secret_fact():
    """True-negative near misses must remain fail-closed after paraphrase repair."""
    from eimemory.retrieval.engine import GovernedRecallEngine
    from eimemory.retrieval.answer_requirements import requested_attribute, supports_answer_requirements

    threshold = float(GovernedRecallEngine._relevance_selector_thresholds["non_exact_min_grounding"])
    preference = "网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。"
    query = "网关重启的管理员密码是什么"
    assert requested_attribute(query) == "secret"
    assert not supports_answer_requirements(query, preference)
    signal = analyze_lexical_signal(query, preference, record_kind="memory", record_source="test")
    assert signal.score < threshold
