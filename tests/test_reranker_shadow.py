"""Explicit offline scorer fixtures; these tests never contact a model service."""
from __future__ import annotations

from dataclasses import asdict, replace
from importlib import import_module
import json

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.contracts import CandidateRef, ExactScope
from eimemory.retrieval.relevance import RelevanceAdmission, RelevanceConfig


def _record(index, *, scope=None, source_id="native"):
    return RecordEnvelope.create(
        kind="memory", title=f"Evidence {index}", content={"text": f"Private evidence number {index}."},
        scope=scope or ScopeRef(user_id="owner"), source_id=source_id,
    )


def _ref(record):
    return CandidateRef(record.record_id, ExactScope.from_scope(record.scope), record.source_id)


class _Scorer:
    def __init__(self, scores=None, callback=None):
        self.scores, self.callback, self.calls = scores, callback, []

    def score(self, query, texts, **kwargs):
        self.calls.append((query, list(texts), kwargs))
        if self.callback:
            self.callback()
        return list(range(len(texts))) if self.scores is None else self.scores


def _case(module, records, *, case_id="case-1", relevant=None, authorized=None):
    return module.ShadowCase(
        case_id=case_id, query="Private benchmark query", candidates=tuple(records),
        authorized_refs=frozenset(_ref(r) for r in records) if authorized is None else authorized,
        relevant_refs=frozenset({_ref(records[-1])}) if relevant is None else relevant,
    )


def test_enabled_relevance_admission_explicitly_does_not_call_reranker():
    records = [_record(i) for i in range(3)]
    scorer = _Scorer([0, 1, 10])
    gate = RelevanceAdmission(RelevanceConfig(enabled=True, api_key="not-a-real-key", revision="a" * 40), scorer)
    selected, report = gate.select(records, query="ordinary query", limit=3, validate=lambda _: True)
    assert selected == records
    assert scorer.calls == []
    assert report["reranker_called"] is False


def test_shadow_is_disabled_by_default_even_with_online_reranker_environment(monkeypatch):
    module = import_module("eimemory.retrieval.reranker_shadow")
    monkeypatch.setenv("EIMEMORY_RERANKER_ENABLED", "1")
    records = [_record(0)]
    scorer = _Scorer()
    report = module.evaluate_reranker_shadow(
        [_case(module, records)], scorer=scorer,
        validate=lambda _: pytest.fail("disabled shadow must not inspect authority"),
    )
    assert report["status"] == "disabled"
    assert report["model_calls"] == 0 and scorer.calls == []
    assert report["affects_delivery"] is False


def test_shadow_compares_bounded_authorized_candidates_without_changing_delivery():
    module = import_module("eimemory.retrieval.reranker_shadow")
    records = [_record(i) for i in range(30)]
    before = [asdict(r) for r in records]
    forbidden = _record("forbidden", scope=ScopeRef(user_id="other"))
    forged_source = replace(records[0], source_id="other-source")
    case = _case(module, [forbidden, forged_source, *records],
                 relevant=frozenset({_ref(records[23])}),
                 authorized=frozenset(_ref(r) for r in records))
    scorer = _Scorer()
    report = module.evaluate_reranker_shadow([case], scorer=scorer, validate=lambda _: True,
                                            config=module.ShadowConfig(enabled=True, max_candidates=24))
    result = report["cases"][0]
    assert report["status"] == "complete" and report["model_calls"] == 1
    assert len(scorer.calls[0][1]) == 24
    assert all("forbidden" not in text for text in scorer.calls[0][1])
    assert result["dropped_reasons"]["unauthorized_reference"] == 2
    assert result["dropped_reasons"]["candidate_budget"] == 6
    assert result["candidate_recall_at_k"] == 1.0
    assert result["baseline"]["mrr"] == 0.0  # The answer was outside delivery's top five.
    assert result["shadow"]["precision_at_1"] == 1.0
    assert result["shadow"]["mrr"] == 1.0
    assert result["baseline_order"] != result["shadow_order"]
    assert [asdict(r) for r in records] == before
    assert "Private evidence" not in json.dumps(report)
    assert "Private benchmark query" not in json.dumps(report)
    assert "other-source" not in json.dumps(report)
    assert report["affects_delivery"] is False


@pytest.mark.parametrize("count", [19, 33, True, 24.0])
def test_shadow_candidate_configuration_is_explicitly_between_twenty_and_thirty_two(count):
    module = import_module("eimemory.retrieval.reranker_shadow")
    with pytest.raises(ValueError, match="shadow_candidate_bounds"):
        module.ShadowConfig(enabled=True, max_candidates=count)


@pytest.mark.parametrize("scores", [[float("nan")], [True], ["1"], [], [1, 2], {"score": 1}])
def test_malformed_shadow_scores_cannot_publish_a_comparison(scores):
    module = import_module("eimemory.retrieval.reranker_shadow")
    records = [_record(0)]
    before = [asdict(r) for r in records]
    report = module.evaluate_reranker_shadow([_case(module, records)], scorer=_Scorer(scores),
                                            validate=lambda _: True, config=module.ShadowConfig(enabled=True))
    result = report["cases"][0]
    assert result["status"] == "unavailable"
    assert result["reason"] == "reranker_response_invalid"
    assert result["shadow_order"] == [] and result["shadow"] is None
    assert report["paired_case_count"] == 0
    assert [asdict(r) for r in records] == before


def test_scorer_failure_does_not_leak_remote_error_text_or_fabricate_quality():
    module = import_module("eimemory.retrieval.reranker_shadow")
    class Broken:
        def score(self, *_args, **_kwargs):
            raise RuntimeError("private query and secret endpoint credentials")
    result = module.evaluate_reranker_shadow(
        [_case(module, [_record(0)])], scorer=Broken(), validate=lambda _: True,
        config=module.ShadowConfig(enabled=True),
    )
    assert result["cases"][0]["reason"] == "shadow_scorer_failed"
    assert result["paired_case_count"] == 0
    assert "credentials" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["status", "content", "authorization"])
def test_shadow_revalidates_authority_and_record_snapshot_after_scoring(mutation):
    module = import_module("eimemory.retrieval.reranker_shadow")
    record = _record(0)
    authorized = [True]
    def mutate():
        if mutation == "status":
            record.status = "quarantined"
        elif mutation == "content":
            record.content["text"] = "Changed while scoring"
        else:
            authorized[0] = False
    report = module.evaluate_reranker_shadow(
        [_case(module, [record])], scorer=_Scorer([1.0], mutate),
        validate=lambda _: authorized[0], config=module.ShadowConfig(enabled=True),
    )
    result = report["cases"][0]
    assert result["status"] == "unavailable" and result["reason"] == "authority_changed"
    assert result["shadow"] is None and result["shadow_order"] == []


def test_shadow_checks_authority_again_before_any_text_reaches_the_scorer():
    module = import_module("eimemory.retrieval.reranker_shadow")
    first, second = _record(0), _record(1)
    allowed = [True]
    def validate(record):
        if record is second:
            allowed[0] = False
            return True
        return allowed[0]
    scorer = _Scorer()
    report = module.evaluate_reranker_shadow(
        [_case(module, [first, second])], scorer=scorer, validate=validate,
        config=module.ShadowConfig(enabled=True),
    )
    assert report["cases"][0]["reason"] == "authority_changed"
    assert scorer.calls == [] and report["model_calls"] == 0


def test_shadow_unlabelled_cases_do_not_become_negative_query_quality():
    module = import_module("eimemory.retrieval.reranker_shadow")
    record = _record(0)
    case = module.ShadowCase("unlabelled", "Fixture query", (record,), frozenset({_ref(record)}))
    report = module.evaluate_reranker_shadow([case], scorer=_Scorer(), validate=lambda _: True,
                                            config=module.ShadowConfig(enabled=True))
    assert report["status"] == "complete"
    assert report["case_count"] == 1 and report["paired_case_count"] == 0
    assert report["baseline"] is None and report["shadow"] is None
    assert report["candidate_recall_at_k"] is None


def test_shadow_deadline_is_shared_with_scorer_and_late_result_is_discarded(monkeypatch):
    module = import_module("eimemory.retrieval.reranker_shadow")
    clock = [10.0]
    monkeypatch.setattr(module, "perf_counter", lambda: clock[0])
    scorer = _Scorer([1.0], lambda: clock.__setitem__(0, 10.7))
    report = module.evaluate_reranker_shadow(
        [_case(module, [_record(0)])], scorer=scorer, validate=lambda _: True,
        config=module.ShadowConfig(enabled=True, timeout_seconds=2), deadline_at=10.5,
    )
    call = scorer.calls[0][2]
    assert call["deadline_at"] == 10.5 and call["timeout_seconds"] == .5
    assert report["cases"][0]["reason"] == "shadow_deadline_exceeded"
    assert report["cases"][0]["shadow_order"] == []


def test_shadow_skips_expired_and_inactive_candidates_without_model_calls(monkeypatch):
    module = import_module("eimemory.retrieval.reranker_shadow")
    monkeypatch.setattr(module, "perf_counter", lambda: 5.0)
    record = _record(0)
    scorer = _Scorer()
    expired = module.evaluate_reranker_shadow(
        [_case(module, [record])], scorer=scorer, validate=lambda _: pytest.fail("expired"),
        config=module.ShadowConfig(enabled=True), deadline_at=4.0,
    )
    assert expired["cases"][0]["reason"] == "shadow_deadline_exceeded"
    record.status = "quarantined"
    inactive = module.evaluate_reranker_shadow(
        [_case(module, [record])], scorer=scorer, validate=lambda _: True,
        config=module.ShadowConfig(enabled=True),
    )
    assert inactive["cases"][0]["dropped_reasons"]["inactive_record"] == 1
    assert inactive["model_calls"] == 0 and scorer.calls == []


def test_authority_timeout_cannot_be_reported_as_a_successful_negative_case(monkeypatch):
    module = import_module("eimemory.retrieval.reranker_shadow")
    clock = [1.0]
    monkeypatch.setattr(module, "perf_counter", lambda: clock[0])
    def validate(_record):
        clock[0] = 4.0
        return False
    report = module.evaluate_reranker_shadow(
        [_case(module, [_record(0)], relevant=frozenset())], scorer=_Scorer(), validate=validate,
        config=module.ShadowConfig(enabled=True, timeout_seconds=2),
    )
    assert report["cases"][0]["status"] == "unavailable"
    assert report["cases"][0]["reason"] == "shadow_deadline_exceeded"
    assert report["paired_case_count"] == 0


def test_shadow_reports_paired_quality_false_positive_rate_and_measured_p95(monkeypatch):
    module = import_module("eimemory.retrieval.reranker_shadow")
    clock = [10.0]
    durations = iter([.01, .02, .03, .04])
    monkeypatch.setattr(module, "perf_counter", lambda: clock[0])
    scorer = _Scorer(callback=lambda: clock.__setitem__(0, clock[0] + next(durations)))
    records = [_record(0), _record(1)]
    cases = [_case(module, records, case_id=f"quality-{index}",
                   relevant=frozenset() if index == 3 else frozenset({_ref(records[1])})) for index in range(4)]
    report = module.evaluate_reranker_shadow(cases, scorer=scorer, validate=lambda _: True,
                                            config=module.ShadowConfig(enabled=True))
    assert report["paired_case_count"] == 4
    assert report["baseline"]["precision_at_1"] == 0
    assert report["shadow"]["precision_at_1"] == .75
    assert report["baseline"]["false_positive_rate"] == 1.0
    assert report["shadow"]["false_positive_rate"] == 1.0
    assert report["latency_ms"]["p95"] == pytest.approx(40.0)
    assert report["latency_ms"]["sample_count"] == 4
    assert report["evidence_origin"] == "caller_supplied_offline_cases"
