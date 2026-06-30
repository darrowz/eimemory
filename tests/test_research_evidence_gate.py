from __future__ import annotations

from eimemory.knowledge.daily_brief import build_daily_brief
from eimemory.knowledge.evidence_gate import filter_answer_evidence
from eimemory.knowledge.synthesis import build_research_digest
from eimemory.models.records import RecordEnvelope, ScopeRef


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
