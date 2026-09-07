from dataclasses import replace
import json

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.relevance import (
    RelevanceAdmission, RelevanceConfig, RelevanceUnavailable, TEIReranker,
    authoritative_text, record_digest, validate_scores,
)


def record(text, **kwargs):
    return RecordEnvelope.create(kind="memory", title=text, summary=text,
        content={"text": text}, scope=ScopeRef(user_id="owner"), **kwargs)


class Scorer:
    def __init__(self, scores):
        self.scores, self.calls = scores, []

    def score(self, query, texts, **kwargs):
        self.calls.append((query, texts))
        return self.scores[:len(texts)]


def test_admission_reranks_rejects_tails_and_never_pads():
    items = [record("a"), record("b"), record("c")]
    gate = RelevanceAdmission(RelevanceConfig(min_score=0), Scorer([-4, 3, -2]))
    selected, report = gate.select(items, query="question", limit=5, validate=lambda _: True)
    assert selected == [items[1]]
    assert report["status"] == "evidence_found"
    assert report["dropped_reasons"] == {"insufficient_relevance": 2}


def test_no_evidence_is_not_service_failure():
    items = [record("unrelated")]
    gate = RelevanceAdmission(RelevanceConfig(), Scorer([-2]))
    assert gate.select(items, query="question", limit=5, validate=lambda _: True)[1]["status"] == "no_evidence"


def test_unavailable_does_not_fall_back_to_similarity():
    class Broken:
        def score(self, *args, **kwargs):
            raise RelevanceUnavailable("reranker_busy")
    selected, report = RelevanceAdmission(RelevanceConfig(), Broken()).select(
        [record("unrelated")], query="question", limit=5, validate=lambda _: True)
    assert selected == []
    assert report["status"] == "unavailable"


def test_unauthorized_text_never_leaves_authority_and_mutations_fail_closed():
    private, valid = record("private"), record("valid")
    scorer = Scorer([3])
    checks = 0
    def validate(item):
        nonlocal checks
        if item is private:
            return False
        checks += 1
        return checks == 1
    selected, report = RelevanceAdmission(RelevanceConfig(), scorer).select(
        [private, valid], query="question", limit=5, validate=validate)
    assert scorer.calls[0][1] == ["valid"]
    assert not selected and report["status"] == "unavailable"
    assert report["dropped_reasons"]["authority_changed_during_scoring"] == 1


def test_exact_lookup_is_distinct_and_still_authorized():
    item = record("Exact title")
    scorer = Scorer([])
    gate = RelevanceAdmission(RelevanceConfig(), scorer)
    selected, report = gate.select([item], query="Exact title", limit=5, validate=lambda _: True)
    assert selected == [item] and report["mode"] == "identity_lookup" and not scorer.calls
    assert not gate.select([item], query="Exact title", limit=5, validate=lambda _: False)[0]


def test_budget_and_duplicate_content_are_bounded():
    scorer = Scorer([1, 2])
    items = [record("same"), record("same"), record("second"), record("third")]
    selected, report = RelevanceAdmission(RelevanceConfig(max_candidates=2), scorer).select(
        items, query="question", limit=5, validate=lambda _: True)
    assert len(scorer.calls[0][1]) == 2 and len(selected) == 2
    assert report["dropped_reasons"]["duplicate_content"] == 1
    assert report["dropped_reasons"]["candidate_budget"] == 1


@pytest.mark.parametrize("rows", [None, {}, [], [{"index": 0, "score": float("nan")}],
    [{"index": True, "score": 1}], [{"index": 1, "score": 1}],
    [{"index": 0, "score": "1"}], [{"index": 0, "score": True}]])
def test_malformed_scores_rejected(rows):
    with pytest.raises(RelevanceUnavailable):
        validate_scores(rows, 1)


def test_scores_restore_input_order_and_reject_duplicate_indices():
    assert validate_scores([{"index": 1, "score": 2}, {"index": 0, "score": -1}], 2) == [-1, 2]
    with pytest.raises(RelevanceUnavailable):
        validate_scores([{"index": 0, "score": 2}, {"index": 0, "score": -1}], 2)


@pytest.mark.parametrize("endpoint", ["https://remote.example", "http://localhost:80",
    "http://127.0.0.1@remote.example", "http://user:pass@127.0.0.1", "http://127.0.0.1/?key=x"])
def test_no_external_inference_endpoint(endpoint):
    with pytest.raises(ValueError):
        RelevanceConfig(endpoint=endpoint)


def test_client_requests_raw_scores_without_logging_content():
    config = RelevanceConfig(api_key="secret")
    client = TEIReranker(config)
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit): return b'[{"index":0,"score":2.5}]'
    class Opener:
        def open(self, request, **kwargs):
            assert json.loads(request.data)["raw_scores"] is True
            assert request.get_header("Authorization") == "Bearer secret"
            return Response()
    client._opener = Opener()
    assert client.score("private question", ["private fact"]) == [2.5]
    assert "secret" not in repr(config) and "secret" not in json.dumps(config.identity())


def test_projection_deduplicates_and_preserves_end_of_long_body():
    item = record("title")
    item.detail = "title\n" + "prefix " * 200 + "the actual last fact"
    item.summary = item.detail
    text = authoritative_text(item, max_chars=300)
    assert len(text) <= 300 and text.endswith("the actual last fact")
    assert text.count("title") == 1
    assert record_digest(item) != record_digest(replace(item, detail="changed"))


def test_enabled_configuration_requires_pinned_identity_and_auth():
    with pytest.raises(ValueError):
        RelevanceConfig.from_env({"EIMEMORY_RERANKER_ENABLED": "1"})
    config = RelevanceConfig.from_env({"EIMEMORY_RERANKER_ENABLED": "1",
        "EIMEMORY_RERANKER_REVISION": "a" * 40, "EIMEMORY_RERANKER_API_KEY": "x"})
    assert config.enabled and config.revision == "a" * 40


def test_engine_optional_identity_binds_admission_configuration(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    monkeypatch.setenv("EIMEMORY_RERANKER_ENABLED", "1")
    monkeypatch.setenv("EIMEMORY_RERANKER_REVISION", "a" * 40)
    monkeypatch.setenv("EIMEMORY_RERANKER_API_KEY", "secret")
    runtime = Runtime.create(root=tmp_path)
    try:
        identity = runtime.memory.recall_engine.effective_identity()
        assert identity["relevance_admission"]["revision"] == "a" * 40
        assert "secret" not in json.dumps(identity)
    finally:
        runtime.close()
