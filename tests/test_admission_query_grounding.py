"""Admission must score the host request, not Context entities expansion."""
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall.lexical import analyze_lexical_signal, _content_query_terms, _extract_terms, _clean_text
from eimemory.retrieval.answer_requirements import admission_query, requested_attribute
from eimemory.retrieval.engine import GovernedRecallEngine


def _engine():
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine._relevance_selector_thresholds = {"non_exact_min_grounding": 0.08}
    return engine


def _item(summary: str, *, title: str = "memory") -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory",
        title=title,
        summary=summary,
        content={},
        scope=ScopeRef(),
    )


def test_admission_query_strips_context_entities_suffix():
    q = "检查eimemory\nContext entities: eimemory 1.15.26, recall-quality-fixes"
    assert admission_query(q) == "检查eimemory"
    assert admission_query("plain request") == "plain request"
    assert admission_query("") == ""


def test_context_entities_do_not_impose_release_status_attribute():
    expanded = "检查eimemory\nContext entities: eimemory 1.15.26 部署验收"
    assert requested_attribute(expanded) == "release_status"
    assert requested_attribute(admission_query(expanded)) == ""


def test_grounding_uses_admission_query_not_context_entities():
    engine = _engine()
    item = _item("eimemory 召回质量：检查 grounding 与属性门，确认当前部署状态。")
    expanded = "检查eimemory\nContext entities: eimemory 1.15.26, recall-quality-fixes branch tip"
    # Contaminated hint that would previously overrule the diluted score.
    hints = {
        engine._record_key(item): {
            "_fts_arm_present": True,
            "lexical_signal": {"score": 0.02},
            "vector_score": 0.4,
        }
    }
    score_exp, reason_exp = engine._non_exact_grounding_score(
        query=expanded,
        item=item,
        evidence={"vector_match"},
        component_hints_by_ref=hints,
        graph_grounded_ids=set(),
        vector_min_score=0.12,
        explicit_recall_boundary=False,
    )
    score_lit, _ = engine._non_exact_grounding_score(
        query="检查eimemory",
        item=item,
        evidence={"vector_match"},
        component_hints_by_ref={},
        graph_grounded_ids=set(),
        vector_min_score=0.12,
        explicit_recall_boundary=False,
    )
    assert score_exp >= 0.08, (score_exp, reason_exp)
    assert score_lit >= 0.08
    assert score_exp == score_lit


def test_expanded_query_ignores_contaminated_hint_lexical_signal():
    engine = _engine()
    # Record matches host request weakly under expanded dilution but strongly
    # under literal admission; a high contaminated hint must not be required,
    # and a low one must not block.
    item = _item("查看 eimemory 运行状态与召回空结果率")
    expanded = "查看eimemory\nContext entities: " + ", ".join(
        f"entity-{i}-version-1.15.{i}" for i in range(40)
    )
    hints = {
        engine._record_key(item): {
            "_fts_arm_present": True,
            "lexical_signal": {"score": 0.01},
        }
    }
    score, reason = engine._non_exact_grounding_score(
        query=expanded,
        item=item,
        evidence={"vector_match"},
        component_hints_by_ref=hints,
        graph_grounded_ids=set(),
        vector_min_score=0.12,
        explicit_recall_boundary=False,
    )
    assert score >= 0.08, (score, reason)


def test_chinese_content_terms_prefer_nonoverlapping_segments():
    q = "提交推送部署切换到新版本并验收"
    terms = _extract_terms(_clean_text(q))
    content = _content_query_terms(terms, query_text=q)
    # Overlapping bigrams would keep 交推/送部/署切…; non-overlapping drops them.
    assert "交推" not in content
    assert "提交" in content
    assert "推送" in content
    rec = "已经提交并推送了 eimemory 代码，部署切换还在进行"
    assert analyze_lexical_signal(q, rec).score >= 0.08


def test_howto_content_rescoring_still_clears_threshold():
    q = "更换镜头之前应该先做什么？"
    rec = "更换镜头之前应该先清洁卡口并确认对焦"
    assert analyze_lexical_signal(q, rec).score >= 0.08
