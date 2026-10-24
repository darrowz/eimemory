"""An anchor reserve cannot put rejected evidence back into delivered slots."""
from contextlib import closing
from dataclasses import asdict

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.answer_requirements import supports_answer_requirements
from eimemory.retrieval.contracts import CandidateBatch, CandidateHit, CandidateRef, ExactScope
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(tenant_id="audit", agent_id="memory", workspace_id="anchors", user_id="owner")


def _record(text, index):
    return RecordEnvelope.create(kind="memory", title=f"Evidence {index}", summary=text,
        content={"text": text, "memory_type": "fact"},
        meta={"memory_type": "fact", "quality": {"capture_decision": "accept"}},
        scope=SCOPE, source="test", source_id="test")


@pytest.mark.parametrize(("query", "answers", "noise"), [
    ("Alpha 合同总金额是多少？", ["Alpha 合同总金额为 1200 元。", "Alpha 合同总金额 1200 元，已确认。"],
     "Alpha 预付款为 600 元。"),
    ("Alpha v1.2.3 部署状态是什么？", ["Alpha v1.2.3 已部署成功。", "Alpha v1.2.3 已发布上线。"],
     "Beta v1.2.3 已部署成功。"),
    ("Alpha v1.2.3 部署状态是什么？", ["Alpha v1.2.3 已部署成功。", "Alpha v1.2.3 已发布上线。"],
     "Alpha v1.2.2 已部署成功。"),
])
def test_anchor_reserve_requires_same_answer_support_as_normal_selection(tmp_path, query, answers, noise):
    assert all(supports_answer_requirements(query, text, []) for text in answers)
    assert not supports_answer_requirements(query, noise, [])
    with closing(RuntimeStore(tmp_path)) as store:
        records = [store.append(_record(text, index)) for index, text in enumerate([*answers, noise])]
        engine = MemoryAPI(store).recall_engine
        keys = [engine._record_key(record) for record in records]
        fusion = {"detail_by_ref": {key: {"score": score} for key, score in zip(keys, [0.9, 0.8, 0.7])}}
        hints = {key: {"lexical_score": 0.9} for key in keys}
        hints[keys[-1]].update(candidate_sources=["anchor"], quality_score=1.0)

        selected, report = engine._select_post_fusion_items(records, query=query, limit=2,
            fusion_state=fusion, component_hints_by_ref=hints)
        assert [record.record_id for record in selected] == [record.record_id for record in records[:2]]
        assert report["anchor_reserve_swap"] is False
        assert report["dropped_reasons"]["requested_attribute_missing"] == 1


def test_reserve_skips_rejected_anchor_and_keeps_searching_for_grounded_one(tmp_path):
    query = "Alpha 合同总金额是多少？"
    texts = ["Alpha 合同总金额为 1200 元。", "Alpha 合同总金额 1200 元，已确认。",
             "Alpha 预付款为 600 元。", "Alpha 合同总金额为 1200 元，验收后付款。"]
    with closing(RuntimeStore(tmp_path)) as store:
        records = [store.append(_record(text, index)) for index, text in enumerate(texts)]
        engine = MemoryAPI(store).recall_engine
        keys = [engine._record_key(record) for record in records]
        fusion = {"detail_by_ref": {key: {"score": 0.9 - index / 10} for index, key in enumerate(keys)}}
        hints = {key: {"lexical_score": 0.9} for key in keys}
        for key in keys[2:]:
            hints[key].update(candidate_sources=["anchor"], quality_score=1.0)
        selected, report = engine._select_post_fusion_items(records, query=query, limit=2,
            fusion_state=fusion, component_hints_by_ref=hints)
        assert [record.record_id for record in selected] == [records[0].record_id, records[3].record_id]
        assert report["anchor_reserve_swap"] is True
        # The replacement is still after the first selected record in fused
        # order; the explicit reserve flag identifies the changed slot.
        assert report["preserved_fused_order"] is True
        assert report["dropped_reasons"]["requested_attribute_missing"] == 1


def test_memory_api_cannot_reintroduce_wrong_amount_through_anchor_reserve(tmp_path):
    query = "Alpha 合同总金额是多少？"
    texts = ["Alpha 合同总金额为 1200 元。", "Alpha 合同总金额 1200 元，已确认。",
             "Alpha 预付款为 600 元。"]
    with closing(RuntimeStore(tmp_path)) as store:
        records = [store.append(_record(text, index)) for index, text in enumerate(texts)]

        class ControlledSource:
            name = "synthetic-anchor-source"

            def search(self, request):
                return CandidateBatch(hits=tuple(CandidateHit(
                    ref=CandidateRef(item.record_id, ExactScope.from_scope(item.scope), item.source_id),
                    source_rank=index + 1, source_score=0.9 - index / 10,
                    component_hints={"lexical_score": 0.9 - index / 10, "vector_score": 0.0,
                                     "quality_score": 0.9,
                                     "candidate_sources": ["anchor"] if index == 2 else ["fts"]},
                ) for index, item in enumerate(records)))

        bundle = MemoryAPI(store, candidate_source=ControlledSource()).recall(
            query=query, scope=asdict(SCOPE), limit=2,
            task_context={"exact_scope_only": True, "source_ids": ["test"]})
        assert {item.record_id for item in bundle.items} == {item.record_id for item in records[:2]}
        assert bundle.explanation["relevance_selector"]["anchor_reserve_swap"] is False
