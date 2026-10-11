"""Dated aggregates sharing a summary are not interchangeable knowledge."""
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_recall import run_production_recall_eval
from eimemory.knowledge.synthesis import digest_to_record
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.scheduler.jobs import _production_recall_smoke_dataset


SUMMARY = ("Recent papers: deterministic evaluation and reliable retrieval. "
           "Themes: bounded memory, evidence and reproducible ranking. " * 4)
SCOPE = ScopeRef(agent_id="digest-test", workspace_id="quality", user_id="operator")


def digest(date, conclusion, source_ids):
    record = digest_to_record({
        "digest_date": date, "summary": SUMMARY,
        "source_ids": source_ids if isinstance(source_ids, list) else [source_ids],
        "notable_claims": [{"text": conclusion}], "top_papers": [],
        "themes": [], "open_questions": [],
    }, scope=SCOPE)
    record.record_id = record.content["knowledge_page_id"]
    record.source_id = "fixture-shared"
    record.time = replace(record.time, updated_at=f"{date}T01:00:00Z")
    return record


@pytest.mark.parametrize("detection", ["meta", "content", "source"])
def test_digest_full_identity_keeps_distinct_evidence_with_same_summary(detection):
    old = digest("2026-10-09", "Retain the original evaluation result.", "paper-a")
    new = digest("2026-10-10", "The independent follow-up result differs.", "paper-b")
    for record in (old, new):
        if detection != "meta":
            record.meta.pop("page_type", None)
        if detection != "content":
            record.content.pop("page_type", None)
        if detection != "source":
            record.source = "operator.aggregate"
    topic = RecordEnvelope.create(kind="knowledge_page", title="Research topic",
                                  summary=SUMMARY, content={"page_type": "topic"},
                                  source_id="fixture-shared", scope=SCOPE)
    for records in ([topic, old, new], [new, old, topic]):
        assert {r.record_id for r in MemoryAPI._dedupe_records(records)} == {
            topic.record_id, old.record_id, new.record_id}


def test_exact_digest_duplicate_still_collapses_and_namespaces_remain_separate():
    old = digest("2026-10-09", "Retain the original evaluation result.", "paper-a")
    same = deepcopy(old)
    same.record_id = "duplicate-digest"
    same.meta["quality"] = {"salience_score": 0.9}
    assert len(MemoryAPI._dedupe_records([old, same])) == 1
    other = deepcopy(same)
    other.record_id = "other-source"
    other.source_id = "another-partition"
    assert len(MemoryAPI._dedupe_records([old, other])) == 2


def test_digest_tail_difference_cannot_hide_behind_summary_prefix():
    old = digest("2026-10-09", "Retain the original evaluation result.", "paper-a")
    changed = deepcopy(old)
    changed.record_id = "different-conclusion"
    changed.content["sections"] = [{"name": "notable_claims", "text": "Reject this conclusion."}]
    assert len(MemoryAPI._dedupe_records([old, changed])) == 2


def test_native_smoke_retains_expected_digest_among_same_prefix_pages(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        topic = RecordEnvelope.create(kind="knowledge_page", title="Research topic",
            summary="Research topic: reliable retrieval, deterministic evaluation and bounded memory.",
            content={"page_type": "topic", "source_ids": ["paper-topic"]},
            source="eimemory.knowledge.compiler", source_id="fixture-shared", scope=SCOPE)
        topic.time = replace(topic.time, updated_at="2026-10-08T01:00:00Z")
        sources = [f"paper-{i:03d}" for i in range(256)]
        target = digest("2026-10-10", "Retain the original evaluation result.", sources)
        newer = digest("2026-10-11", "The independent follow-up result differs.", sources)
        for record in (topic, target, newer):
            runtime.store.append(record)
        dataset = _production_recall_smoke_dataset(runtime, scope=asdict(SCOPE),
                                                  stable_before="2026-10-11T00:30:00Z")
        case = next(c for c in dataset["cases"] if c["expected_record_ids"] == [target.record_id])
        assert not any(newer.record_id in c["expected_record_ids"] for c in dataset["cases"])
        assert case["expected_record_ids"] == [target.record_id]
        assert case["query"] == target.summary[:160]
        report = run_production_recall_eval(runtime, dataset, seed=False, persist_report=True)
        sample = next(s for s in report["samples"] if s["case_id"] == case["case_id"])
        assert target.record_id in sample["returned_record_ids"]
        assert sample["passed"] is True and report["false_recall_count"] == 0
        assert report["quality_gate"]["thresholds"]["false_recall_rate"] == 0.05
        assert report["blocked_reason"] == "recall_quality_evidence_incomplete"
        assert report["accepted"] is False
        # Ordinary queries retain digest suppression; only the explicit digest view admits them.
        assert runtime.memory._is_default_recall_suppressed_record(target, {})
    finally:
        runtime.close()
