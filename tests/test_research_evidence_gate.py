from __future__ import annotations

from eimemory.knowledge.daily_brief import build_daily_brief
from eimemory.knowledge.evidence_gate import filter_answer_evidence, grade_research_evidence
from eimemory.knowledge.synthesis import build_research_digest
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef


SCOPE = ScopeRef(agent_id="research", workspace_id="evidence-gate")


def _record(kind: str, title: str, *, source_url: str = "", published_at: str = "", confidence: float = 0.8, conflict: bool = False) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind=kind,
        title=title,
        summary=title,
        scope=SCOPE,
        source="test.research",
        content={
            "source_url": source_url,
            "canonical_url": source_url,
            "published_at": published_at,
            "confidence": confidence,
            "claim_text": title,
            "conflict": conflict,
        },
        meta={
            "source_url": source_url,
            "published_at": published_at,
            "confidence": confidence,
            "conflict": conflict,
        },
    )


def test_research_digest_excludes_items_without_source_date_or_conflict_check() -> None:
    good = _record("claim_card", "Graph memory improves recall", source_url="https://example.com/paper", published_at="2026-06-29", confidence=0.9)
    no_date = _record("claim_card", "Undated claim", source_url="https://example.com/no-date", confidence=0.9)
    conflict = _record("claim_card", "Conflicting claim", source_url="https://example.com/conflict", published_at="2026-06-29", conflict=True)

    digest = build_research_digest(paper_sources=[], claim_cards=[good, no_date, conflict], knowledge_pages=[], limit=5, digest_date="2026-06-30")

    assert digest["notable_claims"] == [
        {
            "claim_id": good.record_id,
            "paper_source_id": "",
            "text": "Graph memory improves recall",
            "confidence": 0.9,
            "source": "https://example.com/paper",
            "published_at": "2026-06-29",
            "evidence_tier": "T2",
            "conflict_check": "clear",
        }
    ]
    assert digest["evidence_gate"]["excluded_count"] == 2
    assert {item["reason"] for item in digest["evidence_gate"]["excluded"]} == {"missing_date", "conflict_unresolved"}


def test_research_digest_gates_papers_not_only_claims() -> None:
    good = _record("paper_source", "Grounded paper", source_url="https://example.com/paper", published_at="2026-06-29")
    weak = _record("paper_source", "Untethered paper")
    weak.time = TimeRef(
        created_at="2026-06-30T08:00:00+08:00",
        updated_at="2026-06-30T08:00:00+08:00",
        occurred_at="2026-06-30T08:00:00+08:00",
    )

    digest = build_research_digest(paper_sources=[weak, good], claim_cards=[], knowledge_pages=[], limit=5, digest_date="2026-06-30")

    assert [item["record_id"] for item in digest["top_papers"]] == [good.record_id]
    assert digest["evidence_gate"]["excluded_count"] == 1
    assert digest["evidence_gate"]["excluded"][0]["record_id"] == weak.record_id
    assert digest["evidence_gate"]["excluded"][0]["reason"] == "missing_source"


def test_daily_brief_hides_ungated_research_and_news_items() -> None:
    good_news = _record("news", "Good news", source_url="https://example.com/news", published_at="2026-06-30")
    bad_news = _record("news", "Bad news", source_url="", published_at="2026-06-30")

    brief = build_daily_brief([good_news, bad_news], date="2026-06-30", research_lookback_days=1)

    assert brief["news_digest"]["count"] == 1
    assert brief["news_digest"]["items"][0]["title"] == "Good news"
    assert brief["news_digest"]["items"][0]["evidence_gate"]["ok"] is True
    assert brief["source_health"]["evidence_gate"]["excluded_count"] == 1


def test_final_answer_context_filters_research_and_news_without_evidence_gate() -> None:
    good_news = _record("news", "Good news", source_url="https://example.com/news", published_at="2026-06-30")
    bad_news = _record("news", "Bad news", source_url="", published_at="2026-06-30")
    ordinary = _record("reflection", "Local operator preference")

    report = filter_answer_evidence([good_news, bad_news, ordinary], task_type="research.answer")

    assert [record.title for record in report["records"]] == ["Good news", "Local operator preference"]
    assert report["evidence_gate"]["excluded_count"] == 1
    assert report["evidence_gate"]["excluded"][0]["record_id"] == bad_news.record_id
    assert report["evidence_gate"]["excluded"][0]["reason"] == "missing_source"
    assert report["evidence_gate"]["kept_count"] == 2


def test_final_answer_context_filters_weak_knowledge_candidates() -> None:
    candidate = RecordEnvelope.create(
        kind="knowledge_candidate",
        title="Weak generated knowledge candidate",
        summary="Weak generated knowledge candidate",
        scope=SCOPE,
        source="eimemory.news.collect",
        content={"published_at": "2026-06-30", "confidence": 0.9},
        meta={"published_at": "2026-06-30", "confidence": 0.9},
    )
    ordinary = _record("reflection", "Local operator preference")

    report = filter_answer_evidence([candidate, ordinary], task_type="research.answer")

    assert [record.title for record in report["records"]] == ["Local operator preference"]
    assert report["evidence_gate"]["excluded_count"] == 1
    assert report["evidence_gate"]["excluded"][0]["record_id"] == candidate.record_id
    assert report["evidence_gate"]["excluded"][0]["reason"] == "missing_source"


def test_news_evidence_gate_uses_record_time_but_still_requires_url() -> None:
    good_news = _record("news", "Timed news", source_url="https://example.com/news")
    good_news.time = TimeRef(
        created_at="2026-06-30T08:00:00+08:00",
        updated_at="2026-06-30T08:00:00+08:00",
        occurred_at="2026-06-30T08:00:00+08:00",
    )
    no_url = _record("news", "Untethered news", source_url="")
    no_url.time = good_news.time

    assert grade_research_evidence(good_news)["ok"] is True
    assert grade_research_evidence(good_news)["published_at"] == "2026-06-30"
    assert grade_research_evidence(no_url)["reason"] == "missing_source"


def test_internal_research_artifact_without_real_source_is_rejected_even_with_record_time() -> None:
    paper = _record("paper_source", "Internal paper without URL")
    paper.time = TimeRef(
        created_at="2026-06-30T08:00:00+08:00",
        updated_at="2026-06-30T08:00:00+08:00",
        occurred_at="2026-06-30T08:00:00+08:00",
    )

    gate = grade_research_evidence(paper)

    assert gate["ok"] is False
    assert gate["reason"] == "missing_source"
    assert gate["published_at"] == "2026-06-30"


def test_claim_evidence_gate_accepts_internal_paper_source_attribution() -> None:
    claim = RecordEnvelope.create(
        kind="claim_card",
        title="Memory retrieval improves long horizon agent performance.",
        summary="Memory retrieval improves long horizon agent performance.",
        scope=SCOPE,
        source="eimemory.knowledge.claims",
        content={
            "paper_source_id": "paper_123",
            "claim_text": "Memory retrieval improves long horizon agent performance.",
            "confidence": 0.72,
        },
        evidence=["paper_123"],
        provenance={"published_at": "2026-04-20", "paper_source_id": "paper_123"},
        meta={"paper_source_id": "paper_123", "confidence": 0.72},
    )

    gate = grade_research_evidence(claim)

    assert gate["ok"] is True
    assert gate["source"] == "paper_123"
    assert gate["published_at"] == "2026-04-20"
