"""Offline regressions for ranking and page evidence in governed recall."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, ExactScope
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(tenant_id="rank-test", agent_id="agent", workspace_id="workspace", user_id="user")


def _memory(text: str) -> RecordEnvelope:
    return RecordEnvelope.create(
        kind="memory", title=text, summary=text, content={"text": text},
        meta={"memory_type": "fact", "quality": {"capture_decision": "accept"}},
        scope=SCOPE, source_id="rank-source", source="test.rank",
    )


class _RankedSource:
    name = "offline-ranking-fixture"

    def __init__(self, records, extra_hints=None):
        self.records = records
        self.extra_hints = extra_hints or {}

    def search(self, request):
        return CandidateBatch(hits=tuple(
            CandidateHit(
                ref=CandidateRef(item.record_id, ExactScope.from_scope(item.scope), item.source_id),
                source_rank=rank,
                source_score=0.5 / rank,
                component_hints={"lexical_score": 0.5 / rank, "vector_score": 0.0,
                                 **self.extra_hints.get(item.record_id, {})},
            )
            for rank, item in enumerate(self.records, start=1)
        ))


def _recall(store, records, *, extra_hints=None, query="archive routing destination"):
    engine = GovernedRecallEngine(store=store, candidate_source=_RankedSource(records, extra_hints))
    return MemoryAPI(store, recall_engine=engine).recall(
        query=query, scope=asdict(SCOPE), limit=2,
        task_context={"source_ids": ["rank-source"], "exact_scope_only": True},
    )


@pytest.mark.parametrize("used,rejected", [(0, 1), (3, 2)])
def test_nonpositive_usage_feedback_cannot_add_a_positive_rrf_vote(tmp_path, used, rejected):
    with closing(RuntimeStore(tmp_path)) as store:
        answer = store.append(_memory("Archive routing destination is the shared endpoint."))
        weaker = store.append(_memory("Archive routing destination reference from an earlier note."))
        before = _recall(store, [answer, weaker])
        assert [item.record_id for item in before.items] == [answer.record_id, weaker.record_id]

        for index in range(used + rejected):
            key = "used_record_ids" if index < used else "rejected_record_ids"
            store.append(RecordEnvelope.create(
                kind="feedback", title=f"Ranking feedback {index}",
                content={"report_type": "memory_usage_telemetry", key: [weaker.record_id]},
                meta={"report_type": "memory_usage_telemetry"},
                scope=SCOPE, source_id="rank-source", source="test.feedback",
            ))

        after = _recall(store, [answer, weaker])
        assert [item.record_id for item in after.items] == [answer.record_id, weaker.record_id]
        weaker_detail = next(item for item in after.explanation["fusion"]["selected"]
                             if item["record_id"] == weaker.record_id)
        assert weaker_detail["contributions"].get("usage", 0.0) == 0.0


def test_positive_usage_feedback_still_contributes_to_fusion(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        answer = store.append(_memory("Archive routing destination is the shared endpoint."))
        used = store.append(_memory("Archive routing destination reference from an earlier note."))
        store.append(RecordEnvelope.create(
            kind="feedback", title="Useful routing reference",
            content={"report_type": "memory_usage_telemetry", "used_record_ids": [used.record_id]},
            meta={"report_type": "memory_usage_telemetry"},
            scope=SCOPE, source_id="rank-source", source="test.feedback",
        ))
        bundle = _recall(store, [answer, used])
        assert bundle.items[0].record_id == used.record_id
        assert bundle.explanation["fusion"]["selected"][0]["contributions"]["usage"] > 0


@pytest.mark.parametrize("adjustment", [-0.2, -0.001, 0.0])
def test_nonpositive_lifecycle_evidence_cannot_add_a_positive_rrf_vote(tmp_path, adjustment):
    with closing(RuntimeStore(tmp_path)) as store:
        answer = store.append(_memory("Archive routing destination is the shared endpoint."))
        weaker = store.append(_memory("Archive routing destination reference from an earlier note."))
        bundle = _recall(store, [answer, weaker], extra_hints={
            weaker.record_id: {"living_score_adjustments": {"total_adjustment": adjustment}},
        })
        assert [item.record_id for item in bundle.items] == [answer.record_id, weaker.record_id]
        weaker_detail = next(item for item in bundle.explanation["fusion"]["selected"]
                             if item["record_id"] == weaker.record_id)
        assert weaker_detail["contributions"].get("living", 0.0) == 0.0


def test_positive_lifecycle_evidence_still_contributes_to_fusion(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        answer = store.append(_memory("Archive routing destination is the shared endpoint."))
        stronger_lifecycle = store.append(_memory("Archive routing destination reference from an earlier note."))
        bundle = _recall(store, [answer, stronger_lifecycle], extra_hints={
            stronger_lifecycle.record_id: {"living_score_adjustments": {"total_adjustment": 0.2}},
        })
        assert bundle.items[0].record_id == stronger_lifecycle.record_id
        assert bundle.explanation["fusion"]["selected"][0]["contributions"]["living"] > 0


@pytest.mark.parametrize("query,answer_text", [
    ("archive routing destination", "Archive routing destination is the shared endpoint."),
    ("无人机试飞场水池", "无人机试飞场水池必须相邻，支持消防取水展示。"),
])
def test_page_pool_keeps_a_grounded_sibling_when_the_top_chunk_cannot_answer(tmp_path, query, answer_text):
    with closing(RuntimeStore(tmp_path)) as store:
        unrelated = _memory("Garden greenhouse temperature schedule.")
        answer = _memory(answer_text)
        for item in (unrelated, answer):
            item.content["page_id"] = "shared-document"
            store.append(item)
        bundle = _recall(store, [unrelated, answer], query=query, extra_hints={
            unrelated.record_id: {"_provider_rank_is_hybrid": True, "vector_score": 0.9, "quality_score": 0.9},
            answer.record_id: {"_provider_rank_is_hybrid": True},
        })
        assert [item.record_id for item in bundle.items] == [answer.record_id]
        selected = bundle.explanation["fusion"]["selected"][0]
        assert selected["record_id"] == answer.record_id
        assert set(selected["member_record_ids"]) == {unrelated.record_id, answer.record_id}
        # Pool aggregates remain diagnostic only. The authority wrapper must
        # restrict selector evidence/scores to the replacement's own record.
        assert selected["aggregate_score"] > selected["score"]
        assert bundle.explanation["relevance_selector"]["top_score"] == pytest.approx(selected["score"])
        assert "vector" not in selected["contributions"]
        assert "living" not in selected["contributions"]
        assert bundle.explanation["fusion"]["pre_pool_count"] == 2
        assert bundle.explanation["fusion"]["post_pool_count"] == 1


def test_page_replacement_keeps_its_own_rank_against_other_pages(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        unrelated = _memory("Garden greenhouse temperature schedule.")
        answer = _memory("Archive routing destination is the shared endpoint.")
        other_page = _memory("Archive routing destination has a verified fallback endpoint.")
        for item in (unrelated, answer):
            item.content["page_id"] = "shared-document"
        other_page.content["page_id"] = "independent-document"
        for item in (unrelated, answer, other_page):
            store.append(item)
        bundle = _recall(store, [unrelated, answer, other_page], extra_hints={
            unrelated.record_id: {"_provider_rank_is_hybrid": True, "vector_score": 0.9, "quality_score": 0.9},
            answer.record_id: {"_provider_rank_is_hybrid": True},
            other_page.record_id: {"_provider_rank_is_hybrid": True, "vector_score": 0.3},
        })
        assert [item.record_id for item in bundle.items] == [other_page.record_id, answer.record_id]


def test_page_pool_cannot_borrow_grounding_when_every_member_is_unrelated(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        records = [_memory("Garden greenhouse temperature schedule."), _memory("Garden humidity observations.")]
        for item in records:
            item.content["page_id"] = "shared-document"
            store.append(item)
        bundle = _recall(store, records, extra_hints={
            item.record_id: {"_provider_rank_is_hybrid": True, "vector_score": 0.9, "quality_score": 0.9}
            for item in records
        })
        assert bundle.items == []
        assert bundle.explanation["relevance_selector"]["status"] == "no_evidence"
